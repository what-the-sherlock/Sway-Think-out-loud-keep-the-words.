"""Configuration loading.

Three layers, highest precedence first:

1. Environment variables — ``FLOW_<SECTION>_<FIELD>`` (e.g. ``FLOW_LATENCY_ENABLED``),
   plus ``FLOW_DATA_DIR`` and ``GROQ_API_KEY``.
2. A TOML file — ``$FLOW_CONFIG``, else ``./config.toml``, else
   ``<user config dir>/config.toml``.
3. Dataclass defaults.

Values are read once at startup into a frozen :class:`Config`; nothing in the
app mutates configuration afterwards.

Unknown keys in the TOML file are a hard error. A silently ignored typo in a
config file is the kind of bug that costs an hour, and the cost of strictness
is one clear message.
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from sway import APP_NAME
from sway.errors import ConfigError
from sway.platform import get_platform

_ENV_PREFIX = "SWAY_"
#: Honoured for continuity across the Flow -> Sway rename. ``SWAY_`` always
#: wins; a ``FLOW_`` variable is used only when its ``SWAY_`` counterpart is
#: absent, and using one is logged once so the deprecation is visible rather
#: than silent.
_LEGACY_ENV_PREFIX = "FLOW_"

#: Set once the first legacy variable is seen, so the warning is not repeated
#: for every field on every config load.
_warned_legacy_env = False


def _env_lookup(env: Mapping[str, str], suffix: str) -> str | None:
    """Read ``SWAY_<suffix>``, falling back to ``FLOW_<suffix>``."""
    value = env.get(f"{_ENV_PREFIX}{suffix}")
    if value is not None:
        return value
    legacy = env.get(f"{_LEGACY_ENV_PREFIX}{suffix}")
    if legacy is not None:
        global _warned_legacy_env
        if not _warned_legacy_env:
            _warned_legacy_env = True
            logging.getLogger(__name__).warning(
                "%s%s is deprecated; rename it to %s%s",
                _LEGACY_ENV_PREFIX,
                suffix,
                _ENV_PREFIX,
                suffix,
            )
    return legacy
_CONFIG_FILENAME = "config.toml"
_ENV_FILENAME = ".env"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

#: Kept in sync with flow.hotkey.KNOWN_KEYS (checked by a test) rather than
#: imported from it, so validating a config file never needs pynput imported.
_VALID_HOTKEYS = frozenset(
    {
        "ctrl_r",
        "ctrl_l",
        "alt_r",
        "alt_l",
        "shift_r",
        "shift_l",
        "cmd",
        "caps_lock",
        "f13",
        "f14",
        "f15",
        "f16",
        "f17",
        "f18",
    }
)

#: Kept in sync with flow.hotkey.CHORD_MODIFIERS's names (checked by a test)
#: rather than imported from it, for the same pynput-free reason
#: _VALID_HOTKEYS exists.
_VALID_CHORD_MODIFIERS = frozenset({"ctrl", "alt", "shift", "cmd"})

#: Kept in sync with flow.hotkey.CHORD_KEYS's names (checked by a test).
_VALID_CHORD_KEY_NAMES = frozenset(
    {f"f{n}" for n in range(1, 21)}
    | {"space", "enter", "tab", "backspace", "delete", "home", "end", "insert"}
)

#: Kept in sync with flow.hotkey._RESERVED_CHORDS's keys (checked by a test).
_RESERVED_CHORD_SHAPES = frozenset({"ctrl+c", "ctrl+v", "ctrl+x", "ctrl+alt+delete", "cmd+l"})


def _is_valid_chord_shape(shortcut: str) -> bool:
    """Pynput-free mirror of the "modifiers + key" chord acceptance rules in
    flow.hotkey.parse_binding (config.toml can only express this one binding
    shape -- the richer ones, like a modifier-only combo or a mouse button,
    are settings.json/UI-only; see sway.hotkey.effective_bindings).

    Validates a chord string read from config.toml without importing
    pynput, for the same reason _VALID_HOTKEYS exists instead of importing
    KNOWN_KEYS. A test checks this never drifts from parse_binding's actual
    behaviour for this shape.
    """
    parts = [p.strip().lower() for p in shortcut.split("+") if p.strip()]
    if not parts:
        return False
    *modifier_names, key = parts
    if not modifier_names or any(m not in _VALID_CHORD_MODIFIERS for m in modifier_names):
        return False
    if key in _VALID_CHORD_MODIFIERS:
        return False
    if key not in _VALID_CHORD_KEY_NAMES and not (len(key) == 1 and key.isalnum()):
        return False
    order = [m for m in ("ctrl", "alt", "shift", "cmd") if m in modifier_names]
    canonical = "+".join([*order, key])
    return canonical not in _RESERVED_CHORD_SHAPES


#: webrtcvad-supported sample rates and frame durations (flow.audio.vad).
_VALID_VAD_SAMPLE_RATES = (8000, 16000, 32000, 48000)
_VALID_VAD_FRAME_MS = (10, 20, 30)
#: Values Groq's chat-completions endpoint accepts for reasoning_effort on
#: the models that support it. Blank is also valid and sends the field at all.
_VALID_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "default"})
#: [overlay].reduced_motion. "auto" defers to the OS accessibility setting.
_VALID_REDUCED_MOTION = frozenset({"auto", "on", "off"})


class Secret:
    """A string that refuses to appear in logs, reprs, or tracebacks.

    API keys end up inside exception messages and debug dumps far too easily.
    Wrapping them makes leaking one an explicit ``.reveal()`` call.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str = "") -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(***)" if self._value else "Secret(unset)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Console and rotating-file logging."""

    level: str = "INFO"
    file_enabled: bool = True
    file_max_bytes: int = 1_000_000
    file_backup_count: int = 3


@dataclass(frozen=True, slots=True)
class LatencyConfig:
    """Stage-latency tracking (see :mod:`flow.latency`)."""

    enabled: bool = True
    #: Defaults to ``<data_dir>/latency.db``. See :attr:`Config.latency_db_path`.
    db_path: Path | None = None
    #: Runs older than this are pruned at startup. 0 disables pruning.
    retention_days: int = 30
    #: Bounded so a stalled disk can never grow memory without limit.
    queue_size: int = 1024
    #: Rows written per transaction by the background writer.
    batch_size: int = 64


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Microphone capture. 16kHz mono is a hard CLAUDE.md choice, not a UI knob."""

    #: PortAudio device name substring, or None for the system default.
    device: str | None = None
    sample_rate: int = 16000
    channels: int = 1
    #: Ring buffer capacity. A hold longer than this drops the oldest audio.
    ring_seconds: int = 60
    #: 0 lets PortAudio choose the most efficient block size.
    blocksize_frames: int = 0
    #: webrtcvad aggressiveness, 0 (least) to 3 (most) aggressive about
    #: classifying a frame as non-speech.
    vad_mode: int = 2
    vad_frame_ms: int = 30
    #: Speech-adjacent silence kept on each side of the trimmed clip.
    vad_padding_ms: int = 200
    #: How much already-captured audio to include from *before* the hotkey
    #: press. The mic stream is always open and always writing to the ring
    #: buffer (CLAUDE.md), so this audio already exists and costs nothing to
    #: include -- and it is what stops the first syllable being clipped when
    #: a user starts speaking as they press rather than strictly after. The
    #: VAD trims the silence back off, so a clean press just gets a slightly
    #: longer silent lead-in that never reaches Groq.
    preroll_ms: int = 300


