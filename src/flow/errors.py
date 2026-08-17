"""Exception hierarchy for Flow.

Everything raised on purpose derives from :class:`FlowError` so the CLI can
render a short message instead of a traceback, while genuine bugs still crash
loudly with full context.
"""

from __future__ import annotations


class FlowError(Exception):
    """Base class for all expected, user-facing Flow failures."""


class ConfigError(FlowError):
    """Configuration file, environment variable, or CLI value was invalid."""


class LatencyStoreError(FlowError):
    """The latency database could not be opened, migrated, or queried."""


class DictionaryStoreError(FlowError):
    """The personal dictionary database could not be opened, migrated, or queried."""


class HistoryStoreError(FlowError):
    """The dictation history database could not be opened, migrated, or queried."""


class AudioError(FlowError):
    """The microphone input stream could not be opened or failed."""


class ClipboardError(FlowError):
    """The OS clipboard could not be read or written."""


class PasteBlockedError(FlowError):
    """A synthetic paste would silently be dropped by the OS (e.g. the
    focused window runs at a higher privilege level than Flow itself)."""


__all__ = [
    "AudioError",
    "ClipboardError",
    "ConfigError",
    "DictionaryStoreError",
    "FlowError",
    "HistoryStoreError",
    "LatencyStoreError",
    "PasteBlockedError",
]
