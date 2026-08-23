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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sway import credentials
from sway.dictionary import SOURCES
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
    "hotkey": lambda p: _optional_str(p, "hotkey", max_chars=64),
    "audio_device": lambda p: _optional_str(p, "audio_device"),
    "language": lambda p: _optional_str(p, "language", max_chars=32),
    "launch_at_login": lambda p: _require_bool(p, "launch_at_login"),
    "onboarding_complete": lambda p: _require_bool(p, "onboarding_complete"),
    "history_retention_days": lambda p: _require_int(p, "history_retention_days", low=0, high=3650),
    "latency_retention_days": lambda p: _require_int(p, "latency_retention_days", low=0, high=3650),
    "remove_fillers": lambda p: _require_bool(p, "remove_fillers"),
    "auto_learn_vocabulary": lambda p: _require_bool(p, "auto_learn_vocabulary"),
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
    "save_trigger",
    "set_key",
    "trigger_view",
    "update_settings",
]
