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

#: pynput key names are not user-facing text. The bar and the window both
#: show the hotkey, so the mapping lives here rather than in either of them.
_PRETTY_KEYS = {
    "ctrl_r": "Right Ctrl",
    "ctrl_l": "Left Ctrl",
    "shift_r": "Right Shift",
    "shift_l": "Left Shift",
    "alt_r": "Right Alt",
    "alt_l": "Left Alt",
    "cmd": "Win",
    "caps_lock": "Caps Lock",
}


def prettify_hotkey(name: str) -> str:
    """``ctrl_r`` -> ``Right Ctrl``. Unknown names are returned unchanged, so
    a key added to KNOWN_KEYS without a label here still displays."""
    return _PRETTY_KEYS.get(str(name).lower(), str(name))



# ---------------------------------------------------------------------------
# Chords: tap a modifier combination to fire an action
#
# Separate from HotkeyListener on purpose. Dictation is *hold to record* -- it
# needs press and release, and it needs exactly one key so the key can be held
# down for seconds without anything else reacting. A transform shortcut is *tap
# to fire*: it needs modifiers, it needs many bindings at once, and it does not
# care about release. Bolting the second onto the first would put modifier-state
# bookkeeping on the path that records every dictation, for no benefit there.
# ---------------------------------------------------------------------------

#: Modifier names accepted in a chord, mapped to the pynput keys that satisfy
#: them. Left and right count as the same modifier: nobody means "the right
#: Ctrl specifically" when they write ctrl+alt+p.
CHORD_MODIFIERS: dict[str, tuple[keyboard.Key, ...]] = {
    "ctrl": (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r),
    "alt": (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr),
    "shift": (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r),
    "cmd": (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r),
}

#: Named non-character keys a chord may end on, beyond a plain letter or digit.
CHORD_KEYS: dict[str, keyboard.Key] = {
    f"f{n}": getattr(keyboard.Key, f"f{n}") for n in range(1, 21)
} | {
    "space": keyboard.Key.space,
    "enter": keyboard.Key.enter,
    "tab": keyboard.Key.tab,
    "backspace": keyboard.Key.backspace,
    "delete": keyboard.Key.delete,
    "home": keyboard.Key.home,
    "end": keyboard.Key.end,
    "insert": keyboard.Key.insert,
}


class ChordError(ValueError):
    """A shortcut string that cannot be bound."""


class Chord:
    """One parsed modifier combination, e.g. ``ctrl+alt+p``.

    Value-like: two chords written differently ("Ctrl+Alt+P", "alt+ctrl+p")
    compare equal and hash the same, so conflict detection is a set lookup
    rather than string juggling.
    """

    __slots__ = ("_key", "_modifiers")

    def __init__(self, modifiers: frozenset[str], key: str) -> None:
        self._modifiers = modifiers
        self._key = key

    @property
    def modifiers(self) -> frozenset[str]:
        return self._modifiers

    @property
    def key(self) -> str:
        return self._key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Chord):
            return NotImplemented
        return self._modifiers == other._modifiers and self._key == other._key

    def __hash__(self) -> int:
        return hash((self._modifiers, self._key))

    def __str__(self) -> str:
        # Canonical order, so a chord reads the same everywhere it is shown --
        # the list, the editor, and a conflict message.
        order = [m for m in ("ctrl", "alt", "shift", "cmd") if m in self._modifiers]
        return "+".join([*order, self._key])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Chord({str(self)!r})"

    def pretty(self) -> str:
        """How the chord is shown to a person."""
        labels = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
        order = [labels[m] for m in ("ctrl", "alt", "shift", "cmd") if m in self._modifiers]
        key = self._key.upper() if len(self._key) == 1 else self._key.capitalize()
        return " + ".join([*order, key])


