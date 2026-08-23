"""User-editable settings, persisted to ``<data_dir>/settings.json``.

This is deliberately a *separate* layer from :mod:`flow.config`, not an
extension of it. ``flow.config`` resolves the engine's startup configuration
from defaults, ``config.toml`` and the environment; it is frozen, it is read
once, and a developer editing a TOML file expects it to win. This module is
the surface a *normal user* edits from the UI, and it must satisfy a
different set of rules:

* **It must never brick startup.** A corrupt, truncated, or hand-mangled
  ``settings.json`` falls back to defaults and logs, per D5. Every value is
  validated on load and an out-of-range one is replaced individually rather
  than discarding the whole file -- losing one bad field is recoverable,
  losing a user's entire trigger list because a boolean was spelled wrong is
  not.
* **It must be safe to write while the app is running.** Writes go to a
  temporary file in the same directory and are then atomically replaced, so
  an interrupted write (or a crash mid-save) leaves the previous file intact
  rather than a half-written one.
* **It must round-trip unknown keys.** A settings file written by a newer
  build and opened by an older one keeps the fields the older build does not
  understand, instead of silently deleting them on the next save.

Resolution order, so the precedence is explicit rather than emergent:
``settings.json`` (this module) overrides ``config.toml``/env values *only*
for the specific fields listed in :class:`Settings`. Everything else stays
exactly where it was. :func:`apply_to_config` is the single place that
overlay happens.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTINGS_FILENAME = "settings.json"

#: Bumped when a migration is needed. Unknown *higher* versions are read
#: best-effort rather than refused: a user who downgrades should lose the
#: settings the old build cannot express, not the ability to start.
SCHEMA_VERSION = 1

#: Where the overlay pill may sit. Values are contract with the UI and with
#: :mod:`flow.overlay`; adding one means handling it in both.
OVERLAY_POSITIONS = ("top-center", "bottom-center", "top-right", "bottom-right")

#: Cleanup styles a per-app rule may select. Mirrors
#: :class:`flow.profiles.CleanupStyle` without importing it, so settings stay
#: loadable in contexts that never touch the profile resolver.
CLEANUP_STYLES = ("casual", "formal", "verbatim")

#: How hard the self-correction pass looks. Mirrors
#: :data:`sway.corrections.MODES` without importing it, for the same reason
#: CLEANUP_STYLES mirrors CleanupStyle: settings must stay loadable in contexts
#: that never touch the transcript pipeline.
CONTEXT_MODES = ("off", "conservative", "balanced")

#: Trigger action types this build actually executes. D8.1 defines four;
#: only the two that make *no model call* are implemented here, so a trigger
#: can never silently add latency or spend quota. ``mode`` and ``transform``
#: are D8 proper.
TRIGGER_TYPES = ("macro", "date")

#: Hard ceilings. These exist to stop a hand-edited file from making the app
#: unusable (a 10-million-day retention, a 50,000-entry trigger list read on
#: every utterance), not to second-guess reasonable input.
MAX_TRIGGERS = 200
MAX_PHRASE_CHARS = 80
MAX_PAYLOAD_CHARS = 4000
MAX_APP_RULES = 200


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True, slots=True)
class Trigger:
    """One spoken phrase that becomes text instead of being transcribed.

    Only the zero-cost D8.1 types are representable: ``macro`` substitutes a
    literal payload, ``date`` substitutes today's date. Neither makes a
    network call, which is what makes it safe to check every trigger against
    every utterance locally.
    """

    id: str
    phrase: str
    payload: str
    type: str = "macro"
    enabled: bool = True
    created_at: float = 0.0

    def normalised_phrase(self) -> str:
        """Lower-cased, whitespace-collapsed, punctuation-trimmed.

        Matching happens against this form, so "My Email." and "my  email"
        are the same trigger. Kept here rather than in the resolver so the
        conflict check and the matcher cannot drift apart.
        """
        return normalise_phrase(self.phrase)


@dataclass(frozen=True, slots=True)
class AppRule:
    """A per-app cleanup style override, keyed on the focused process name."""

    process: str
    style: str

    def normalised_process(self) -> str:
        return self.process.strip().lower()


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the user can change from the UI.

    ``None`` consistently means "not overridden -- use whatever
    :mod:`flow.config` resolved", which is what lets this file stay small and
    lets a developer's ``config.toml`` keep working untouched.
    """

    #: Hotkey override, a pynput key name (e.g. ``ctrl_r``). ``None`` = config's.
    hotkey: str | None = None
    #: Input device override, matching ``[audio].device``. ``None`` = system default.
    audio_device: str | None = None
    #: Dictation language hint passed to Whisper. ``None`` = auto-detect.
    language: str | None = None
    #: Where the pill sits. Must be one of :data:`OVERLAY_POSITIONS`.
    overlay_position: str = "top-center"
    #: Whether the app registers itself in the per-user Run key.
    launch_at_login: bool = False
    #: Retention, in days. 0 disables pruning entirely.
    history_retention_days: int = 90
    latency_retention_days: int = 30
    #: False until the user finishes onboarding. Gates the first-run flow.
    onboarding_complete: bool = False
    #: How aggressively spoken self-corrections are resolved. One of
    #: :data:`CONTEXT_MODES`; "off" pastes what was literally said.
    context_mode: str = "conservative"
    #: Whether the cleanup pass strips "um"/"uh"/"you know". Off keeps them,
    #: for users who want a closer record of what they actually said.
    remove_fillers: bool = True
    #: User-defined zero-cost triggers.
    triggers: tuple[Trigger, ...] = ()
    #: Per-app cleanup style overrides, layered over :mod:`flow.profiles`.
    app_rules: tuple[AppRule, ...] = ()
    #: Fields written by a build newer than this one, preserved verbatim so a
    #: downgrade-then-save does not destroy them.
    unknown: dict[str, Any] = field(default_factory=dict, compare=False)


