"""Everything the UI can *change*, separated from how it is routed.

:mod:`flow.dashboard.server` owns transport concerns -- binding, the token,
origin checks, JSON encoding. This module owns what the actions mean. The
split exists so every mutation is testable by calling a function with a
dataclass, without standing up a socket or minting a token, and so the
security-critical routing code stays small enough to read in one sitting.

Two rules hold throughout:

* **Validation happens once, here.** Route handlers pass raw decoded JSON
  straight in; every function below treats its input as hostile and returns a
  typed result or raises :class:`ActionError` with a message written for a
  human. Nothing downstream re-checks.
* **A failure to persist is reported, never swallowed.** If a setting cannot
  be written, the caller finds out -- a UI that says "Saved" over a failed
  write is worse than one that says nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sway import credentials
from sway.dictionary import SOURCES
from sway.hotkey import KNOWN_KEYS, ChordError, find_binding_conflicts, parse_chord, prettify_hotkey
from sway.transforms import (
    DEFAULT_IDS,
    DEFAULT_TRANSFORMS,
    MAX_DESCRIPTION_CHARS,
    MAX_INSTRUCTION_CHARS,
    MAX_TRANSFORMS,
    Transform,
    find_shortcut_conflicts,
)
from sway.settings import (
    CLEANUP_STYLES,
    CONTEXT_MODES,
    MAX_PAYLOAD_CHARS,
    MAX_NAME_CHARS,
    MAX_PHRASE_CHARS,
    MAX_TRIGGERS,
    OVERLAY_POSITIONS,
    TRIGGER_TYPES,
    AppRule,
    Settings,
    SettingsStore,
    SettingsWriteError,
    Trigger,
    normalise_phrase,
)
from sway.triggers import find_conflicts

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """A user-facing problem with a request. Rendered as-is in the UI."""


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------


def _require_str(payload: Mapping[str, Any], key: str, *, max_chars: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ActionError(f"{key.replace('_', ' ')} is required")
    text = value.strip()
    if not text:
        raise ActionError(f"{key.replace('_', ' ')} cannot be empty")
    if len(text) > max_chars:
        raise ActionError(f"{key.replace('_', ' ')} is too long (limit {max_chars} characters)")
    return text


def _optional_str(payload: Mapping[str, Any], key: str, *, max_chars: int = 200) -> str | None:
    """``None`` and ``""`` both mean "clear this override", which is what the
    UI sends when a user picks "System default"."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ActionError(f"{key.replace('_', ' ')} must be text")
    text = value.strip()
    if not text:
        return None
    return text[:max_chars]


def _optional_key(payload: Mapping[str, Any], key: str, *, max_chars: int = 64) -> str | None:
    """A hold-key name from :data:`sway.hotkey.KNOWN_KEYS`, or ``None``.

    Unlike :func:`_optional_str`, a value that is not a known key is a hard
    rejection at the door -- a UI save is exactly the place to refuse
    clearly, unlike a possibly-stale settings.json read at startup.
    """
    text = _optional_str(payload, key, max_chars=max_chars)
    if text is None:
        return None
    if text not in KNOWN_KEYS:
        raise ActionError(f"{prettify_hotkey(text)!r} is not a key Sway can hold -- pick a modifier key")
    return text


def _optional_chord(payload: Mapping[str, Any], key: str, *, max_chars: int = 64) -> str | None:
    """A chord string parseable by :func:`sway.hotkey.parse_chord`, or ``None``.

    Stored in canonical form, and rejected with ``ChordError``'s own
    message -- already written for a person, e.g. "a shortcut needs at
    least one modifier, like Ctrl+Alt+P".
    """
    text = _optional_str(payload, key, max_chars=max_chars)
    if text is None:
        return None
    try:
        return str(parse_chord(text))
    except ChordError as exc:
        raise ActionError(str(exc)) from exc