@dataclass(frozen=True, slots=True)
class HotkeyConfig:
    """Hold-to-dictate key. See flow.hotkey.KNOWN_KEYS for valid names."""

    key: str = "ctrl_r"
    #: Holds shorter than this are treated as an accidental tap and ignored.
    min_hold_ms: int = 200
    #: Hold this key instead, with text selected in the focused app, to apply
    #: a spoken instruction to the selection (Phase 5, e.g. "make this bullet
    #: points") -- see flow.commands. None disables voice commands entirely.
    #: Shift rather than Ctrl/Alt by default: those are common menu/accelerator
    #: modifiers in many apps and holding one risks disturbing the selection.
    command_key: str | None = "shift_r"
    #: Optional alternate binding for dictation: a modifier chord (see
    #: flow.hotkey.parse_binding) that starts/stops a session with a tap
    #: instead of being held. None means dictation only answers to `key`.
    key_chord: str | None = None
    #: Optional alternate chord for the voice-command hotkey, same shape as
    #: key_chord.
    command_key_chord: str | None = None
    #: Hands-free dictation (Phase 6): tap once to start, tap again to stop,
    #: no holding. Bind either a single key (tapped, or double-tapped if
    #: hands_free_double_tap is set) or a chord -- at most one binding style
    #: is expected at a time, enforced by validate().
    hands_free_key: str | None = None
    hands_free_chord: str | None = None
    #: When true, hands_free_key must be double-tapped within a short window
    #: rather than tapped once, freeing a single tap of that key for its
    #: ordinary use elsewhere.
    hands_free_double_tap: bool = False
    #: Safety ceiling: a hands-free session force-stops after this long even
    #: if its stop tap never arrives, so the mic can never be left hot
    #: indefinitely.
    hands_free_max_seconds: int = 120


