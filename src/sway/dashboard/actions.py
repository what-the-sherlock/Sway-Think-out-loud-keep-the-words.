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
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sway import credentials, languages
from sway.dictionary import SOURCES
from sway.diff import as_dicts, word_diff
from sway.hotkey import (
    BindingError,
    binding_advisory,
    effective_bindings,
    find_binding_list_conflicts,
    parse_binding,
)
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
    MAX_BINDINGS_PER_ACTION,
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


def _one_binding(text: str) -> str:
    """A binding string in canonical form, or a rejection written for a
    person -- :class:`~sway.hotkey.BindingError`'s own message, e.g. "a
    shortcut needs at least one modifier, like Ctrl+Alt+P"."""
    try:
        return str(parse_binding(text))
    except BindingError as exc:
        raise ActionError(str(exc)) from exc


def _binding_list(
    payload: Mapping[str, Any], key: str, *, max_items: int = MAX_BINDINGS_PER_ACTION
) -> tuple[str, ...]:
    """Every binding in ``payload[key]``, validated and de-duplicated.

    Absent or not a list is treated as "no bindings" (an empty tuple) rather
    than an error -- a transform or activation target is allowed to have
    none, same as it is allowed to have no phrase.
    """
    raw = payload.get(key)
    if not isinstance(raw, list):
        return ()
    items: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        canonical = _one_binding(entry)
        if canonical in seen:
            continue
        seen.add(canonical)
        items.append(canonical)
        if len(items) >= max_items:
            raise ActionError(f"you can have at most {max_items} bindings for one action")
    return tuple(items)


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
    # dictation_bindings, command_bindings, and hands_free_bindings are set
    # through save_activation_binding instead of here: those three need
    # pairwise conflict-checking against each other, which a
    # one-field-at-a-time save cannot do.
    "audio_device": lambda p: _optional_str(p, "audio_device"),
    "language": lambda p: _optional_str(p, "language", max_chars=32),
    # Normalised, unlike the input hint above: an unrecognised target has no
    # script for the guardrail to verify against, so it resolves to None
    # ("same language I spoke") rather than being stored as a live request.
    "target_language": lambda p: languages.normalize(
        _optional_str(p, "target_language", max_chars=32)
    ),
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
    position and anything else is an append. A transform is a full-record
    save, same as a trigger -- the editor always sends the whole record
    (autosaved on every field change), so there is no partial-update case to
    support here.

    A shortcut is only rejected here for being unparsable or an exact
    duplicate of another transform's (see :func:`transform_view`'s
    ``conflicts`` for the latter, surfaced rather than blocked, same as a
    trigger's phrase). It is never rejected merely for sharing a modifier
    with another binding -- :class:`sway.hotkey.BindingListener` resolves
    that ambiguity itself at dispatch time (prefix grace), so e.g. a
    transform on Ctrl+Shift+T works correctly alongside dictation on bare
    Ctrl with no configuration-time decision needed.
    """
    current = store.get()
    name = _require_str(payload, "name", max_chars=MAX_NAME_CHARS)
    instruction = _require_str(payload, "instruction", max_chars=MAX_INSTRUCTION_CHARS)

    # Validated at the door rather than at bind time, so an unusable binding
    # is refused with a reason the user is looking at instead of being
    # silently skipped by the engine hours later.
    shortcuts = _binding_list(payload, "shortcuts")

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

    # What this instruction is allowed to do to the text. Editing a built-in
    # keeps its shipped expectation unless the payload says otherwise, for the
    # same reason the settings parser does (see settings._expectation_flag): a
    # form that round-trips without these keys must not quietly re-arm a
    # guardrail against the very output the instruction asks for. The example
    # pair works the same way: the editor never sends it back, so an ordinary
    # save cannot erase a stored example -- only the engine writes one, after
    # the transform's first real run (see Engine._run_transform).
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

    def build(existing_id: str, *, builtin: bool) -> Transform:
        return Transform(
            id=existing_id,
            name=name,
            instruction=instruction,
            description=_optional_str(payload, "description", max_chars=MAX_DESCRIPTION_CHARS) or "",
            phrase=phrase,
            shortcuts=shortcuts,
            icon=_optional_str(payload, "icon", max_chars=8) or "",
            builtin=builtin,
            example_input=prior.example_input if prior is not None else "",
            example_output=prior.example_output if prior is not None else "",
            allows_markdown=allows_markdown,
            may_shorten=may_shorten,
            may_lengthen=may_lengthen,
        )

    updated: list[Transform] = []
    replaced = False
    for existing in current.transforms:
        if existing.id == transform_id:
            # Editing a built-in keeps it a built-in, so "reset to defaults"
            # can still put it back.
            updated.append(build(existing.id, builtin=existing.builtin))
            replaced = True
        else:
            updated.append(existing)

    if not replaced:
        from sway.settings import _new_id

        updated.append(build(transform_id or _new_id(), builtin=False))

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
    destructive in a way its name does not suggest. "The user's own" is
    computed fresh against the *current* build's default ids, so a transform
    that shipped as a built-in in an older version but no longer does (the
    old Fix grammar/Make casual/Turn into bullet points set) is treated as
    the user's own from here on rather than silently dropped.
    """
    custom = tuple(t for t in store.get().transforms if t.id not in DEFAULT_IDS)
    try:
        return store.update(transforms=DEFAULT_TRANSFORMS + custom)
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def transform_view(settings: Settings) -> list[dict[str, Any]]:
    """Transforms plus their shortcut conflicts, shaped for the UI.

    ``conflicts`` lists only an *exact* duplicate shortcut shared with
    another transform (:func:`~sway.transforms.find_shortcut_conflicts`) --
    two transforms cannot both usefully hold the identical gesture, since it
    would only ever reach whichever one the listener dispatches to first.
    A shortcut that merely *overlaps* another binding without being
    identical (e.g. a transform on Ctrl+Shift+T while dictation holds bare
    Ctrl) is not a conflict at all: :class:`sway.hotkey.BindingListener`
    already resolves that ambiguity at dispatch time via prefix grace, so
    both fire correctly and independently. See ``shortcut_advisories`` for
    the one thing worth telling the user about a shortcut that is not a
    conflict: Sway's hooks are non-suppressing, so a shortcut using Alt also
    triggers menu mnemonics in whatever app is focused.
    """
    conflicts = find_shortcut_conflicts(settings.transforms)
    by_id = {t.id: t for t in settings.transforms}
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "instruction": t.instruction,
            "phrase": t.phrase,
            "shortcuts": list(t.shortcuts),
            "shortcut_labels": [parse_binding(s).pretty() for s in t.shortcuts],
            "shortcut_advisories": [binding_advisory(parse_binding(s)) for s in t.shortcuts],
            "icon": t.icon,
            "builtin": t.builtin,
            "runnable": bool(t.shortcuts) or bool(t.phrase.strip()),
            "example_input": t.example_input,
            "example_output": t.example_output,
            "example_diff": (
                as_dicts(word_diff(t.example_input, t.example_output))
                if t.example_input and t.example_output
                else []
            ),
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
# hands-free dictation. Each holds a *list* of bindings (a hold key, a
# modifier combo, a chord, or a mouse button, in any mix) rather than a
# single primary key plus one alternate chord -- see sway.hotkey.Binding.
# Handled together, rather than through _SIMPLE_FIELDS, because the three
# must be checked pairwise for a collision before any of them is written --
# two listeners sharing one physical gesture is genuinely broken (both fire
# on every press).
# ---------------------------------------------------------------------------

