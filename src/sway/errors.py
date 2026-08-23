"""Exception hierarchy for Sway.

Everything raised on purpose derives from :class:`SwayError` so the CLI can
render a short message instead of a traceback, while genuine bugs still crash
loudly with full context.
"""

from __future__ import annotations


class SwayError(Exception):
    """Base class for all expected, user-facing Sway failures."""


class ConfigError(SwayError):
    """Configuration file, environment variable, or CLI value was invalid."""


class LatencyStoreError(SwayError):
    """The latency database could not be opened, migrated, or queried."""


class DictionaryStoreError(SwayError):
    """The personal dictionary database could not be opened, migrated, or queried."""


class HistoryStoreError(SwayError):
    """The dictation history database could not be opened, migrated, or queried."""


class AudioError(SwayError):
    """The microphone input stream could not be opened or failed."""


class ClipboardError(SwayError):
    """The OS clipboard could not be read or written."""


class PasteBlockedError(SwayError):
    """A synthetic paste would silently be dropped by the OS (e.g. the
    focused window runs at a higher privilege level than Sway itself)."""


__all__ = [
    "AudioError",
    "ClipboardError",
    "ConfigError",
    "DictionaryStoreError",
    "SwayError",
    "HistoryStoreError",
    "LatencyStoreError",
    "PasteBlockedError",
]