@dataclass(frozen=True, slots=True)
class GroqConfig:
    """Groq transcription API. See flow.groq_client."""

    base_url: str = "https://api.groq.com"
    timeout_s: float = 15.0
    connect_timeout_s: float = 5.0
    #: Retries apply only to transient network errors and 5xx; see flow.groq_client.
    max_retries: int = 1
    #: ISO-639-1 hint (e.g. "en"); None lets Whisper auto-detect.
    language: str | None = None
    #: The default speech-to-text model. Spelled out rather than imported
    #: from sway.groq_client: this module is deliberately dependency-light
    #: (it is loaded before anything else and must not drag in httpx), and
    #: sway.groq_client already imports from here. Kept in step by
    #: tests/test_config.py.
    stt_model: str = "whisper-large-v3-turbo"
    #: Used instead of stt_model for languages whose transcription quality
    #: materially improves on the full model -- see
    #: sway.languages.Language.needs_accurate_stt and groq_client's
    #: WHISPER_ACCURATE_MODEL for the measured trade (+140ms median).
    stt_accurate_model: str = "whisper-large-v3"


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    """LLM cleanup pass over the raw transcript. See flow.cleanup (Phase 2).

    Network settings (timeout, retries, ...) come from [groq]: cleanup shares
    the same persistent httpx client as transcription, per CLAUDE.md's
    one-client rule, rather than duplicating them here.
    """

    enabled: bool = True
    #: Groq retired llama-3.1-8b-instant (it now 404s, which silently took
    #: the entire cleanup stage down); see flow.groq_client.CLEANUP_MODEL.
    model: str = "openai/gpt-oss-20b"
    #: 0.0-2.0, the chat-completions sampling temperature. 0.0: a cleanup
    #: rewrite should be deterministic, and at 0.2 the same transcript was
    #: observed dropping a hedge word ("we should probably send this" ->
    #: "we should send this") on some runs and not others.
    temperature: float = 0.0
    #: Transcripts shorter than this, with no filler markers, skip the LLM.
    skip_min_words: int = 8
    #: Passed through to models that support it (the gpt-oss family). "low"
    #: measured ~2x faster than the model default for identical rewrites --
    #: cleanup is the largest stage in the post-release budget. Blank sends
    #: no reasoning_effort at all.
    reasoning_effort: str = "low"
    #: Ceiling on the rewrite length, so a model that ignores the prompt and
    #: starts writing an essay cannot run away with the latency budget.
    max_output_tokens: int = 2048
    #: Skip the LLM when Whisper itself was unsure of the transcript: the
    #: rewrite would be guessing from the same shaky input, and a confident-
    #: sounding rewrite of a bad transcript is worse than the bad transcript.
    skip_when_unconfident: bool = True


@dataclass(frozen=True, slots=True)
class DictionaryConfig:
    """Personal dictionary injected into Whisper's prompt. See flow.dictionary
    (Phase 3).
    """

    enabled: bool = True
    #: Whisper's prompt is itself capped at roughly 224 tokens by the model;
    #: this caps how many characters of the dictionary we send, so a large
    #: dictionary degrades gracefully instead of being silently dropped.
    max_prompt_chars: int = 200


@dataclass(frozen=True, slots=True)
class StreamingConfig:
    """Phase 4: overlapping chunked transcription while the hotkey is held.

    While the user keeps talking, ``chunk_seconds``-long windows of audio are
    transcribed in the background every ``chunk_seconds - overlap_seconds``,
    so at key release only the short tail since the last chunk needs a fresh
    request. See flow.streaming. Falls back to a single whole-clip request
    (Phase 1 behavior) whenever a chunk request fails or the hold was shorter
    than one chunk.

    Off by default, on measured evidence: whisper-large-v3-turbo on Groq
    returns in ~190ms whether the clip is 2 seconds or 23 (p50 191ms, p95
    235ms over the project's fixture set), because the request is dominated
    by round trip and model overhead rather than audio length. There is
    therefore almost no tail latency for chunking to remove, while chunking
    costs 2-4x the audio requests against a 2000/day free-tier budget and
    adds chunk-boundary stitching risk to every long dictation. Turn it on
    only if a measurement on your own connection shows transcription time
    actually growing with clip length.
    """

    enabled: bool = False
    chunk_seconds: float = 5.0
    #: Shared between consecutive chunks (and the final tail) so the boundary
    #: dedup in flow.streaming.stitch_transcripts has real overlap to find.
    overlap_seconds: float = 1.0
    #: How often the background worker checks whether a chunk is due.
    poll_interval_s: float = 0.2