def _require_bool(payload: Mapping[str, Any], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ActionError(f"{key.replace('_', ' ')} must be true or false")
    return value


def _require_int(payload: Mapping[str, Any], key: str, *, low: int, high: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActionError(f"{key.replace('_', ' ')} must be a number")
    number = int(value)
    if not low <= number <= high:
        raise ActionError(f"{key.replace('_', ' ')} must be between {low} and {high}")
    return number


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

#: Fields the UI may set directly, with their validators. Anything not listed
#: cannot be written over HTTP at all, which is a deliberate allowlist: a new
#: field on :class:`Settings` is not remotely writable until someone adds it
#: here on purpose.
_SIMPLE_FIELDS: dict[str, Any] = {
    # hotkey, hotkey_chord, command_hotkey, command_hotkey_chord,
    # hands_free_key, and hands_free_chord are set through
    # save_activation_binding instead of here: those six need pairwise
    # conflict-checking against each other, which a one-field-at-a-time save
    # cannot do.
    "audio_device": lambda p: _optional_str(p, "audio_device"),
    "language": lambda p: _optional_str(p, "language", max_chars=32),
    "launch_at_login": lambda p: _require_bool(p, "launch_at_login"),
    "onboarding_complete": lambda p: _require_bool(p, "onboarding_complete"),
    "history_retention_days": lambda p: _require_int(p, "history_retention_days", low=0, high=3650),
    "latency_retention_days": lambda p: _require_int(p, "latency_retention_days", low=0, high=3650),
    "remove_fillers": lambda p: _require_bool(p, "remove_fillers"),
    "auto_learn_vocabulary": lambda p: _require_bool(p, "auto_learn_vocabulary"),
    "hands_free_enabled": lambda p: _require_bool(p, "hands_free_enabled"),
    "hands_free_double_tap": lambda p: _require_bool(p, "hands_free_double_tap"),
}


def update_settings(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Apply exactly the fields present in ``payload``.

    Partial by design: the UI saves one control at a time, so an absent key
    means "leave it alone" rather than "reset it". That also means two
    controls saved concurrently cannot clobber each other's values.
    """
    changes: dict[str, Any] = {}

    for name, validator in _SIMPLE_FIELDS.items():
        if name in payload:
            changes[name] = validator(payload)

    if "overlay_position" in payload:
        position = payload.get("overlay_position")
        if position not in OVERLAY_POSITIONS:
            raise ActionError(f"unknown overlay position: {position!r}")
        changes["overlay_position"] = position

    if "context_mode" in payload:
        mode = payload.get("context_mode")
        if mode not in CONTEXT_MODES:
            raise ActionError(f"unknown context mode: {mode!r}")
        changes["context_mode"] = mode

    if not changes:
        raise ActionError("nothing to change")

    try:
        return store.update(**changes)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc
    except ValueError as exc:  # unknown field slipped past the allowlist
        raise ActionError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


def save_trigger(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Create or update one trigger.

    A payload carrying an ``id`` that already exists is an edit and keeps its
    position in the list; anything else is an append. Position matters only
    for display -- :func:`flow.triggers.apply_triggers` orders by phrase
    length at match time -- but a list that reshuffles itself on every edit
    is disorienting.
    """
    current = store.get()
    phrase = _require_str(payload, "phrase", max_chars=MAX_PHRASE_CHARS)
    # Optional: a snippet the user calls "Intro email" is triggered by saying
    # "intro email", and most of the time those are the same string. Only the
    # phrase is required.
    name = _optional_str(payload, "name", max_chars=MAX_NAME_CHARS) or ""

    kind = payload.get("type", "macro")
    if kind not in TRIGGER_TYPES:
        raise ActionError(
            f"unsupported trigger type {kind!r}; this build supports "
            f"{' and '.join(TRIGGER_TYPES)}"
        )

    payload_text = ""
    if kind == "macro":
        payload_text = _require_str(payload, "payload", max_chars=MAX_PAYLOAD_CHARS)

    normalised = normalise_phrase(phrase)
    if not normalised:
        raise ActionError("that phrase has no words in it")

    trigger_id = payload.get("id")
    trigger_id = trigger_id if isinstance(trigger_id, str) and trigger_id else None

    # A second trigger with the same spoken phrase could never fire, so it is
    # refused at the door rather than silently dropped on the next load.
    for existing in current.triggers:
        if existing.normalised_phrase() == normalised and existing.id != trigger_id:
            raise ActionError(f"“{existing.phrase}” already uses that phrase")

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True

    if trigger_id is None and len(current.triggers) >= MAX_TRIGGERS:
        raise ActionError(f"you can have at most {MAX_TRIGGERS} triggers")

    updated: list[Trigger] = []
    replaced = False
    for existing in current.triggers:
        if existing.id == trigger_id:
            updated.append(
                Trigger(
                    id=existing.id,
                    name=name,
                    phrase=phrase,
                    payload=payload_text,
                    type=kind,
                    enabled=enabled,
                    created_at=existing.created_at,
                )
            )
            replaced = True
        else:
            updated.append(existing)

    if not replaced:
        from sway.settings import _new_id  # local: keeps id minting in one place

        updated.append(
            Trigger(
                id=trigger_id or _new_id(),
                name=name,
                phrase=phrase,
                payload=payload_text,
                type=kind,
                enabled=enabled,
                created_at=time.time(),
            )
        )

    try:
        return store.update(triggers=tuple(updated))
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def delete_trigger(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Remove one trigger. Deleting one that is already gone is not an error.

    Idempotent on purpose: the UI deletes optimistically and offers an undo,
    so a repeated delete (double click, retried request) must not surface a
    failure the user cannot act on.
    """
    trigger_id = payload.get("id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise ActionError("which trigger?")
    remaining = tuple(t for t in store.get().triggers if t.id != trigger_id)
    try:
        return store.update(triggers=remaining)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def trigger_view(settings: Settings) -> list[dict[str, Any]]:
    """Triggers plus their conflicts, shaped for the UI."""
    conflicts = find_conflicts(settings.triggers)
    by_id = {t.id: t for t in settings.triggers}
    return [
        {
            "id": t.id,
            "name": t.name,
            "label": t.display_name(),
            "phrase": t.phrase,
            "payload": t.payload,
            "type": t.type,
            "enabled": t.enabled,
            "created_at": t.created_at,
            "conflicts": [
                by_id[other].phrase for other in conflicts.get(t.id, []) if other in by_id
            ],
        }
        for t in settings.triggers
    ]


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


def save_transform(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Create or update one transform.

    Like :func:`save_trigger`, an existing id is an edit that keeps its
    position and anything else is an append.
    """
    current = store.get()
    name = _require_str(payload, "name", max_chars=MAX_NAME_CHARS)
    instruction = _require_str(payload, "instruction", max_chars=MAX_INSTRUCTION_CHARS)

    shortcut = _optional_str(payload, "shortcut", max_chars=64) or ""
    if shortcut:
        # Validated at the door rather than at bind time, so an unusable
        # shortcut is refused with a reason the user is looking at instead of
        # being silently skipped by the engine hours later.
        try:
            shortcut = str(parse_chord(shortcut))
        except ChordError as exc:
            raise ActionError(str(exc)) from exc

    phrase = _optional_str(payload, "phrase", max_chars=MAX_PHRASE_CHARS) or ""
    transform_id = payload.get("id")
    transform_id = transform_id if isinstance(transform_id, str) and transform_id else None

    if phrase and not normalise_phrase(phrase):
        raise ActionError("that phrase has no words in it")
    for existing in current.transforms:
        if (
            phrase
            and existing.id != transform_id
            and normalise_phrase(existing.phrase) == normalise_phrase(phrase)
        ):
            raise ActionError(f"“{existing.name}” already uses that phrase")
    for existing in current.triggers:
        if phrase and existing.normalised_phrase() == normalise_phrase(phrase):
            raise ActionError(f"the snippet “{existing.phrase}” already uses that phrase")

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True

    # What this instruction is allowed to do to the text. Editing a built-in
    # keeps its shipped expectation unless the payload says otherwise, for the
    # same reason the settings parser does (see settings._expectation_flag): a
    # form that round-trips without these keys must not quietly re-arm a
    # guardrail against the very output the instruction asks for.
    prior = next((t for t in current.transforms if t.id == transform_id), None) if transform_id else None

    def expectation(field: str) -> bool:
        value = payload.get(field)
        if isinstance(value, bool):
            return value
        return bool(getattr(prior, field)) if prior is not None else False

    allows_markdown = expectation("allows_markdown")
    may_shorten = expectation("may_shorten")
    may_lengthen = expectation("may_lengthen")

    if transform_id is None and len(current.transforms) >= MAX_TRANSFORMS:
        raise ActionError(f"you can have at most {MAX_TRANSFORMS} transforms")

    updated: list[Transform] = []
    replaced = False
    for existing in current.transforms:
        if existing.id == transform_id:
            updated.append(
                Transform(
                    id=existing.id,
                    name=name,
                    instruction=instruction,
                    description=_optional_str(payload, "description", max_chars=MAX_DESCRIPTION_CHARS)
                    or "",
                    phrase=phrase,
                    shortcut=shortcut,
                    icon=_optional_str(payload, "icon", max_chars=8) or "",
                    enabled=enabled,
                    # Editing a built-in keeps it a built-in, so "reset to
                    # defaults" can still put it back.
                    builtin=existing.builtin,
                    allows_markdown=allows_markdown,
                    may_shorten=may_shorten,
                    may_lengthen=may_lengthen,
                )
            )
            replaced = True
        else:
            updated.append(existing)

    if not replaced:
        from sway.settings import _new_id

        updated.append(
            Transform(
                id=transform_id or _new_id(),
                name=name,
                instruction=instruction,
                description=_optional_str(payload, "description", max_chars=MAX_DESCRIPTION_CHARS)
                or "",
                phrase=phrase,
                shortcut=shortcut,
                icon=_optional_str(payload, "icon", max_chars=8) or "",
                enabled=enabled,
                allows_markdown=allows_markdown,
                may_shorten=may_shorten,
                may_lengthen=may_lengthen,
            )
        )

    try:
        return store.update(transforms=tuple(updated))
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def delete_transform(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    transform_id = payload.get("id")
    if not isinstance(transform_id, str) or not transform_id:
        raise ActionError("which transform?")
    remaining = tuple(t for t in store.get().transforms if t.id != transform_id)
    try:
        return store.update(transforms=remaining)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def reset_transforms(store: SettingsStore) -> Settings:
    """Restore the shipped transforms, keeping the user's own.

    A built-in the user edited goes back to how it shipped; one they created
    is left completely alone. Wiping those too would make this button
    destructive in a way its name does not suggest.
    """
    custom = tuple(t for t in store.get().transforms if t.id not in DEFAULT_IDS)
    try:
        return store.update(transforms=DEFAULT_TRANSFORMS + custom)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def transform_view(settings: Settings) -> list[dict[str, Any]]:
    """Transforms plus their shortcut conflicts, shaped for the UI."""
    conflicts = find_shortcut_conflicts(settings.transforms)
    by_id = {t.id: t for t in settings.transforms}
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "instruction": t.instruction,
            "phrase": t.phrase,
            "shortcut": t.shortcut,
            "shortcut_label": parse_chord(t.shortcut).pretty() if t.shortcut else "",
            "icon": t.icon,
            "enabled": t.enabled,
            "builtin": t.builtin,
            "allows_markdown": t.allows_markdown,
            "may_shorten": t.may_shorten,
            "may_lengthen": t.may_lengthen,
            "conflicts": [
                by_id[other].name for other in conflicts.get(t.id, []) if other in by_id
            ],
        }
        for t in settings.transforms
    ]


# ---------------------------------------------------------------------------
# Activation bindings: the dictation hotkey, the voice-command hotkey, and
# hands-free dictation. Each may be bound to a held key (KNOWN_KEYS) and/or
# an alternate chord (parse_chord). Handled together, rather than through
# _SIMPLE_FIELDS, because the three must be checked pairwise for a
# collision before any of them is written -- two hold-shaped listeners on
# one physical key is genuinely broken (both fire on every press).
# ---------------------------------------------------------------------------

#: The three configurable activation bindings, and the Settings field pair
#: (hold-key field, chord field) each one owns.
ACTIVATION_BINDINGS: dict[str, tuple[str, str]] = {
    "dictation": ("hotkey", "hotkey_chord"),
    "command": ("command_hotkey", "command_hotkey_chord"),
    "hands_free": ("hands_free_key", "hands_free_chord"),
}

_ACTIVATION_LABELS = {
    "dictation": "the dictation hotkey",
    "command": "the voice-command hotkey",
    "hands_free": "hands-free dictation",
}


def _effective_hold_bindings(
    settings: Settings, effective: Mapping[str, Any] | None
) -> dict[str, str]:
    """Every hold-key binding actually in force right now, target -> key name.

    A binding not overridden in ``settings`` falls back to ``effective``
    (the engine's :meth:`~sway.engine.Engine.describe`, which already knows
    config.toml's defaults) when one is supplied. Without it -- a bare
    ``store`` with no engine attached, as in a unit test -- only what
    ``settings`` itself already holds is checked; that is a smaller but
    still useful check, and the authoritative backstop is
    ``sway.config._validate`` on the engine's merged config, which always
    runs before a binding takes live effect regardless of what this
    function could see.
    """
    effective = effective or {}
    resolved: dict[str, str] = {}
    for target, (key_field, _chord_field) in ACTIVATION_BINDINGS.items():
        value = getattr(settings, key_field, None)
        if value is None:
            value = effective.get(key_field)
        if value is not None:
            resolved[target] = value
    return resolved


def _effective_chord_bindings(
    settings: Settings, effective: Mapping[str, Any] | None
) -> dict[str, str]:
    effective = effective or {}
    resolved: dict[str, str] = {}
    for target, (_key_field, chord_field) in ACTIVATION_BINDINGS.items():
        value = getattr(settings, chord_field, None)
        if value is None:
            value = effective.get(chord_field)
        if value is not None:
            resolved[target] = value
    return resolved


def save_activation_binding(
    store: SettingsStore,
    payload: Mapping[str, Any],
    *,
    effective: Mapping[str, Any] | None = None,
) -> Settings:
    """Set one activation binding's held key and/or alternate chord.

    ``payload``: ``{"target": "dictation"|"command"|"hands_free", "key":
    ... | None, "chord": ... | None}``. Either or both of ``key``/``chord``
    may be present; an absent one is left untouched (matching
    :func:`update_settings`'s partial-save contract), and an explicit
    ``None`` (or ``""``) clears that half of the binding back to config's
    default.
    """
    target = payload.get("target")
    if target not in ACTIVATION_BINDINGS:
        raise ActionError(f"unknown activation target: {target!r}")
    key_field, chord_field = ACTIVATION_BINDINGS[target]

    current = store.get()
    changes: dict[str, Any] = {}
    if "key" in payload:
        changes[key_field] = _optional_key(payload, "key")
    if "chord" in payload:
        changes[chord_field] = _optional_chord(payload, "chord")

    if not changes:
        return current
    candidate = replace(current, **changes)

    hold_bindings = _effective_hold_bindings(candidate, effective)
    this_key = hold_bindings.get(target)
    if this_key is not None:
        for other_target, other_key in hold_bindings.items():
            if other_target != target and other_key == this_key:
                raise ActionError(
                    f"that key is already {_ACTIVATION_LABELS[other_target]} -- pick a different one"
                )

    chord_bindings = _effective_chord_bindings(candidate, effective)
    this_chord = chord_bindings.get(target)
    if this_chord is not None:
        for other_target, other_chord in chord_bindings.items():
            if other_target != target and other_chord == this_chord:
                raise ActionError(
                    f"that shortcut is already {_ACTIVATION_LABELS[other_target]} -- pick a different one"
                )

    try:
        return store.update(**changes)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def reset_activation_binding(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Clear one binding's override(s) back to config's default."""
    target = payload.get("target")
    if target not in ACTIVATION_BINDINGS:
        raise ActionError(f"unknown activation target: {target!r}")
    key_field, chord_field = ACTIVATION_BINDINGS[target]
    try:
        return store.update(**{key_field: None, chord_field: None})
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def activation_view(
    settings: Settings, effective: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Every activation binding's current effective state, shaped for the UI."""
    effective = effective or {}
    hold_bindings = _effective_hold_bindings(settings, effective)
    chord_bindings = _effective_chord_bindings(settings, effective)
    hold_conflicts = find_binding_conflicts(hold_bindings)
    chord_conflicts = find_binding_conflicts(chord_bindings)

    view: dict[str, dict[str, Any]] = {}
    for target, (key_field, chord_field) in ACTIVATION_BINDINGS.items():
        key = hold_bindings.get(target)
        chord = chord_bindings.get(target)
        conflicts = [
            _ACTIVATION_LABELS[other]
            for other in (*hold_conflicts.get(target, []), *chord_conflicts.get(target, []))
        ]
        view[target] = {
            "key": key,
            "key_label": prettify_hotkey(key) if key else "",
            "chord": chord,
            "chord_label": parse_chord(chord).pretty() if chord else "",
            "is_default": getattr(settings, key_field) is None and getattr(settings, chord_field) is None,
            "conflicts": conflicts,
        }
    view["hands_free"]["enabled"] = settings.hands_free_enabled
    view["hands_free"]["double_tap"] = bool(
        settings.hands_free_double_tap
        if settings.hands_free_double_tap is not None
        else effective.get("hands_free_double_tap", False)
    )
    return view


# ---------------------------------------------------------------------------
# Per-app rules
# ---------------------------------------------------------------------------


def save_app_rule(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Set the cleanup style for one process name."""
    process = _require_str(payload, "process", max_chars=120).lower()
    style = payload.get("style")
    if style not in CLEANUP_STYLES:
        raise ActionError(f"unknown style {style!r}")

    rules = [r for r in store.get().app_rules if r.normalised_process() != process]
    rules.append(AppRule(process=process, style=style))
    try:
        return store.update(app_rules=tuple(rules))
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def delete_app_rule(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    process = _require_str(payload, "process", max_chars=120).lower()
    remaining = tuple(r for r in store.get().app_rules if r.normalised_process() != process)
    try:
        return store.update(app_rules=remaining)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------


def dictionary_add(db_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add one or more terms. Accepts a single term or a list."""
    from sway.dictionary import DictionaryStore

    raw = payload.get("terms", payload.get("term"))
    terms = [raw] if isinstance(raw, str) else raw
    if not isinstance(terms, list) or not terms:
        raise ActionError("no terms given")
    cleaned = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    if not cleaned:
        raise ActionError("no terms given")
    if len(cleaned) > 500:
        raise ActionError("that is too many terms at once (limit 500)")

    source = payload.get("source", "manual")
    if source not in SOURCES:
        raise ActionError(f"unknown source {source!r}")

    with DictionaryStore.open(db_path) as store:
        added = store.add_many(cleaned, source=source)
        return {"added": added, **dictionary_payload(store)}


def dictionary_remove(db_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    from sway.dictionary import DictionaryStore

    raw = payload.get("terms", payload.get("term"))
    terms = [raw] if isinstance(raw, str) else raw
    if not isinstance(terms, list) or not terms:
        raise ActionError("no terms given")
    cleaned = [t.strip() for t in terms if isinstance(t, str) and t.strip()]
    if not cleaned:
        raise ActionError("no terms given")

    with DictionaryStore.open(db_path) as store:
        removed = store.remove_many(cleaned)
        return {"removed": removed, **dictionary_payload(store)}


def dictionary_pin(db_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Mark a term important so it survives the prompt budget."""
    from sway.dictionary import DictionaryStore

    term = _require_str(payload, "term", max_chars=200)
    pinned = _require_bool(payload, "pinned")
    with DictionaryStore.open(db_path) as store:
        if not store.set_pinned(term, pinned):
            raise ActionError(f"{term!r} is not in the dictionary")
        return dictionary_payload(store)


def dictionary_export(db_path: Path) -> dict[str, Any]:
    """A shareable vocabulary pack.

    This is the whole of "team vocabulary" in a product with no accounts and no
    server (CLAUDE.md: no server component). One person exports, the others
    import; terms arrive labelled "shared" and can be removed like any other.
    Nothing is uploaded, and nothing leaves the machine unless the user
    deliberately hands someone the file.

    Usage counts and pins are *not* exported. They describe how one person
    works, and they are nobody else's business.
    """
    from sway.dictionary import DictionaryStore

    with DictionaryStore.open(db_path) as store:
        return {
            "version": 1,
            "exported_at": time.time(),
            "terms": [entry.term for entry in store.list_entries()],
        }


def dictionary_import(db_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Ingest a pack produced by :func:`dictionary_export`."""
    from sway.dictionary import SOURCE_SHARED, DictionaryStore

    pack = payload.get("pack", payload)
    # `dict`, not `Mapping`: this module imports Mapping only for typing, and
    # a JSON body decodes to a plain dict regardless.
    if not isinstance(pack, dict):
        raise ActionError("that does not look like a vocabulary pack")
    version = pack.get("version")
    if version != 1:
        raise ActionError(f"this build cannot read a version {version!r} vocabulary pack")
    raw = pack.get("terms")
    if not isinstance(raw, list) or not raw:
        raise ActionError("that pack has no terms in it")
    cleaned = [t.strip() for t in raw if isinstance(t, str) and t.strip()]
    if not cleaned:
        raise ActionError("that pack has no terms in it")
    if len(cleaned) > 2000:
        raise ActionError("that pack is too large (limit 2000 terms)")

    with DictionaryStore.open(db_path) as store:
        added = store.add_many(cleaned, source=SOURCE_SHARED)
        return {"added": added, "skipped": len(cleaned) - added, **dictionary_payload(store)}


def dictionary_payload(store: Any) -> dict[str, Any]:
    """The shape every dictionary route returns, read and write alike, so the
    UI has exactly one contract to render against.

    ``terms`` is kept alongside the richer ``entries`` because the CLI and the
    older client code read it, and dropping it would break them for no gain.
    """
    entries = store.list_entries()
    return {
        "terms": [entry.term for entry in entries],
        "entries": [
            {
                "term": entry.term,
                "source": entry.source,
                "pinned": entry.pinned,
                "hits": entry.hits,
                "last_used_at": entry.last_used_at,
            }
            for entry in entries
        ],
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def history_delete(db_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    from sway.history import HistoryStore

    entry_id = payload.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        raise ActionError("which entry?")
    with HistoryStore.open(db_path) as store:
        return {"deleted": store.delete(entry_id)}


def history_delete_all(db_path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Wipe every transcript.

    Requires ``confirm: true`` in the payload. That is not UI politeness --
    it means a stray or replayed POST to this route cannot destroy a user's
    entire history, which is the one irreversible action the app exposes.
    """
    from sway.history import HistoryStore

    if payload.get("confirm") is not True:
        raise ActionError("delete-all needs an explicit confirmation")
    with HistoryStore.open(db_path) as store:
        return {"deleted": store.delete_all()}


# ---------------------------------------------------------------------------
# API key
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyStatus:
    """What the Settings screen shows about the key, without revealing it."""

    configured: bool
    masked: str
    source: str
    keyring_available: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "masked": self.masked,
            "source": self.source,
            "keyring_available": self.keyring_available,
        }


def key_status() -> KeyStatus:
    key, source = credentials.resolve_key()
    return KeyStatus(
        configured=bool(key),
        masked=credentials.mask(key),
        source=source,
        keyring_available=credentials.keyring_available(),
    )


def set_key(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Store a new API key.

    A key that does not look like a Groq key is stored anyway but reported
    back with a warning: Groq's prefix is not a documented contract, and
    refusing a valid future key would lock the user out of their own app.
    The authoritative check is :func:`test_key`, which asks Groq.
    """
    raw = payload.get("key")
    if not isinstance(raw, str):
        raise ActionError("no key given")
    cleaned = credentials.normalise_key(raw)
    if not cleaned:
        raise ActionError("no key given")

    try:
        credentials.store_key(cleaned)
    except credentials.CredentialError as exc:
        raise ActionError(str(exc)) from exc

    status = key_status()
    return {
        **status.as_dict(),
        "warning": (
            None
            if credentials.looks_like_key(cleaned)
            else "That does not look like a Groq key — they usually start with gsk_."
        ),
    }


__all__ = [
    "ActionError",
    "KeyStatus",
    "delete_app_rule",
    "delete_trigger",
    "dictionary_add",
    "dictionary_payload",
    "dictionary_pin",
    "dictionary_import",
    "dictionary_export",
    "dictionary_remove",
    "history_delete",
    "history_delete_all",
    "key_status",
    "save_app_rule",
    "save_transform",
    "delete_transform",
    "reset_transforms",
    "transform_view",
    "save_trigger",
    "set_key",
    "trigger_view",
    "update_settings",
]