def parse_chord(shortcut: str) -> Chord:
    """Parse ``"ctrl+alt+p"``. Raises :class:`ChordError` with a reason a user
    can act on.

    At least one modifier is required. A bare letter would fire in the middle
    of anything the user typed, which is not a shortcut -- it is a broken
    keyboard.
    """
    parts = [p.strip().lower() for p in shortcut.split("+") if p.strip()]
    if not parts:
        raise ChordError("that shortcut is empty")

    *modifier_names, key = parts
    unknown = [m for m in modifier_names if m not in CHORD_MODIFIERS]
    if unknown:
        raise ChordError(f"{unknown[0]!r} is not a modifier -- use ctrl, alt, shift, or cmd")
    if not modifier_names:
        raise ChordError("a shortcut needs at least one modifier, like Ctrl+Alt+P")
    if key in CHORD_MODIFIERS:
        raise ChordError("a shortcut needs a key as well as its modifiers")
    if key not in CHORD_KEYS and not (len(key) == 1 and key.isalnum()):
        raise ChordError(f"{key!r} cannot be used as a shortcut key")
    return Chord(frozenset(modifier_names), key)


def format_chord(shortcut: str) -> str:
    """Canonical string form, or the input unchanged if it will not parse.

    For display paths where a bad value must not raise -- a settings file can
    hold anything.
    """
    try:
        return str(parse_chord(shortcut))
    except ChordError:
        return shortcut


class ChordListener:
    """One pynput listener dispatching many chords to their handlers.

    One OS hook for every shortcut, not one per binding: each listener is a
    thread and a hook, and a user with eight transforms bound should not pay
    for eight of them.

    Handlers run on the listener thread, so one that blocks delays every
    subsequent key. Callers hand off to a worker, exactly as the dictation
    hotkey does.
    """

    def __init__(self) -> None:
        self._bindings: dict[Chord, Callable[[], None]] = {}
        self._down: set[str] = set()
        self._listener: keyboard.Listener | None = None

    @property
    def is_running(self) -> bool:
        return self._listener is not None

    @property
    def bindings(self) -> dict[Chord, Callable[[], None]]:
        return dict(self._bindings)

    def bind(self, chord: Chord, handler: Callable[[], None]) -> None:
        self._bindings[chord] = handler

    def rebind(self, bindings: dict[Chord, Callable[[], None]]) -> None:
        """Replace every binding at once.

        Lets a settings change take effect without tearing the OS hook down
        and putting it back, which on Windows can drop keystrokes in between.
        """
        self._bindings = dict(bindings)

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
        self._down.clear()
        listener.stop()

    def _handle_press(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        modifier = _modifier_name(key)
        if modifier is not None:
            self._down.add(modifier)
            return
        name = _key_name(key)
        if name is None or not self._down:
            return
        handler = self._bindings.get(Chord(frozenset(self._down), name))
        if handler is None:
            return
        try:
            handler()
        except Exception:
            # A handler that raises must not take the listener thread down with
            # it -- that would silently kill every other shortcut as well.
            logger.exception("shortcut handler failed")

    def _handle_release(self, key: keyboard.Key | keyboard.KeyCode | None) -> None:
        modifier = _modifier_name(key)
        if modifier is not None:
            self._down.discard(modifier)


def _modifier_name(key: object) -> str | None:
    for name, variants in CHORD_MODIFIERS.items():
        if key in variants:
            return name
    return None


def _key_name(key: object) -> str | None:
    """The chord-key name for a pressed key, or ``None`` if it cannot be one.

    ``KeyCode.char`` is ``None`` for a key with no character, and on most
    layouts is the *unmodified* character -- which is what we want, since a
    chord is written as the letter on the keycap.
    """
    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1 and char.isalnum():
        return char.lower()
    for name, known in CHORD_KEYS.items():
        if key is known:
            return name
    return None


__all__ = [
    "CHORD_KEYS",
    "CHORD_MODIFIERS",
    "KNOWN_KEYS",
    "Chord",
    "ChordError",
    "ChordListener",
    "HotkeyListener",
    "KeyHoldTracker",
    "format_chord",
    "parse_chord",
    "prettify_hotkey",
]