@dataclass(frozen=True, slots=True)
class CommandsConfig:
    """Phase 5: voice commands applied to selected text. See flow.commands
    and flow.selection. Enabled by [hotkey].command_key being set.
    """

    model: str = "openai/gpt-oss-20b"
    #: 0.0-2.0. Low, like [cleanup].temperature: a rewrite should follow the
    #: instruction closely rather than improvise.
    temperature: float = 0.2
    #: How long to wait for the Ctrl+C selection-copy round trip before
    #: concluding nothing was selected.
    selection_timeout_s: float = 0.3


@dataclass(frozen=True, slots=True)
class HistoryConfig:
    """The searchable dictation/command history. See flow.history.

    Default: <data_dir>/history.db. See :attr:`Config.history_db_path`.
    """

    enabled: bool = True
    #: Entries older than this are pruned at startup. 0 disables pruning.
    retention_days: int = 90


@dataclass(frozen=True, slots=True)
class AnalyticsConfig:
    """The productivity numbers the dashboard reports. See flow.analytics.

    ``typing_wpm`` is an *assumption*, not a measurement: it is the baseline
    the time-saved estimate compares dictation against. It lives in config
    precisely so the user can set it to their own typing speed, and anything
    derived from it must be labelled an estimate in the UI.
    """

    #: Baseline typing speed for the time-saved estimate, in words per minute.
    typing_wpm: float = 45.0


@dataclass(frozen=True, slots=True)
class TrayConfig:
    """The system tray icon and menu (Phase 5). See flow.tray.

    Best-effort: if the tray backend cannot be started (no system tray
    available, the optional dependency failed to import), Sway logs a warning
    and keeps running without one rather than failing to start.
    """

    enabled: bool = True


@dataclass(frozen=True, slots=True)
class PasteConfig:
    """Clipboard save/restore timing around the paste. See flow.inject."""

    restore_delay_ms: int = 150


@dataclass(frozen=True, slots=True)
class OverlayConfig:
    """The always-on-top pill window. See flow.overlay."""

    enabled: bool = True
    #: Whether the bar's activity orb animates. ``"auto"`` follows the OS
    #: reduce-motion setting (on Windows, Settings > Accessibility > Visual
    #: effects > Animation effects, which is the same switch Edge maps
    #: ``prefers-reduced-motion`` onto); ``"on"`` forces the orb to hold a
    #: single frame; ``"off"`` forces it to animate regardless.
    #:
    #: The name reads the way the accessibility setting does, so ``"on"``
    #: means reduced motion is on -- i.e. less movement, not more.
    reduced_motion: str = "auto"


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved, immutable application configuration."""

    data_dir: Path
    groq_api_key: Secret
    logging: LoggingConfig
    latency: LatencyConfig
    audio: AudioConfig
    hotkey: HotkeyConfig
    groq: GroqConfig
    cleanup: CleanupConfig
    dictionary: DictionaryConfig
    streaming: StreamingConfig
    commands: CommandsConfig
    history: HistoryConfig
    analytics: AnalyticsConfig
    tray: TrayConfig
    paste: PasteConfig
    overlay: OverlayConfig
    #: The TOML file this config was loaded from, if any. Diagnostics only.
    source_file: Path | None = None

    @property
    def latency_db_path(self) -> Path:
        return self.latency.db_path or (self.data_dir / "latency.db")

    @property
    def dictionary_db_path(self) -> Path:
        return self.data_dir / "dictionary.db"

    @property
    def history_db_path(self) -> Path:
        return self.data_dir / "history.db"

    @property
    def log_file_path(self) -> Path:
        return self.data_dir / "sway.log"

    def redacted_dict(self) -> dict[str, Any]:
        """A plain dict safe to print or log."""
        sections = (
            ("logging", LoggingConfig, self.logging),
            ("latency", LatencyConfig, self.latency),
            ("audio", AudioConfig, self.audio),
            ("hotkey", HotkeyConfig, self.hotkey),
            ("groq", GroqConfig, self.groq),
            ("cleanup", CleanupConfig, self.cleanup),
            ("dictionary", DictionaryConfig, self.dictionary),
            ("streaming", StreamingConfig, self.streaming),
            ("commands", CommandsConfig, self.commands),
            ("history", HistoryConfig, self.history),
            ("analytics", AnalyticsConfig, self.analytics),
            ("tray", TrayConfig, self.tray),
            ("paste", PasteConfig, self.paste),
            ("overlay", OverlayConfig, self.overlay),
        )
        result: dict[str, Any] = {
            "data_dir": str(self.data_dir),
            "source_file": str(self.source_file) if self.source_file else None,
            "groq_api_key": "set" if self.groq_api_key else "unset",
            "latency_db_path": str(self.latency_db_path),
            "dictionary_db_path": str(self.dictionary_db_path),
            "history_db_path": str(self.history_db_path),
            "log_file_path": str(self.log_file_path),
        }
        for name, cls, instance in sections:
            result[name] = {f.name: _plain(getattr(instance, f.name)) for f in fields(cls)}
        return result


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, Path) else value


# --------------------------------------------------------------------------
# .env parsing
# --------------------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines from a ``.env`` file.

    Supports comments, blank lines, a leading ``export``, and single- or
    double-quoted values. Deliberately does *not* do shell interpolation —
    a 30-line parser beats a dependency, and interpolation is the part that
    makes .env parsers surprising.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue  # Not an assignment; ignore rather than fail on stray text.
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            # Strip trailing inline comments only on unquoted values.
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        result[key] = value
    return result


def load_env_file(path: Path, env: dict[str, str]) -> None:
    """Merge ``path`` into ``env`` without overriding real environment values.

    A variable exported in the shell always wins over the file, which is what
    people expect when they run ``GROQ_API_KEY=... flow demo``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    for key, value in parse_env_file(text).items():
        env.setdefault(key, value)