DEFAULTS = Settings()


# ---------------------------------------------------------------------------
# Normalisation and validation
# ---------------------------------------------------------------------------

#: Trailing punctuation a speaker never intends as part of a trigger phrase.
#: Whisper punctuates aggressively, so "my email." arrives with a full stop
#: the user never typed into the trigger.
_TRIM_CHARS = " \t\r\n.,!?;:'\"«»…“”‘’"


def normalise_phrase(phrase: str) -> str:
    """The canonical form a trigger phrase is matched and de-duplicated on."""
    return " ".join(phrase.strip(_TRIM_CHARS).lower().split())


def _clamp_int(value: Any, *, default: int, low: int, high: int, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        logger.warning("settings: %s is not a whole number (%r); using %d", name, value, default)
        return default
    if not low <= number <= high:
        clamped = max(low, min(high, number))
        logger.warning("settings: %s=%d is out of range; using %d", name, number, clamped)
        return clamped
    return number


def _as_bool(value: Any, *, default: bool, name: str) -> bool:
    if isinstance(value, bool):
        return value
    logger.warning("settings: %s is not true/false (%r); using %s", name, value, default)
    return default


def _as_optional_str(value: Any, *, name: str, max_chars: int = 200) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        logger.warning("settings: %s is not text (%r); ignoring", name, value)
        return None
    text = value.strip()
    if not text:
        return None
    return text[:max_chars]


def _parse_trigger(raw: Any, *, seen: set[str]) -> Trigger | None:
    """One trigger, or ``None`` if it is unusable.

    A trigger with no phrase or no payload would either never fire or fire and
    insert nothing, so it is dropped rather than kept in a broken state.
    """
    if not isinstance(raw, dict):
        return None
    phrase = _as_optional_str(raw.get("phrase"), name="trigger.phrase", max_chars=MAX_PHRASE_CHARS)
    if phrase is None:
        return None
    kind = raw.get("type", "macro")
    if kind not in TRIGGER_TYPES:
        logger.warning("settings: trigger %r has unsupported type %r; skipping", phrase, kind)
        return None
    payload = _as_optional_str(raw.get("payload"), name="trigger.payload", max_chars=MAX_PAYLOAD_CHARS)
    if kind == "macro" and payload is None:
        logger.warning("settings: macro trigger %r has no payload; skipping", phrase)
        return None

    key = normalise_phrase(phrase)
    if not key or key in seen:
        # A duplicate phrase can only ever fire once, so the second is dead
        # weight that would also make the conflict indicator lie.
        return None
    seen.add(key)

    return Trigger(
        id=_as_optional_str(raw.get("id"), name="trigger.id", max_chars=64) or _new_id(),
        phrase=phrase,
        payload=payload or "",
        type=kind,
        enabled=_as_bool(raw.get("enabled", True), default=True, name="trigger.enabled"),
        created_at=_coerce_time(raw.get("created_at")),
    )


def _coerce_time(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_app_rule(raw: Any, *, seen: set[str]) -> AppRule | None:
    if not isinstance(raw, dict):
        return None
    process = _as_optional_str(raw.get("process"), name="app_rule.process", max_chars=120)
    if process is None:
        return None
    style = raw.get("style")
    if style not in CLEANUP_STYLES:
        logger.warning("settings: app rule %r has unknown style %r; skipping", process, style)
        return None
    key = process.strip().lower()
    if key in seen:
        return None
    seen.add(key)
    return AppRule(process=process, style=style)


_KNOWN_KEYS = {
    "version",
    "hotkey",
    "audio_device",
    "language",
    "overlay_position",
    "launch_at_login",
    "history_retention_days",
    "latency_retention_days",
    "onboarding_complete",
    "context_mode",
    "remove_fillers",
    "triggers",
    "app_rules",
}


def parse(raw: Any) -> Settings:
    """Turn decoded JSON into :class:`Settings`, repairing what it can.

    Pure and total: it never raises and never returns something the rest of
    the app has to re-validate. Anything unrecognised is logged and replaced
    with the default for that one field.
    """
    if not isinstance(raw, dict):
        logger.warning("settings: file is not a JSON object; using defaults")
        return DEFAULTS

    version = raw.get("version", SCHEMA_VERSION)
    if isinstance(version, int) and version > SCHEMA_VERSION:
        logger.warning(
            "settings: file is version %d, this build understands %d; "
            "reading what it can and preserving the rest",
            version,
            SCHEMA_VERSION,
        )

    position = raw.get("overlay_position", DEFAULTS.overlay_position)
    if position not in OVERLAY_POSITIONS:
        if "overlay_position" in raw:
            logger.warning("settings: unknown overlay_position %r; using default", position)
        position = DEFAULTS.overlay_position

    context_mode = raw.get("context_mode", DEFAULTS.context_mode)
    if context_mode not in CONTEXT_MODES:
        if "context_mode" in raw:
            logger.warning("settings: unknown context_mode %r; using default", context_mode)
        context_mode = DEFAULTS.context_mode

    triggers: list[Trigger] = []
    seen_phrases: set[str] = set()
    for item in _as_list(raw.get("triggers")):
        if len(triggers) >= MAX_TRIGGERS:
            logger.warning("settings: more than %d triggers; ignoring the rest", MAX_TRIGGERS)
            break
        parsed = _parse_trigger(item, seen=seen_phrases)
        if parsed is not None:
            triggers.append(parsed)

    rules: list[AppRule] = []
    seen_processes: set[str] = set()
    for item in _as_list(raw.get("app_rules")):
        if len(rules) >= MAX_APP_RULES:
            logger.warning("settings: more than %d app rules; ignoring the rest", MAX_APP_RULES)
            break
        parsed_rule = _parse_app_rule(item, seen=seen_processes)
        if parsed_rule is not None:
            rules.append(parsed_rule)

    return Settings(
        hotkey=_as_optional_str(raw.get("hotkey"), name="hotkey", max_chars=64),
        audio_device=_as_optional_str(raw.get("audio_device"), name="audio_device"),
        language=_as_optional_str(raw.get("language"), name="language", max_chars=32),
        overlay_position=position,
        launch_at_login=_as_bool(
            raw.get("launch_at_login", DEFAULTS.launch_at_login),
            default=DEFAULTS.launch_at_login,
            name="launch_at_login",
        ),
        history_retention_days=_clamp_int(
            raw.get("history_retention_days", DEFAULTS.history_retention_days),
            default=DEFAULTS.history_retention_days,
            low=0,
            high=3650,
            name="history_retention_days",
        ),
        latency_retention_days=_clamp_int(
            raw.get("latency_retention_days", DEFAULTS.latency_retention_days),
            default=DEFAULTS.latency_retention_days,
            low=0,
            high=3650,
            name="latency_retention_days",
        ),
        onboarding_complete=_as_bool(
            raw.get("onboarding_complete", DEFAULTS.onboarding_complete),
            default=DEFAULTS.onboarding_complete,
            name="onboarding_complete",
        ),
        context_mode=context_mode,
        remove_fillers=_as_bool(
            raw.get("remove_fillers", DEFAULTS.remove_fillers),
            default=DEFAULTS.remove_fillers,
            name="remove_fillers",
        ),
        triggers=tuple(triggers),
        app_rules=tuple(rules),
        unknown={k: v for k, v in raw.items() if k not in _KNOWN_KEYS},
    )


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def to_dict(settings: Settings) -> dict[str, Any]:
    """The JSON shape written to disk. Unknown keys are merged back in."""
    payload: dict[str, Any] = {
        "version": SCHEMA_VERSION,
        "hotkey": settings.hotkey,
        "audio_device": settings.audio_device,
        "language": settings.language,
        "overlay_position": settings.overlay_position,
        "launch_at_login": settings.launch_at_login,
        "history_retention_days": settings.history_retention_days,
        "latency_retention_days": settings.latency_retention_days,
        "onboarding_complete": settings.onboarding_complete,
        "context_mode": settings.context_mode,
        "remove_fillers": settings.remove_fillers,
        "triggers": [asdict(t) for t in settings.triggers],
        "app_rules": [asdict(r) for r in settings.app_rules],
    }
    # Unknown keys never shadow a key this build owns, so a stale field from a
    # newer version cannot resurrect itself over a value the user just set.
    for key, value in settings.unknown.items():
        payload.setdefault(key, value)
    return payload


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class SettingsStore:
    """Reads and writes one ``settings.json``, safely, from any thread.

    Held by the engine for the process's lifetime. Reads are served from an
    in-memory copy so a settings lookup on the dictation path never touches
    the disk; writes go through :meth:`update`, which persists atomically and
    then swaps the cached value.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._settings = DEFAULTS
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @classmethod
    def open(cls, data_dir: Path | str) -> SettingsStore:
        """Load (or create) the settings file under ``data_dir``."""
        store = cls(Path(data_dir) / SETTINGS_FILENAME)
        store.load()
        return store

    def load(self) -> Settings:
        """Read from disk. Never raises: any failure yields the defaults.

        A file that cannot be parsed is preserved under a ``.corrupt`` suffix
        rather than overwritten, so a user who hand-edited it badly can get
        their triggers back instead of finding them silently gone.
        """
        with self._lock:
            self._loaded = True
            try:
                text = self._path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._settings = DEFAULTS
                return self._settings
            except OSError as exc:
                logger.warning("settings: could not read %s (%s); using defaults", self._path, exc)
                self._settings = DEFAULTS
                return self._settings

            try:
                raw = json.loads(text)
            except ValueError as exc:
                logger.error("settings: %s is not valid JSON (%s); using defaults", self._path, exc)
                self._quarantine(text)
                self._settings = DEFAULTS
                return self._settings

            self._settings = parse(raw)
            return self._settings

    def _quarantine(self, text: str) -> None:
        """Keep a copy of an unparseable file instead of losing its contents."""
        try:
            backup = self._path.with_suffix(f".corrupt-{int(time.time())}.json")
            backup.write_text(text, encoding="utf-8")
            logger.warning("settings: kept the unreadable file at %s", backup)
        except OSError:
            logger.debug("settings: could not preserve the corrupt file", exc_info=True)

    def get(self) -> Settings:
        """The current settings. Cheap; safe to call on any thread."""
        with self._lock:
            if not self._loaded:
                self.load()
            return self._settings

    def update(self, **changes: Any) -> Settings:
        """Apply field changes, persist, and return the new settings.

        Raises :class:`ValueError` for a field this store does not own, so a
        typo in a route handler fails loudly here rather than being written to
        disk and silently ignored on the next load.
        """
        with self._lock:
            current = self.get()
            unknown_fields = set(changes) - {f for f in Settings.__slots__ if f != "unknown"}
            if unknown_fields:
                raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown_fields))}")
            candidate = replace(current, **changes)
            # Round-trip through the validator so a value arriving from the UI
            # is held to exactly the same rules as one read off disk.
            validated = parse(to_dict(candidate))
            self._save(validated)
            self._settings = validated
            return validated

    def _save(self, settings: Settings) -> None:
        """Atomic write: temp file in the same directory, then os.replace.

        Same directory matters -- ``os.replace`` is only atomic within a
        filesystem, and the data directory and the system temp directory are
        routinely on different volumes on Windows.
        """
        payload = json.dumps(to_dict(settings), indent=2, ensure_ascii=False)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=".settings-",
                suffix=".tmp",
                delete=False,
            )
            try:
                with handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(handle.name, self._path)
            except BaseException:
                # Leaving a stray .tmp behind would accumulate one file per
                # failed save, so clean up on every failure path including
                # KeyboardInterrupt.
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
                raise
        except OSError as exc:
            # A settings write that fails must not take the app down: the user
            # keeps the in-memory value for this session and is told why.
            logger.error("settings: could not write %s: %s", self._path, exc)
            raise SettingsWriteError(f"could not save settings: {exc}") from exc


class SettingsWriteError(Exception):
    """Raised when settings could not be persisted. Not fatal to the app."""


# ---------------------------------------------------------------------------
# Overlay onto flow.config
# ---------------------------------------------------------------------------


def apply_to_config(config: Any, settings: Settings) -> Any:
    """Return ``config`` with the user's overrides layered on top.

    The single place settings and config meet. Only fields the user can
    actually edit are touched; everything else on the config object is passed
    through untouched, so a developer's ``config.toml`` keeps working for the
    dozens of values that have no UI.
    """
    audio = config.audio
    if settings.audio_device is not None:
        audio = replace(audio, device=settings.audio_device)

    hotkey = config.hotkey
    if settings.hotkey is not None:
        hotkey = replace(hotkey, key=settings.hotkey)

    groq = config.groq
    if settings.language is not None:
        groq = replace(groq, language=settings.language)

    history = replace(config.history, retention_days=settings.history_retention_days)
    latency = replace(config.latency, retention_days=settings.latency_retention_days)

    return replace(
        config,
        audio=audio,
        hotkey=hotkey,
        groq=groq,
        history=history,
        latency=latency,
    )


__all__ = [
    "CLEANUP_STYLES",
    "CONTEXT_MODES",
    "DEFAULTS",
    "MAX_PAYLOAD_CHARS",
    "MAX_PHRASE_CHARS",
    "MAX_TRIGGERS",
    "OVERLAY_POSITIONS",
    "SCHEMA_VERSION",
    "SETTINGS_FILENAME",
    "TRIGGER_TYPES",
    "AppRule",
    "Settings",
    "SettingsStore",
    "SettingsWriteError",
    "Trigger",
    "apply_to_config",
    "normalise_phrase",
    "parse",
    "to_dict",
]
