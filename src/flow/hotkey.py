"""Global hotkey: hold a key to record.

Uses ``pynput``, not the ``keyboard`` package, because ``keyboard`` requires
administrator privileges on Windows (CLAUDE.md).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from pynput import keyboard

logger = logging.getLogger(__name__)

#: Recognised hotkey names for [hotkey].key in config.toml. Mirrored (and
#: checked for drift by a test) in flow.config, which validates the string
#: without importing pynput.
KNOWN_KEYS: dict[str, keyboard.Key] = {
    "ctrl_r": keyboard.Key.ctrl_r,
    "ctrl_l": keyboard.Key.ctrl_l,
    "alt_r": keyboard.Key.alt_r,
    "alt_l": keyboard.Key.alt_l,
    "shift_r": keyboard.Key.shift_r,
    "shift_l": keyboard.Key.shift_l,
    "cmd": keyboard.Key.cmd,
    "caps_lock": keyboard.Key.caps_lock,
    "f13": keyboard.Key.f13,
    "f14": keyboard.Key.f14,
    "f15": keyboard.Key.f15,
    "f16": keyboard.Key.f16,
    "f17": keyboard.Key.f17,
    "f18": keyboard.Key.f18,
}


class KeyHoldTracker:
    """Debounces OS key-repeat: press()/release() each fire once per physical hold.

    Windows re-fires ``on_press`` repeatedly while a key is held (OS key
    repeat); without this, every repeat would restart the recording.
    """

    __slots__ = ("_held",)

    def __init__(self) -> None:
        self._held = False

    def press(self) -> bool:
        """Returns True the first time a hold starts, False for repeats."""
        if self._held:
            return False
        self._held = True
        return True

    def release(self) -> bool:
        """Returns True for a genuine release, False if nothing was held."""
        if not self._held:
            return False
        self._held = False
        return True


class HotkeyListener:
    """Wraps a pynput global listener around a single held key."""

    def __init__(
        self,
        key: keyboard.Key,
        *,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self._key = key
        self._on_press = on_press
        self._on_release = on_release
        self._tracker = KeyHoldTracker()
        self._listener: keyboard.Listener | None = None

    @property
    def is_running(self) -> bool:
        return self._listener is not None

    def start(self) -> None:
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._handle_press, on_release=self._handle_release
        )
        self._listener.start()

    def stop(self) -> None:
        if self._listener is None:
            return
        listener, self._listener = self._listener, None
        listener.stop()

    def _handle_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key != self._key or not self._tracker.press():
            return
        self._safe_call(self._on_press)

    def _handle_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        if key != self._key or not self._tracker.release():
            return
        self._safe_call(self._on_release)

    def _safe_call(self, fn: Callable[[], None]) -> None:
        # pynput silently kills its listener thread if a callback raises;
        # swallowing here keeps the hotkey alive for the next attempt
        # instead of leaving the app looking hung.
        try:
            fn()
        except Exception:
            logger.exception("hotkey callback raised")


__all__ = ["HotkeyListener", "KeyHoldTracker", "KNOWN_KEYS"]