# --------------------------------------------------------------------------
# Coercion
# --------------------------------------------------------------------------


def _coerce(target: Any, raw: Any, source: str) -> Any:
    """Convert a TOML value or environment string to ``target``."""
    origin = get_origin(target)
    if origin in (Union, UnionType):
        args = [a for a in get_args(target) if a is not type(None)]
        if len(args) != 1:
            raise ConfigError(f"{source}: unsupported option type {target!r}")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        return _coerce(args[0], raw, source)

    if target is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ConfigError(f"{source}: expected a boolean, got {raw!r}")

    if target is int:
        if isinstance(raw, bool):
            raise ConfigError(f"{source}: expected an integer, got a boolean")
        try:
            return int(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(f"{source}: expected an integer, got {raw!r}") from exc

    if target is float:
        if isinstance(raw, bool):
            raise ConfigError(f"{source}: expected a number, got a boolean")
        try:
            return float(str(raw).strip())
        except ValueError as exc:
            raise ConfigError(f"{source}: expected a number, got {raw!r}") from exc

    if target is Path:
        text = str(raw).strip()
        if not text:
            raise ConfigError(f"{source}: expected a path, got an empty value")
        return Path(text).expanduser()

    if target is str:
        return str(raw)

    raise ConfigError(f"{source}: unsupported option type {target!r}")


def _build_section(cls: type, table: Mapping[str, Any], env: Mapping[str, str], name: str):
    """Instantiate a config dataclass from TOML values then env overrides."""
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    values: dict[str, Any] = {}

    for key, raw in table.items():
        if key not in known:
            options = ", ".join(sorted(known))
            raise ConfigError(f"unknown option [{name}].{key}; valid options: {options}")
        values[key] = _coerce(hints[key], raw, f"[{name}].{key}")

    for field_name in sorted(known):
        env_suffix = f"{name.upper()}_{field_name.upper()}"
        raw_env = _env_lookup(env, env_suffix)
        if raw_env is not None:
            values[field_name] = _coerce(
                hints[field_name], raw_env, f"{_ENV_PREFIX}{env_suffix}"
            )

    return cls(**values)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def bundle_dir() -> Path | None:
    """The directory holding the executable in a frozen (PyInstaller) build,
    else ``None`` when running from source.

    A packaged copy of Sway is handed to someone as a folder they unzip
    somewhere and double-click, and the ``.env`` holding their API key
    naturally lives in that same folder. Searching only the *working*
    directory would find it when launched from Explorer (which sets the
    working directory to the executable's own) but not from a Start-menu
    shortcut, a pinned taskbar icon, or a scheduled task -- all of which set
    it to something else entirely, making the app mysteriously forget its
    configuration depending on how it was started.
    """
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable).resolve().parent


def _config_search_paths() -> tuple[Path, ...]:
    """Where ``config.toml`` is looked for, in precedence order."""
    candidates = [Path.cwd()]
    packaged = bundle_dir()
    if packaged is not None and packaged != candidates[0]:
        candidates.append(packaged)
    candidates.append(get_platform().user_config_dir(APP_NAME))
    return tuple(candidates)


