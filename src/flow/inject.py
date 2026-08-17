"""Paste-based text injection.

CLAUDE.md: save the existing clipboard, set ours, send Ctrl+V, restore the
original clipboard after 150ms. Never simulate typing — keystroke simulation
is unreliable across apps with input masking/formatting/IME handling, and
every fast dictation tool (including the Wispr Flow benchmark) pastes
instead.
"""

from __future__ import annotations

import logging
import threading

from flow.errors import FlowError, PasteBlockedError
from flow.platform import PlatformAdapter

logger = logging.getLogger(__name__)

DEFAULT_RESTORE_DELAY_MS = 150


class TextInjector:
    def __init__(self, platform: PlatformAdapter, *, restore_delay_ms: int = DEFAULT_RESTORE_DELAY_MS) -> None:
        if restore_delay_ms < 0:
            raise ValueError("restore_delay_ms must be >= 0")
        self._platform = platform
        self._delay_s = restore_delay_ms / 1000.0

    def inject(self, text: str) -> None:
        """Paste ``text`` at the cursor. Schedules the clipboard restore and returns.

        Raises :class:`~flow.errors.PasteBlockedError` -- without touching
        the clipboard at all -- when the platform is confident the paste
        would silently do nothing (see
        :meth:`~flow.platform.PlatformAdapter.paste_likely_blocked`), so
        callers can fall back to something the user can act on instead of
        clobbering their clipboard for nothing.
        """
        if not text:
            return
        if self._platform.paste_likely_blocked():
            raise PasteBlockedError(
                "the focused window appears to run at a higher privilege level; "
                "a synthetic paste would silently be dropped"
            )
        try:
            previous = self._platform.clipboard_get()
        except FlowError:
            logger.warning("could not read the clipboard to save it; pasting anyway")
            previous = None

        self._platform.clipboard_set(text)
        self._platform.send_paste()

        # A background timer, not time.sleep: this method must not block the
        # caller (the dictation worker thread) for the restore delay.
        timer = threading.Timer(self._delay_s, self._restore, args=(previous, text))
        timer.daemon = True
        timer.start()

    def _restore(self, previous: str | None, injected: str) -> None:
        """Put the user's clipboard back, unless it stopped being ours.

        Between the paste and this callback the user may have copied
        something themselves, or the target app may have taken clipboard
        ownership. Restoring blindly in that window would silently destroy
        whatever they just copied, so the restore only proceeds while the
        dictated text is still what's on the clipboard.
        """
        if previous is None:
            return
        try:
            current = self._platform.clipboard_get()
        except FlowError:
            logger.debug("could not read the clipboard before restoring it; restoring anyway")
        else:
            if current != injected:
                logger.debug(
                    "clipboard changed since the paste; leaving the newer contents alone"
                )
                return
        try:
            self._platform.clipboard_set(previous)
        except FlowError:
            logger.warning("could not restore the original clipboard contents")


__all__ = ["DEFAULT_RESTORE_DELAY_MS", "TextInjector"]