#: The three configurable activation targets: each one's Settings list field,
#: and the pair of sway.engine.Engine.describe() keys that hold config.toml's
#: default hold-key and alternate-chord for it (see
#: sway.hotkey.effective_bindings).
ACTIVATION_BINDINGS: dict[str, tuple[str, str, str]] = {
    "dictation": ("dictation_bindings", "hotkey", "hotkey_chord"),
    "command": ("command_bindings", "command_hotkey", "command_hotkey_chord"),
    "hands_free": ("hands_free_bindings", "hands_free_key", "hands_free_chord"),
}

_ACTIVATION_LABELS = {
    "dictation": "the dictation hotkey",
    "command": "the voice-command hotkey",
    "hands_free": "hands-free dictation",
}


def _effective_binding_lists(
    settings: Settings, effective: Mapping[str, Any] | None
) -> dict[str, tuple[str, ...]]:
    """Every binding actually in force right now, target -> its list.

    A target not overridden in ``settings`` (``None``) falls back to
    ``effective`` (the engine's :meth:`~sway.engine.Engine.describe`, which
    already knows config.toml's defaults) when one is supplied. Without it --
    a bare ``store`` with no engine attached, as in a unit test -- only
    config's compiled-in defaults (``HotkeyConfig``'s dataclass defaults, via
    a plain ``{}``) are missing; that is a smaller but still useful check, and
    the authoritative backstop is ``sway.config._validate`` on the engine's
    merged config, which always runs before a binding takes live effect
    regardless of what this function could see.
    """
    effective = effective or {}
    return {
        target: effective_bindings(
            getattr(settings, field), effective.get(default_key), effective.get(default_chord_key)
        )
        for target, (field, default_key, default_chord_key) in ACTIVATION_BINDINGS.items()
    }