def _find_config_file(explicit: Path | None, env: Mapping[str, str]) -> Path | None:
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"config file not found: {explicit}")
        return explicit

    from_env = _env_lookup(env, "CONFIG")
    if from_env:
        candidate = Path(from_env).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate} (from {_ENV_PREFIX}CONFIG)")
        return candidate

    for directory in _config_search_paths():
        candidate = directory / _CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc


def _validate(config: Config) -> None:
    if config.logging.level.upper() not in logging.getLevelNamesMapping():
        raise ConfigError(f"[logging].level: unknown level {config.logging.level!r}")
    if config.logging.file_max_bytes < 0:
        raise ConfigError("[logging].file_max_bytes must be >= 0")
    if config.logging.file_backup_count < 0:
        raise ConfigError("[logging].file_backup_count must be >= 0")
    if config.latency.retention_days < 0:
        raise ConfigError("[latency].retention_days must be >= 0")
    if config.analytics.typing_wpm <= 0:
        raise ConfigError("[analytics].typing_wpm must be > 0")
    if config.latency.queue_size < 1:
        raise ConfigError("[latency].queue_size must be >= 1")
    if config.latency.batch_size < 1:
        raise ConfigError("[latency].batch_size must be >= 1")

    if config.audio.channels != 1:
        raise ConfigError("[audio].channels must be 1 (mono only)")
    if config.audio.sample_rate not in _VALID_VAD_SAMPLE_RATES:
        raise ConfigError(f"[audio].sample_rate must be one of {_VALID_VAD_SAMPLE_RATES}")
    if config.audio.ring_seconds < 1:
        raise ConfigError("[audio].ring_seconds must be >= 1")
    if config.audio.blocksize_frames < 0:
        raise ConfigError("[audio].blocksize_frames must be >= 0")
    if not 0 <= config.audio.vad_mode <= 3:
        raise ConfigError("[audio].vad_mode must be between 0 and 3")
    if config.audio.vad_frame_ms not in _VALID_VAD_FRAME_MS:
        raise ConfigError(f"[audio].vad_frame_ms must be one of {_VALID_VAD_FRAME_MS}")
    if config.audio.vad_padding_ms < 0:
        raise ConfigError("[audio].vad_padding_ms must be >= 0")
    if config.audio.preroll_ms < 0:
        raise ConfigError("[audio].preroll_ms must be >= 0")
    if config.audio.preroll_ms >= config.audio.ring_seconds * 1000:
        raise ConfigError("[audio].preroll_ms must be less than [audio].ring_seconds")

    if config.overlay.reduced_motion not in _VALID_REDUCED_MOTION:
        raise ConfigError(
            f"[overlay].reduced_motion must be one of {sorted(_VALID_REDUCED_MOTION)}"
        )

    if config.hotkey.key not in _VALID_HOTKEYS:
        raise ConfigError(f"[hotkey].key must be one of {sorted(_VALID_HOTKEYS)}")
    if config.hotkey.min_hold_ms < 0:
        raise ConfigError("[hotkey].min_hold_ms must be >= 0")
    if config.hotkey.command_key is not None and config.hotkey.command_key not in _VALID_HOTKEYS:
        raise ConfigError(f"[hotkey].command_key must be one of {sorted(_VALID_HOTKEYS)}")
    if config.hotkey.hands_free_key is not None and config.hotkey.hands_free_key not in _VALID_HOTKEYS:
        raise ConfigError(f"[hotkey].hands_free_key must be one of {sorted(_VALID_HOTKEYS)}")
    if config.hotkey.hands_free_max_seconds <= 0:
        raise ConfigError("[hotkey].hands_free_max_seconds must be > 0")
    if config.hotkey.hands_free_key is not None and config.hotkey.hands_free_chord is not None:
        raise ConfigError(
            "[hotkey].hands_free_key and [hotkey].hands_free_chord are mutually exclusive "
            "-- pick one binding style for hands-free"
        )

    # Every configured *hold-key* binding must be pairwise distinct: two
    # hold-shaped listeners on the same physical key is genuinely broken
    # (both fire on every press). A hold key and a chord can never collide
    # with each other -- a chord always has a terminal key, a hold binding
    # never does -- so chords are validated separately below. A double-tap
    # hands-free key is exempted from this set: it needs two taps to mean
    # anything, so sharing a key with a hold binding is a different (if
    # confusing) gesture, not an outright conflict.
    hold_bindings: dict[str, str] = {"key": config.hotkey.key}
    if config.hotkey.command_key is not None:
        hold_bindings["command_key"] = config.hotkey.command_key
    if config.hotkey.hands_free_key is not None and not config.hotkey.hands_free_double_tap:
        hold_bindings["hands_free_key"] = config.hotkey.hands_free_key
    seen: dict[str, str] = {}
    for field_name, value in hold_bindings.items():
        if value in seen:
            raise ConfigError(
                f"[hotkey].{field_name} must differ from [hotkey].{seen[value]} (both are {value!r})"
            )
        seen[value] = field_name

    chord_bindings: dict[str, str] = {}
    for field_name, value in (
        ("key_chord", config.hotkey.key_chord),
        ("command_key_chord", config.hotkey.command_key_chord),
        ("hands_free_chord", config.hotkey.hands_free_chord),
    ):
        if value is None:
            continue
        if not _is_valid_chord_shape(value):
            raise ConfigError(f"[hotkey].{field_name} is not a usable shortcut: {value!r}")
        if value in chord_bindings.values():
            other = next(k for k, v in chord_bindings.items() if v == value)
            raise ConfigError(
                f"[hotkey].{field_name} must differ from [hotkey].{other} (both are {value!r})"
            )
        chord_bindings[field_name] = value

    if config.groq.timeout_s <= 0:
        raise ConfigError("[groq].timeout_s must be > 0")
    if config.groq.connect_timeout_s <= 0:
        raise ConfigError("[groq].connect_timeout_s must be > 0")
    if config.groq.max_retries < 0:
        raise ConfigError("[groq].max_retries must be >= 0")
    if config.groq.language is not None and not config.groq.language.strip():
        raise ConfigError("[groq].language must not be blank")
    if not config.groq.stt_model.strip():
        raise ConfigError("[groq].stt_model must not be blank")
    if not config.groq.stt_accurate_model.strip():
        raise ConfigError("[groq].stt_accurate_model must not be blank")

    if not config.cleanup.model.strip():
        raise ConfigError("[cleanup].model must not be blank")
    if not 0.0 <= config.cleanup.temperature <= 2.0:
        raise ConfigError("[cleanup].temperature must be between 0.0 and 2.0")
    if config.cleanup.skip_min_words < 0:
        raise ConfigError("[cleanup].skip_min_words must be >= 0")
    if config.cleanup.reasoning_effort and config.cleanup.reasoning_effort not in _VALID_REASONING_EFFORTS:
        raise ConfigError(
            f"[cleanup].reasoning_effort must be one of {sorted(_VALID_REASONING_EFFORTS)} "
            "(or blank to send none)"
        )
    if config.cleanup.max_output_tokens < 0:
        raise ConfigError("[cleanup].max_output_tokens must be >= 0")

    if config.dictionary.max_prompt_chars < 0:
        raise ConfigError("[dictionary].max_prompt_chars must be >= 0")

    if config.streaming.chunk_seconds <= 0:
        raise ConfigError("[streaming].chunk_seconds must be > 0")
    if config.streaming.overlap_seconds < 0:
        raise ConfigError("[streaming].overlap_seconds must be >= 0")
    if config.streaming.overlap_seconds >= config.streaming.chunk_seconds:
        raise ConfigError("[streaming].overlap_seconds must be less than chunk_seconds")
    if config.streaming.poll_interval_s <= 0:
        raise ConfigError("[streaming].poll_interval_s must be > 0")

    if not config.commands.model.strip():
        raise ConfigError("[commands].model must not be blank")
    if not 0.0 <= config.commands.temperature <= 2.0:
        raise ConfigError("[commands].temperature must be between 0.0 and 2.0")
    if config.commands.selection_timeout_s <= 0:
        raise ConfigError("[commands].selection_timeout_s must be > 0")

    if config.history.retention_days < 0:
        raise ConfigError("[history].retention_days must be >= 0")

    if config.paste.restore_delay_ms < 0:
        raise ConfigError("[paste].restore_delay_ms must be >= 0")


