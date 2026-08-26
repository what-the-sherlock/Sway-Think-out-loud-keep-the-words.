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
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

from sway.errors import SwayError
from sway.platform import PlatformAdapter

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 0.3
DEFAULT_POLL_INTERVAL_S = 0.02

#: Modifiers that corrupt the synthetic Ctrl+C if the user is still
#: physically holding them when it is sent. Ctrl is deliberately absent:
#: Ctrl+C with Ctrl already down is still Ctrl+C, so waiting for it would
#: cost latency for nothing. Each of the rest turns the chord into something
#: else entirely -- Ctrl+Shift+C is devtools in every browser and "new
#: terminal" in VS Code, Ctrl+Alt+C is an AltGr composition on most layouts
#: (and Ctrl+Alt is what every shipped transform shortcut uses), Win+Ctrl+C
#: is a Windows accessibility toggle. None of them copy, so the sentinel
#: below never changes and the read reports nothing was selected.
_BLOCKING_MODIFIERS = frozenset({"shift_l", "shift_r", "alt_l", "alt_r", "cmd"})

#: How long read() will spend, in total across *both* attempts, waiting for
#: those to clear. Shared rather than per-attempt: attempt 2 already happens
#: a full poll timeout after attempt 1, by which time the user has almost
#: always let go, and re-paying the wait would push read() past the caller's
#: own deadline for it -- see selection_budget_s.
DEFAULT_MODIFIER_WAIT_S = 0.45

#: Paid once, only after a wait that actually happened, so the target has
#: processed the key-ups before Ctrl+C arrives behind them. Windows
#: serialises SendInput with real hardware input, so ordering is already
#: guaranteed in principle; this covers targets that route input
#: asynchronously across processes (Chromium/Electron, some Java toolkits)
#: and can be a frame behind on modifier state. Skipped entirely when
#: nothing was held, so the common path and a mouse-triggered transform pay
#: nothing at all.
_SETTLE_AFTER_RELEASE_S = 0.04

#: Slack folded into selection_budget_s for the clipboard round trips,
#: whose _open_clipboard retries 8x20ms per open when another app holds it.
_CLIPBOARD_SLACK_S = 0.4

#: Written to the clipboard right before Ctrl+C so a real change can be
#: detected by polling, rather than trusting a fixed delay: something no
#: application would ever legitimately have copied.
_SENTINEL = "\x00flow-selection-sentinel\x00"