def save_activation_binding(
    store: SettingsStore,
    payload: Mapping[str, Any],
    *,
    effective: Mapping[str, Any] | None = None,
) -> Settings:
    """Add, remove, or replace one binding in one activation target's list.

    ``payload``: ``{"target": "dictation"|"command"|"hands_free", "op":
    "add"|"remove"|"replace", "binding": "ctrl+cmd", "old_binding": ...}``.
    ``op`` defaults to ``"add"``. ``"replace"`` needs ``old_binding`` (the
    binding being edited) alongside the new ``binding``; if ``old_binding``
    is not actually present the new one is simply added, so a stale edit
    dialog degrades to an add rather than silently doing nothing.

    The very first customization of a target materializes its *whole*
    effective list (config's defaults included) before applying the one
    change, so "add a second binding" means exactly that rather than
    silently discarding the config-default binding the user had not
    previously had to think about.
    """
    target = payload.get("target")
    if target not in ACTIVATION_BINDINGS:
        raise ActionError(f"unknown activation target: {target!r}")
    field, _default_key, _default_chord_key = ACTIVATION_BINDINGS[target]

    lists = _effective_binding_lists(store.get(), effective)
    working = list(lists[target])
    op = payload.get("op", "add")

    if op == "add":
        raw = payload.get("binding")
        if not isinstance(raw, str) or not raw.strip():
            raise ActionError("which binding?")
        binding = _one_binding(raw)
        if binding in working:
            raise ActionError("that binding is already set for this action")
        if len(working) >= MAX_BINDINGS_PER_ACTION:
            raise ActionError(f"you can have at most {MAX_BINDINGS_PER_ACTION} bindings for one action")
        working.append(binding)
    elif op == "remove":
        raw = payload.get("binding")
        if not isinstance(raw, str) or not raw.strip():
            raise ActionError("which binding?")
        binding = _one_binding(raw)
        if binding not in working:
            raise ActionError("that binding is not set for this action")
        working.remove(binding)
    elif op == "replace":
        new_raw = payload.get("binding")
        if not isinstance(new_raw, str) or not new_raw.strip():
            raise ActionError("which binding?")
        new_binding = _one_binding(new_raw)
        old_raw = payload.get("old_binding")
        old_binding = _one_binding(old_raw) if isinstance(old_raw, str) and old_raw.strip() else None
        if old_binding is not None and old_binding in working:
            working = [new_binding if b == old_binding else b for b in working]
        elif new_binding not in working:
            working.append(new_binding)
        # De-duplicate: replacing one binding with a value already present
        # elsewhere in the list must not leave two copies of it.
        seen: set[str] = set()
        deduped: list[str] = []
        for b in working:
            if b not in seen:
                seen.add(b)
                deduped.append(b)
        working = deduped
    else:
        raise ActionError(f"unknown operation: {op!r}")

    candidate_lists = dict(lists)
    candidate_lists[target] = tuple(working)
    conflicts = find_binding_list_conflicts(candidate_lists)
    if conflicts.get(target):
        other = conflicts[target][0]
        raise ActionError(
            f"that binding is already {_ACTIVATION_LABELS[other]} -- pick a different one"
        )

    try:
        return store.update(**{field: tuple(working)})
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def reset_activation_binding(store: SettingsStore, payload: Mapping[str, Any]) -> Settings:
    """Clear one target's override back to config's default list."""
    target = payload.get("target")
    if target not in ACTIVATION_BINDINGS:
        raise ActionError(f"unknown activation target: {target!r}")
    field, _default_key, _default_chord_key = ACTIVATION_BINDINGS[target]
    try:
        return store.update(**{field: None})
    except SettingsWriteError as exc:
        raise ActionError(str(exc)) from exc