def _resolve_api_key(merged_env: Mapping[str, str]) -> str:
    """The Groq key, from the credential store or the environment.

    Resolution order lives in :mod:`flow.credentials` so there is exactly one
    place that answers "where does the key come from". Kept behind a function
    rather than inlined because the import is deliberately lazy: ``keyring``
    touches the Windows vault, and ``flow config`` on a machine with a broken
    credential store must still print a configuration.

    Falls back to the plain environment lookup this function replaced if the
    credentials module cannot be used at all, so a key in ``.env`` keeps
    working no matter what.
    """
    try:
        from sway import credentials

        key, _source = credentials.resolve_key(merged_env)
        return key
    except Exception:  # pragma: no cover - defensive
        logging.getLogger(__name__).debug(
            "falling back to the plain environment for the API key", exc_info=True
        )
        return merged_env.get("GROQ_API_KEY", "").strip()


def load_config(
    *,
    config_file: Path | None = None,
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> Config:
    """Load, merge, and validate configuration.

    Args:
        config_file: Explicit TOML path; overrides the search order.
        env: Environment mapping to read (defaults to ``os.environ``). Injectable
            so tests never touch process state.
        env_file: ``.env`` path (defaults to ``./.env``). Values from it never
            override real environment variables.
    """
    merged_env: dict[str, str] = dict(os.environ if env is None else env)
    if env_file is not None:
        load_env_file(env_file, merged_env)
    else:
        # Working directory first, then the executable's own directory in a
        # packaged build (see bundle_dir). load_env_file uses setdefault, so
        # the first file to define a key wins and real environment variables
        # still beat both.
        load_env_file(Path.cwd() / _ENV_FILENAME, merged_env)
        packaged = bundle_dir()
        if packaged is not None:
            load_env_file(packaged / _ENV_FILENAME, merged_env)

    path = _find_config_file(config_file, merged_env)
    table = _read_toml(path) if path is not None else {}

    top_level_keys = {"data_dir"}
    section_specs = {
        "logging": LoggingConfig,
        "latency": LatencyConfig,
        "audio": AudioConfig,
        "hotkey": HotkeyConfig,
        "groq": GroqConfig,
        "cleanup": CleanupConfig,
        "dictionary": DictionaryConfig,
        "streaming": StreamingConfig,
        "commands": CommandsConfig,
        "history": HistoryConfig,
        "analytics": AnalyticsConfig,
        "tray": TrayConfig,
        "paste": PasteConfig,
        "overlay": OverlayConfig,
    }
    for key in table:
        if key not in top_level_keys and key not in section_specs:
            valid = ", ".join(sorted(top_level_keys | set(section_specs)))
            raise ConfigError(f"unknown top-level key {key!r} in {path}; valid keys: {valid}")

    for name in section_specs:
        value = table.get(name)
        if value is not None and not isinstance(value, dict):
            raise ConfigError(f"[{name}] must be a table in {path}")

    raw_data_dir = _env_lookup(merged_env, "DATA_DIR") or table.get("data_dir")
    data_dir = (
        _coerce(Path, raw_data_dir, f"{_ENV_PREFIX}DATA_DIR")
        if raw_data_dir
        else get_platform().user_data_dir(APP_NAME)
    )

    config = Config(
        data_dir=data_dir.resolve(),
        groq_api_key=Secret(_resolve_api_key(merged_env)),
        logging=_build_section(LoggingConfig, table.get("logging") or {}, merged_env, "logging"),
        latency=_build_section(LatencyConfig, table.get("latency") or {}, merged_env, "latency"),
        audio=_build_section(AudioConfig, table.get("audio") or {}, merged_env, "audio"),
        hotkey=_build_section(HotkeyConfig, table.get("hotkey") or {}, merged_env, "hotkey"),
        groq=_build_section(GroqConfig, table.get("groq") or {}, merged_env, "groq"),
        cleanup=_build_section(CleanupConfig, table.get("cleanup") or {}, merged_env, "cleanup"),
        dictionary=_build_section(
            DictionaryConfig, table.get("dictionary") or {}, merged_env, "dictionary"
        ),
        streaming=_build_section(
            StreamingConfig, table.get("streaming") or {}, merged_env, "streaming"
        ),
        commands=_build_section(CommandsConfig, table.get("commands") or {}, merged_env, "commands"),
        history=_build_section(HistoryConfig, table.get("history") or {}, merged_env, "history"),
        analytics=_build_section(
            AnalyticsConfig, table.get("analytics") or {}, merged_env, "analytics"
        ),
        tray=_build_section(TrayConfig, table.get("tray") or {}, merged_env, "tray"),
        paste=_build_section(PasteConfig, table.get("paste") or {}, merged_env, "paste"),
        overlay=_build_section(OverlayConfig, table.get("overlay") or {}, merged_env, "overlay"),
        source_file=path,
    )
    _validate(config)
    return config


__all__ = [
    "AnalyticsConfig",
    "AudioConfig",
    "CleanupConfig",
    "CommandsConfig",
    "Config",
    "DictionaryConfig",
    "GroqConfig",
    "HistoryConfig",
    "HotkeyConfig",
    "LatencyConfig",
    "LoggingConfig",
    "OverlayConfig",
    "PasteConfig",
    "Secret",
    "StreamingConfig",
    "TrayConfig",
    "load_config",
    "load_env_file",
    "parse_env_file",
]