def selection_budget_s(
    timeout_s: float, *, modifier_wait_s: float = DEFAULT_MODIFIER_WAIT_S
) -> float:
    """How long a caller must allow for one :meth:`SelectionReader.read`.

    ``read()``'s worst case is one shared modifier wait, a settle delay,
    then two full poll timeouts, plus the clipboard round trips in between.
    A caller (see ``sway.engine``) previously hard-coded ``timeout_s + 1.0``,
    which covered the two polls only by coincidence and would have stopped
    covering them the moment a wait was added in front of them -- the number
    belongs here, beside the constants it is made of.
    """
    return modifier_wait_s + _SETTLE_AFTER_RELEASE_S + 2 * timeout_s + _CLIPBOARD_SLACK_S


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
        suspend_input: Callable[[], AbstractContextManager[None]] = nullcontext,
        modifier_probe: Callable[[], frozenset[str] | None] | None = None,
        modifier_wait_s: float = DEFAULT_MODIFIER_WAIT_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
        if modifier_wait_s < 0:
            raise ValueError("modifier_wait_s must be >= 0")
        self._platform = platform
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        #: Wraps the synthetic Ctrl+C below so Sway's own global binding
        #: listener (see sway.hotkey.BindingListener.suspended) doesn't
        #: mistake it for a real keystroke. Defaults to a no-op so this
        #: module stays decoupled from hotkey.py -- it only knows about
        #: PlatformAdapter, same as before.
        self._suspend_input = suspend_input
        #: Best-effort OS query for what is *physically* held right now (see
        #: PlatformAdapter.modifier_keys_down), injected rather than taken
        #: off `platform` directly so this module keeps working against any
        #: adapter that predates it, and so a test can drive it without
        #: implementing the whole method. None means "never wait" -- the
        #: behaviour this class had before the wait existed at all.
        self._modifier_probe = modifier_probe
        self._modifier_wait_s = modifier_wait_s

    def read(self) -> str:
        # A confidently-elevated foreground window blocks synthetic input
        # the same way it blocks a synthetic paste (Windows' UIPI is not
        # asymmetric about it) -- checked up front so a doomed attempt
        # fails fast with a clear reason instead of spending the full
        # poll timeout (twice, once per attempt below) finding out.
        if self._platform.synthetic_input_likely_blocked():
            logger.info(
                "the focused window appears to run at a higher privilege level; "
                "not attempting to read its selection"
            )
            return ""

        try:
            previous = self._platform.clipboard_get()
        except SwayError:
            logger.warning("could not save the clipboard before reading a selection")
            previous = None

        # ONE budget for the whole read, not one per attempt -- see
        # DEFAULT_MODIFIER_WAIT_S. Computed once so attempt 2 cannot re-pay
        # a wait that attempt 1 already exhausted.
        modifier_deadline = time.monotonic() + self._modifier_wait_s

        selected = self._try_copy(1, modifier_deadline)
        if not selected:
            # The retry is no longer just a timing hope: attempt 1 may have
            # spent the whole modifier budget and sent the copy anyway, and
            # by now the user has had a further timeout_s to let go.
            selected = self._try_copy(2, modifier_deadline)
            if selected:
                logger.info("read the selection on the second copy attempt")
            else:
                logger.warning(
                    "no selection read: two copy attempts left the clipboard "
                    "untouched (interfering modifiers still held: %s)",
                    sorted(self._blocking_now()) or "none",
                )

        if previous is not None:
            try:
                self._platform.clipboard_set(previous)
            except SwayError:
                logger.warning("could not restore the clipboard after reading a selection")

        return selected

    def _try_copy(self, attempt: int, modifier_deadline: float) -> str:
        """One sentinel-write-copy-poll attempt. "" on any failure."""
        try:
            self._platform.clipboard_set(_SENTINEL)
        except SwayError:
            logger.warning("could not clear the clipboard before reading a selection")
            return ""

        self._wait_for_modifiers(modifier_deadline, attempt=attempt)

        started = time.monotonic()
        with self._suspend_input():
            self._platform.send_copy()
        text = self._poll_for_change()
        if not text:
            logger.info(
                "copy attempt %d found nothing selected after %.0fms",
                attempt,
                (time.monotonic() - started) * 1000.0,
            )
        return text

    def _wait_for_modifiers(self, deadline: float, *, attempt: int) -> None:
        """Block until no interfering modifier is physically held, or the
        shared budget runs out.

        Returns immediately -- one cheap probe, no sleep, no settle delay --
        when nothing interfering is down, which is every mouse-triggered
        transform and every user who has already let go. The wait only ever
        costs anything on the path that would otherwise silently fail.
        """
        if self._modifier_probe is None:
            return
        held = self._blocking_now()
        if not held:
            return
        logger.info(
            "copy attempt %d: waiting for held modifiers to clear first: %s",
            attempt,
            sorted(held),
        )
        started = time.monotonic()
        while time.monotonic() < deadline:
            time.sleep(self._poll_interval_s)
            held = self._blocking_now()
            if not held:
                logger.info(
                    "copy attempt %d: modifiers cleared after %.0fms",
                    attempt,
                    (time.monotonic() - started) * 1000.0,
                )
                time.sleep(_SETTLE_AFTER_RELEASE_S)
                return
        logger.warning(
            "copy attempt %d: %s still held after %.0fms; sending the copy "
            "shortcut anyway, the focused app will most likely read it as a "
            "different shortcut",
            attempt,
            sorted(held),
            (time.monotonic() - started) * 1000.0,
        )

    def _blocking_now(self) -> frozenset[str]:
        """The interfering modifiers currently held. Empty when no probe was
        supplied, when the platform cannot answer, or when the probe fails --
        an unreadable key state is never a reason to refuse to try the copy.
        """
        if self._modifier_probe is None:
            return frozenset()
        try:
            down = self._modifier_probe()
        except Exception:
            logger.debug("could not read modifier key state", exc_info=True)
            return frozenset()
        if down is None:
            return frozenset()
        return frozenset(down) & _BLOCKING_MODIFIERS

    def _poll_for_change(self) -> str:
        deadline = time.monotonic() + self._timeout_s
        while True:
            try:
                current = self._platform.clipboard_get()
            except SwayError:
                return ""
            if current != _SENTINEL:
                return current
            if time.monotonic() >= deadline:
                return ""  # nothing was selected (or the app never responded to Ctrl+C)
            time.sleep(self._poll_interval_s)


__all__ = [
    "DEFAULT_MODIFIER_WAIT_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
    "SelectionReader",
    "selection_budget_s",
]