def activation_view(
    settings: Settings, effective: Mapping[str, Any] | None = None
) -> dict[str, dict[str, Any]]:
    """Every activation target's current effective bindings, shaped for the UI."""
    effective = effective or {}
    lists = _effective_binding_lists(settings, effective)
    conflicts = find_binding_list_conflicts(lists)

    view: dict[str, dict[str, Any]] = {}
    for target, (field, _default_key, _default_chord_key) in ACTIVATION_BINDINGS.items():
        view[target] = {
            "bindings": [
                {
                    "value": b,
                    "label": parse_binding(b).pretty(),
                    "shape": parse_binding(b).shape,
                    "advisory": binding_advisory(parse_binding(b)),
                }
                for b in lists[target]
            ],
            "is_default": getattr(settings, field) is None,
            "conflicts": [_ACTIVATION_LABELS[other] for other in conflicts.get(target, [])],
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
# Diagnostics
# ---------------------------------------------------------------------------

#: Matches a header line written by sway.logging_setup's file formatter:
#: "%(asctime)s %(process)d %(levelname)-7s %(name)s [%(threadName)s]: %(message)s".
#: A line that does not match (a traceback frame, a multi-line message) is
#: folded into the entry currently being built rather than dropped, so an
#: exception logged with an error is not truncated to its first line.
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"\d+ (?P<level>[A-Z]+)\s+(?P<logger>\S+) \[[^\]]*\]: (?P<message>.*)$"
)
_DIAGNOSTIC_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
#: A runaway traceback should not blow up the response the dashboard renders.
_MAX_DIAGNOSTIC_MESSAGE_CHARS = 4000


def recent_diagnostics(log_file: Path | None, *, limit: int = 50) -> dict[str, Any]:
    """The most recent warnings and errors from Sway's own log file.

    This exists because a packaged build has no console to watch
    (sway.logging_setup skips the console handler when ``sys.stderr`` is
    ``None``, which is how a windowed PyInstaller build runs) -- sway.log is
    then the *only* place a failure is ever recorded, and a user should not
    have to go find and open a text file to see it. Reads the file fresh on
    every call rather than tailing it, so this reflects whatever the running
    process (or a previous one, sharing the same file) actually wrote.

    Returns entries newest-first, each trimmed to
    :data:`_MAX_DIAGNOSTIC_MESSAGE_CHARS` so one enormous traceback cannot
    balloon the response.
    """
    if log_file is None or not log_file.exists():
        return {"available": False, "path": str(log_file) if log_file else "", "entries": []}

    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"available": False, "path": str(log_file), "entries": [], "error": str(exc)}

    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        match = _LOG_LINE_RE.match(line)
        if match is not None:
            current = None
            if match.group("level") in _DIAGNOSTIC_LEVELS:
                current = {
                    "timestamp": match.group("ts"),
                    "level": match.group("level"),
                    "logger": match.group("logger"),
                    "message": match.group("message"),
                }
                entries.append(current)
        elif current is not None:
            current["message"] += "\n" + line

    recent = entries[-limit:]
    recent.reverse()
    for entry in recent:
        if len(entry["message"]) > _MAX_DIAGNOSTIC_MESSAGE_CHARS:
            entry["message"] = entry["message"][:_MAX_DIAGNOSTIC_MESSAGE_CHARS] + "\n… (truncated)"
    return {
        "available": True,
        "path": str(log_file),
        "entries": recent,
        "total_matched": len(entries),
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
    "recent_diagnostics",
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
