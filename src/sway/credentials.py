"""Where the Groq API key lives.

D5's rule, restated because the ordering is the whole design:

1. **Windows Credential Manager** (via ``keyring``) -- where a normal user's
   key ends up, set from the UI, never written to a file the app controls.
2. **``FLOW_GROQ_API_KEY``** -- an explicit developer override. Deliberately
   *not* the bare ``GROQ_API_KEY``, so a key exported for some other tool in
   the same shell cannot silently outrank the one the user configured here.
3. **``GROQ_API_KEY``** from the environment or ``.env`` -- the existing dev
   flow, unchanged.

On first run, a key found only at level 3 is **copied up** into the keyring
and ``.env`` is left exactly as it was. Silently editing a file the user
wrote is worse than a duplicated secret, and the dev flow depends on ``.env``
continuing to work.

``keyring`` is an optional import. If it is missing, or the platform has no
usable backend (a headless CI box, a Linux machine with no Secret Service),
every function here degrades to "the keyring holds nothing" and the
environment path keeps working. The app must never fail to start because a
credential *store* is unavailable -- that would trade a missing key for a
dead application.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Credential Manager entry. Shown to the user in that UI, so it reads as a
#: product name rather than a module path.
SERVICE_NAME = "Sway"
ACCOUNT_NAME = "groq-api-key"

#: What the entry was called before the Flow -> Sway rename. Read by
#: :mod:`sway.migrate` to carry an existing key across, and never written.
LEGACY_SERVICE_NAME = "Flow Dictation"

#: Checked before the bare GROQ_API_KEY so a developer can override a stored
#: key without clearing Credential Manager.
ENV_OVERRIDE = "SWAY_GROQ_API_KEY"
#: Accepted for continuity with pre-rename setups. Lower precedence.
ENV_OVERRIDE_LEGACY = "FLOW_GROQ_API_KEY"
ENV_FALLBACK = "GROQ_API_KEY"

#: Groq keys are opaque, but every one issued to date carries this prefix.
#: Used only to catch an obvious paste error (a whole curl command, a URL)
#: before spending a network round trip -- never to reject a key outright,
#: because a prefix change on Groq's side must not lock users out.
_EXPECTED_PREFIX = "gsk_"

#: Longest thing we will accept as a key. Guards against a user pasting an
#: entire file into the field.
_MAX_KEY_CHARS = 512


class CredentialError(Exception):
    """The credential store exists but could not be read or written."""


def _keyring():  # type: ignore[no-untyped-def]
    """The keyring module, or ``None`` if unusable on this machine.

    Both the import and the backend probe can fail, and they fail
    differently: no package versus a package with no working backend. Either
    way the answer for callers is the same, so they are collapsed here.
    """
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception:
        logger.debug("keyring is not available", exc_info=True)
        return None

    try:
        backend = keyring.get_keyring()
    except Exception:
        logger.debug("keyring has no usable backend", exc_info=True)
        return None

    if isinstance(backend, FailKeyring):
        logger.debug("keyring resolved to the null backend; treating it as absent")
        return None
    return keyring


def keyring_available() -> bool:
    """Whether a real credential store is usable on this machine."""
    return _keyring() is not None


def read_stored_key() -> str:
    """The key held in the credential store, or ``""``."""
    keyring = _keyring()
    if keyring is None:
        return ""
    try:
        return (keyring.get_password(SERVICE_NAME, ACCOUNT_NAME) or "").strip()
    except Exception:
        logger.warning("could not read the API key from the credential store", exc_info=True)
        return ""


def read_legacy_key() -> str:
    """The key stored under the pre-rename service name, or ``""``.

    Read-only, and used by :mod:`sway.migrate` alone. There is no matching
    writer: nothing should ever put a new secret under the old name.
    """
    keyring = _keyring()
    if keyring is None:
        return ""
    try:
        return (keyring.get_password(LEGACY_SERVICE_NAME, ACCOUNT_NAME) or "").strip()
    except Exception:
        logger.debug("could not read the pre-rename API key", exc_info=True)
        return ""


def store_key(key: str) -> None:
    """Write the key to the credential store.

    Raises :class:`CredentialError` rather than failing silently: a user who
    presses Save must be told if it did not save, otherwise they will restart
    into the same "no API key" state with no idea why.
    """
    cleaned = normalise_key(key)
    if not cleaned:
        raise CredentialError("the key is empty")

    keyring = _keyring()
    if keyring is None:
        raise CredentialError(
            "this machine has no credential store available, so the key cannot be saved securely"
        )
    try:
        keyring.set_password(SERVICE_NAME, ACCOUNT_NAME, cleaned)
    except Exception as exc:
        raise CredentialError(f"could not save the key: {exc}") from exc


def clear_key() -> None:
    """Remove the stored key. Succeeds when there was nothing to remove."""
    keyring = _keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(SERVICE_NAME, ACCOUNT_NAME)
    except Exception:
        # Deleting a key that is not there is the normal case on a fresh
        # machine and is not worth a warning.
        logger.debug("no stored API key to clear", exc_info=True)


def normalise_key(key: str) -> str:
    """Trim a pasted key. Does not validate it -- only Groq can do that."""
    if not isinstance(key, str):
        return ""
    return key.strip().strip("\"'")[:_MAX_KEY_CHARS]


def looks_like_key(key: str) -> bool:
    """A cheap sanity check, used to warn rather than to block."""
    cleaned = normalise_key(key)
    return bool(cleaned) and cleaned.startswith(_EXPECTED_PREFIX) and len(cleaned) > 20


def mask(key: str) -> str:
    """``gsk_...7d4f`` -- enough to tell two keys apart, not enough to use.

    Showing the last four characters is the one thing masking must not take
    away: a user with two keys needs to know which one is configured.
    """
    cleaned = normalise_key(key)
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "•" * len(cleaned)
    prefix = cleaned[:4] if cleaned.startswith(_EXPECTED_PREFIX) else ""
    return f"{prefix}{'•' * 12}{cleaned[-4:]}"


def resolve_key(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """The key to use, and where it came from.

    Returns ``(key, source)`` where source is one of ``keyring``, ``env``,
    ``dotenv`` or ``""``. The source is surfaced in Settings so a developer
    can see *why* the key in Credential Manager is not the one being used.
    """
    environ = os.environ if env is None else env

    stored = read_stored_key()
    if stored:
        return stored, "keyring"

    override = normalise_key(environ.get(ENV_OVERRIDE, "")) or normalise_key(
        environ.get(ENV_OVERRIDE_LEGACY, "")
    )
    if override:
        return override, "env"

    fallback = normalise_key(environ.get(ENV_FALLBACK, ""))
    if fallback:
        return fallback, "dotenv"

    return "", ""


def migrate_to_keyring(env: Mapping[str, str] | None = None) -> bool:
    """Copy an environment/``.env`` key into the credential store, once.

    Returns True only when a key was actually copied, so the caller can tell
    the user it happened. Never touches ``.env``. Never overwrites a key
    already in the store -- if both exist, the stored one is the user's
    deliberate choice and wins.

    Best-effort: on a machine with no credential store this is a no-op, and
    the environment path continues to work unchanged.
    """
    if not keyring_available():
        return False
    if read_stored_key():
        return False

    environ = os.environ if env is None else env
    candidate = (
        normalise_key(environ.get(ENV_OVERRIDE, ""))
        or normalise_key(environ.get(ENV_OVERRIDE_LEGACY, ""))
        or normalise_key(environ.get(ENV_FALLBACK, ""))
    )
    if not candidate:
        return False

    try:
        store_key(candidate)
    except CredentialError as exc:
        logger.warning("could not migrate the API key into the credential store: %s", exc)
        return False

    logger.info(
        "copied the Groq API key into Windows Credential Manager; "
        "your .env file was left untouched"
    )
    return True


__all__ = [
    "ACCOUNT_NAME",
    "ENV_FALLBACK",
    "ENV_OVERRIDE",
    "ENV_OVERRIDE_LEGACY",
    "LEGACY_SERVICE_NAME",
    "SERVICE_NAME",
    "CredentialError",
    "clear_key",
    "keyring_available",
    "looks_like_key",
    "mask",
    "migrate_to_keyring",
    "normalise_key",
    "read_legacy_key",
    "read_stored_key",
    "resolve_key",
    "store_key",
]
