"""Reads the current text selection, for Phase 5 voice commands.

There is no cross-platform "get the selected text" API without an OS
accessibility layer (UIAutomation on Windows, most apps don't expose it
fully). Every dictation/snippet tool uses the same trick instead: send the OS
copy shortcut and read what landed on the clipboard. The original clipboard
is saved and restored around it, mirroring :class:`~flow.inject.TextInjector`'s
paste dance in the opposite direction.
"""

from __future__ import annotations

import logging
import time

from flow.errors import FlowError
from flow.platform import PlatformAdapter

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 0.3
DEFAULT_POLL_INTERVAL_S = 0.02

#: Written to the clipboard right before Ctrl+C so a real change can be
#: detected by polling, rather than trusting a fixed delay: something no
#: application would ever legitimately have copied.
_SENTINEL = "\x00flow-selection-sentinel\x00"


class SelectionReader:
    """Best-effort: never raises. Returns "" if nothing was selected, the
    copy timed out, or the clipboard could not be read/written at all.
    """

    def __init__(
        self,
        platform: PlatformAdapter,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        self._platform = platform
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s

    def read(self) -> str:
        try:
            previous = self._platform.clipboard_get()
        except FlowError:
            logger.warning("could not save the clipboard before reading a selection")
            previous = None

        try:
            self._platform.clipboard_set(_SENTINEL)
        except FlowError:
            logger.warning("could not clear the clipboard before reading a selection")
            return ""

        self._platform.send_copy()
        selected = self._poll_for_change()

        if previous is not None:
            try:
                self._platform.clipboard_set(previous)
            except FlowError:
                logger.warning("could not restore the clipboard after reading a selection")

        return selected

    def _poll_for_change(self) -> str:
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                current = self._platform.clipboard_get()
            except FlowError:
                return ""
            if current != _SENTINEL:
                return current
            if time.monotonic() >= deadline:
                return ""  # nothing was selected (or the app never responded to Ctrl+C)
            time.sleep(self._poll_interval_s)


__all__ = ["DEFAULT_POLL_INTERVAL_S", "DEFAULT_TIMEOUT_S", "SelectionReader"]
