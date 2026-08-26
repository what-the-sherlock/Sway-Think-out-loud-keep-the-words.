"""Global bindings: hold, tap, or click something to fire an action.

Uses ``pynput``, not the ``keyboard`` package, because ``keyboard`` requires
administrator privileges on Windows (CLAUDE.md). ``pynput.mouse`` is the same
already-vendored dependency as ``pynput.keyboard`` -- binding a mouse button
adds no new package.

A :class:`Binding` is one physical gesture a user can press: a single held
key (``ctrl_r``), two or more modifiers held together with nothing else
(``ctrl+cmd``), a modifier chord ending in a key (``ctrl+alt+p``), a mouse
button alone (``mouse:middle``), or a chord ending in a mouse button
(``ctrl+mouse:back``). Its shape -- ``"hold"`` or ``"tap"`` -- is derived from
that shorthand rather than configured separately, which is what collapses the
old "one primary hold key plus one optional alternate chord" pair into a
single list an action can hold any number of bindings in.

:class:`BindingListener` is the one OS-level hook (one keyboard listener, one
mouse listener) that turns physical press/release events into two abstract
signals per registered binding: *engaged* (every required member just became
held at once) and *disengaged* (a required member let go after that). What
those signals *mean* is entirely up to the caller -- dictation and the voice
command wire both to get genuine hold-to-talk; hands-free and every transform
wire only ``engaged``, because a toggle and an instant action have no "stop"
half.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from pynput import keyboard, mouse

logger = logging.getLogger(__name__)

#: Recognised single-key hold names. Mirrored (and checked for drift by a
#: test) in sway.config, which validates the string without importing pynput.
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

#: Modifier names a binding may use, mapped to the pynput keys that satisfy
#: them. Left and right count as the same modifier: nobody means "the right
#: Ctrl specifically" when they write ctrl+alt+p.
MODIFIER_KEYS: dict[str, tuple[keyboard.Key, ...]] = {
    "ctrl": (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r),
    "alt": (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r, keyboard.Key.alt_gr),
    "shift": (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r),
    "cmd": (keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r),
}
MODIFIER_NAMES = frozenset(MODIFIER_KEYS)

#: Named non-character keys a binding may end on, beyond a plain letter or digit.
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

#: Mouse buttons that may be bound, written ``mouse:<name>``. Left and right
#: are deliberately absent -- binding either would break ordinary clicking,
#: which is the one thing every app needs the mouse to keep doing.
MOUSE_BUTTONS: dict[str, mouse.Button] = {
    "middle": mouse.Button.middle,
    "back": mouse.Button.x1,
    "forward": mouse.Button.x2,
}
#: Rejected by name specifically, so the error names the reason rather than
#: just "not a mouse button Sway can bind".
_UNBINDABLE_MOUSE_BUTTONS = frozenset({"left", "right"})

#: Bindings that must never be assignable, and why. Checked against a
#: binding's canonical string, so it catches both a plain chord and (in
#: principle) any future terminal type that happens to collide.
#:
#: ctrl+c / ctrl+v / ctrl+x are Sway's own internal keystrokes -- selection
#: capture sends a synthetic Ctrl+C (see sway.selection) and paste injection
#: sends a synthetic Ctrl+V. Binding either as an activation chord would mean
#: Sway's own clipboard round-trip could re-trigger the very shortcut it is
#: mid-flight on.
#:
#: ctrl+alt+delete is trapped by the OS below any user-mode hook and pynput
#: never sees it; cmd+l is Windows' lock-screen shortcut and fires
#: regardless of what else is bound to it. Both are listed anyway so the
#: rejection message is specific rather than the shortcut silently never
#: firing.
_RESERVED_BINDINGS: dict[str, str] = {
    "ctrl+c": "used internally to read a selection -- pick something else",
    "ctrl+v": "used internally to paste -- pick something else",
    "ctrl+x": "used internally to read a selection -- pick something else",
    "ctrl+alt+delete": "reserved by Windows and can't be used here",
    "cmd+l": "reserved by Windows (locks the screen) and can't be used here",
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


class TapModeTracker:
    """Pure state machine for a tap-to-start/tap-to-stop (toggle) binding.

    No pynput, no threads, no ``time.monotonic()`` calls of its own -- the
    caller passes ``now``, so this is trivially testable and reused for both
    hands-free dictation and a tap-shaped alternate binding for dictation or
    the voice command (a tap fires once per engage, so it is honestly a
    toggle gesture rather than a hold, unlike a binding's primary hold shape).
    """

    __slots__ = ("_active", "_max_active_s", "_started_at")

    def __init__(self, *, max_active_s: float) -> None:
        self._active = False
        self._started_at: float | None = None
        self._max_active_s = max_active_s

    @property
    def active(self) -> bool:
        return self._active

    def tap(self, now: float) -> str:
        """Advance the state machine on one tap.

        Returns ``"start"`` (a session just began), ``"stop"`` (a session
        just ended normally), or ``"stop_timeout"`` (this tap arrived after
        the active session had already run past ``max_active_s`` -- the
        caller treats that session as force-closed rather than starting a
        new one on the same tap; a genuinely new session needs its own,
        separate tap).
        """
        if self._active and self._started_at is not None and now - self._started_at > self._max_active_s:
            self._active = False
            self._started_at = None
            return "stop_timeout"
        if self._active:
            self._active = False
            self._started_at = None
            return "stop"
        self._active = True
        self._started_at = now
        return "start"

    def check_timeout(self, now: float) -> bool:
        """Force-close a session that has run past ``max_active_s``.

        Called by a periodic safety timer, not just from ``tap()`` -- a
        session must not be able to run forever if the stop tap is
        physically missed. Returns whether a session was just force-closed;
        idempotent (a second call after that returns ``False``).
        """
        if self._active and self._started_at is not None and now - self._started_at > self._max_active_s:
            self._active = False
            self._started_at = None
            return True
        return False


class RefCountedGate:
    """Turns any number of independent "engaged"/"disengaged" signals for one
    action into a single start/stop pair.

    Why this exists: an action (dictation, the voice command) can now hold
    several hold-shaped bindings at once -- e.g. both ``ctrl_r`` and
    ``ctrl+cmd``. If a user's grip happens to satisfy both simultaneously,
    each binding fires its own engaged/disengaged pair independently. Without
    this, a second binding's engage while the first is already held would
    restart the session's bookkeeping (a fresh start frame, a fresh press
    time) mid-recording, silently truncating what the user already said; its
    disengage while the other binding is still held would end the session
    out from under them. Routing every binding's signal through one shared
    gate collapses that into "engaged" firing only on the 0->1 transition and
    "disengaged" only on the 1->0 transition, so the actual start/stop
    handlers run exactly once no matter how many of the action's bindings are
    held at once, in whatever order they happened to be pressed and released.

    Pure counting, no pynput, no threads -- trivially testable and reused for
    both dictation and the voice command.
    """

    __slots__ = ("_count",)

    def __init__(self) -> None:
        self._count = 0

    @property
    def active(self) -> bool:
        return self._count > 0

    def acquire(self) -> bool:
        """One binding engaged. Returns ``True`` on the 0->1 transition --
        the caller should run its "start" logic only then."""
        self._count += 1
        return self._count == 1

    def release(self) -> bool:
        """One binding disengaged. Returns ``True`` on the 1->0 transition --
        the caller should run its "stop" logic only then. A release with
        nothing held (``_count`` already 0) is a no-op, not a negative
        count -- mirroring :class:`KeyHoldTracker`'s own defensiveness
        against a release with no matching press."""
        if self._count == 0:
            return False
        self._count -= 1
        return self._count == 0


class DoubleTapTracker:
    """Pure logic: two taps of the same binding within ``window_s`` toggle a
    session; a lone tap with nothing following it within the window does
    nothing and is not itself treated as an activation.

    Composes :class:`TapModeTracker` for the actual start/stop/timeout
    bookkeeping rather than duplicating it -- double-tap only changes *how*
    a qualifying tap is recognised, not what happens once it is.
    """

    __slots__ = ("_last_tap_at", "_mode", "_window_s")

    def __init__(self, *, max_active_s: float, window_s: float = 0.35) -> None:
        self._mode = TapModeTracker(max_active_s=max_active_s)
        self._window_s = window_s
        self._last_tap_at: float | None = None

    @property
    def active(self) -> bool:
        return self._mode.active

    def tap(self, now: float) -> str | None:
        """Register one physical tap.

        Returns the underlying :meth:`TapModeTracker.tap` outcome on a
        *qualifying* second tap (one that lands within ``window_s`` of the
        previous tap), or ``None`` otherwise -- a lone tap, or a second tap
        that arrived too late and is instead treated as a fresh potential
        first tap (recorded via ``_last_tap_at``, never silently dropped).
        """
        previous = self._last_tap_at
        self._last_tap_at = now
        if previous is None or (now - previous) > self._window_s:
            return None
        # A qualifying pair: consume it so a third, unrelated tap does not
        # pair with this one again.
        self._last_tap_at = None
        return self._mode.tap(now)

    def check_timeout(self, now: float) -> bool:
        return self._mode.check_timeout(now)


#: pynput key names are not user-facing text. The bar and the window both
#: show a binding, so the mapping lives here rather than in either of them.
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

_PRETTY_MOUSE = {
    "middle": "Middle Click",
    "back": "Back Button",
    "forward": "Forward Button",
}

_MODIFIER_LABELS = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
_MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")


def prettify_hotkey(name: str) -> str:
    """``ctrl_r`` -> ``Right Ctrl``. Unknown names are returned unchanged, so
    a key added to KNOWN_KEYS without a label here still displays."""
    return _PRETTY_KEYS.get(str(name).lower(), str(name))


class BindingError(ValueError):
    """A binding string that cannot be bound."""


class Binding:
    """One parsed physical gesture: some modifiers, and at most one of a
    terminal key or a mouse button.

    Value-like: two bindings written differently ("Ctrl+Alt+P",
    "alt+ctrl+p") compare equal and hash the same, so conflict detection is a
    set lookup rather than string juggling.
    """

    __slots__ = ("_key", "_modifiers", "_mouse")

    def __init__(self, modifiers: frozenset[str], key: str = "", mouse: str = "") -> None:
        self._modifiers = modifiers
        self._key = key
        self._mouse = mouse

    @property
    def modifiers(self) -> frozenset[str]:
        return self._modifiers

    @property
    def key(self) -> str:
        return self._key

    @property
    def mouse(self) -> str:
        return self._mouse

    @property
    def shape(self) -> str:
        """``"hold"`` or ``"tap"``.

        A binding is tap-shaped only when it has a terminal member (a key or
        a mouse button) *and* at least one modifier -- that is the only shape
        with nothing to hold: the modifiers are a prefix, and the terminal
        member fires once. Every other shape -- a bare single held key, two
        or more modifiers with no terminal, or a bare mouse button -- is
        something a person holds down for a duration, so it is hold-shaped.
        """
        if (self._key or self._mouse) and self._modifiers:
            return "tap"
        return "hold"

    @property
    def is_modifier_combo(self) -> bool:
        """True for a bare "two or more modifiers, nothing else" binding.

        The only shape that can collide with a tap binding sharing the same
        modifier prefix -- see :class:`ModifierHoldGate`.
        """
        return bool(self._modifiers) and not self._key and not self._mouse

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Binding):
            return NotImplemented
        return (
            self._modifiers == other._modifiers
            and self._key == other._key
            and self._mouse == other._mouse
        )

    def __hash__(self) -> int:
        return hash((self._modifiers, self._key, self._mouse))

    def __str__(self) -> str:
        order = [m for m in _MODIFIER_ORDER if m in self._modifiers]
        tail = [f"mouse:{self._mouse}"] if self._mouse else ([self._key] if self._key else [])
        return "+".join([*order, *tail])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Binding({str(self)!r})"

    def pretty(self) -> str:
        """How the binding is shown to a person."""
        order = [_MODIFIER_LABELS[m] for m in _MODIFIER_ORDER if m in self._modifiers]
        if self._mouse:
            tail = [_PRETTY_MOUSE.get(self._mouse, self._mouse.capitalize())]
        elif self._key:
            if self._key in _PRETTY_KEYS:
                tail = [_PRETTY_KEYS[self._key]]
            else:
                tail = [self._key.upper() if len(self._key) == 1 else self._key.capitalize()]
        else:
            tail = []
        return " + ".join([*order, *tail])


def _reject_if_reserved(binding: Binding) -> None:
    reason = _RESERVED_BINDINGS.get(str(binding))
    if reason is not None:
        raise BindingError(f"{binding.pretty()} is {reason}")


def _parse_mouse_terminal(modifiers: frozenset[str], name: str) -> Binding:
    if name in _UNBINDABLE_MOUSE_BUTTONS:
        raise BindingError(f"the {name} mouse button can't be bound -- it's needed for clicking")
    if name not in MOUSE_BUTTONS:
        raise BindingError(f"{name!r} is not a mouse button Sway can bind -- use middle, back, or forward")
    binding = Binding(modifiers, mouse=name)
    _reject_if_reserved(binding)
    return binding


def parse_binding(text: str) -> Binding:
    """Parse a binding string. Raises :class:`BindingError` with a reason a
    user can act on.

    Accepted shapes:

    * A bare hold-key name, e.g. ``"ctrl_r"`` (see :data:`KNOWN_KEYS`).
    * A bare mouse button, e.g. ``"mouse:middle"``.
    * Two or more modifiers alone, e.g. ``"ctrl+cmd"``.
    * One or more modifiers plus a terminal key or mouse button, e.g.
      ``"ctrl+alt+p"`` or ``"ctrl+mouse:back"``.

    A bare, unmodified letter or named key is never valid on its own -- it
    would be typed into whatever the user is writing rather than acting as a
    shortcut.
    """
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise BindingError("that shortcut is empty")

    if len(parts) == 1:
        only = parts[0]
        if only in KNOWN_KEYS:
            return Binding(frozenset(), key=only)
        if only.startswith("mouse:"):
            return _parse_mouse_terminal(frozenset(), only[len("mouse:") :])
        if only in MODIFIER_NAMES:
            raise BindingError("a shortcut needs at least one more key -- try holding two modifiers, like Ctrl+Win")
        raise BindingError(f"{only!r} needs at least one modifier, like Ctrl+{only.upper()}")

    *head, tail = parts
    if tail in MODIFIER_NAMES and all(p in MODIFIER_NAMES for p in head):
        # A pure modifier-only combination: every part is a modifier.
        modifiers = frozenset(parts)
        if len(modifiers) < 2:
            raise BindingError("a modifier-only shortcut needs two different modifiers, like Ctrl+Win")
        binding = Binding(modifiers)
        _reject_if_reserved(binding)
        return binding

    unknown = [m for m in head if m not in MODIFIER_NAMES]
    if unknown:
        raise BindingError(f"{unknown[0]!r} is not a modifier -- use ctrl, alt, shift, or cmd")
    if not head:
        raise BindingError("a shortcut needs at least one modifier, like Ctrl+Alt+P")
    modifiers = frozenset(head)

    if tail.startswith("mouse:"):
        return _parse_mouse_terminal(modifiers, tail[len("mouse:") :])
    # A hold-only name (ctrl_r, cmd, caps_lock, ...) makes no sense as a
    # chord's terminal key -- it falls through to the generic rejection just
    # below. f13-f18 are deliberately not rejected here even though they are
    # *also* valid hold-key names: CHORD_KEYS lists them too, so a chord like
    # "ctrl+f13" is unambiguous and stays valid.
    if tail not in CHORD_KEYS and not (len(tail) == 1 and tail.isalnum()):
        raise BindingError(f"{tail!r} cannot be used as a shortcut key")

    binding = Binding(modifiers, key=tail)
    _reject_if_reserved(binding)
    return binding


def format_binding(text: str) -> str:
    """Canonical string form, or the input unchanged if it will not parse.

    For display paths where a bad value must not raise -- a settings file can
    hold anything.
    """
    try:
        return str(parse_binding(text))
    except BindingError:
        return text


def effective_bindings(override: tuple[str, ...] | None, *defaults: str | None) -> tuple[str, ...]:
    """The binding list actually in force for one action.

    ``override`` is the user's setting (``None`` means "never customized" --
    fall back to ``defaults``; an explicit, possibly-empty tuple always wins
    outright, including an empty one, because a user who removed every
    binding meant to remove every binding). ``defaults`` are config.toml's
    values for that action (e.g. ``config.hotkey.key``,
    ``config.hotkey.key_chord``), filtered to the ones actually set.

    Kept string-only and dependency-free so it can be called from settings,
    the dashboard actions layer, and the engine without any of them needing
    to agree on where a "HotkeyConfig-shaped" default lives.
    """
    if override is not None:
        return override
    return tuple(d for d in defaults if d)


def find_binding_conflicts(bindings: Mapping[str, str]) -> dict[str, list[str]]:
    """Map binding id -> ids of other bindings on the exact same gesture.

    ``bindings`` is e.g. ``{"dictation:0": "ctrl_r", "command:0": "shift_r",
    "hands_free:0": "ctrl+alt+space"}``. Reported symmetrically, so callers
    can flag every row involved rather than arbitrarily blaming one.
    """
    conflicts: dict[str, list[str]] = {}
    for outer, value in bindings.items():
        for inner, other in bindings.items():
            if inner != outer and value == other:
                conflicts.setdefault(outer, []).append(inner)
    return conflicts


def find_binding_list_conflicts(bindings: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    """Map id -> ids of other entries that share at least one binding.

    Each id maps to a *list* of binding strings (an action may hold several
    alternate bindings at once). Two ids conflict the instant they share any
    one binding, even if the rest of their lists are disjoint -- a shared
    binding would only ever reach whichever action's listener claims it
    first, which is broken for both.
    """
    conflicts: dict[str, list[str]] = {}
    lists = {id_: set(values) for id_, values in bindings.items()}
    for outer, outer_set in lists.items():
        if not outer_set:
            continue
        for inner, inner_set in lists.items():
            if inner != outer and outer_set & inner_set:
                conflicts.setdefault(outer, []).append(inner)
    return conflicts


# ---------------------------------------------------------------------------
# ModifierHoldGate -- pure state machine for one modifier-combo hold's
# engaged/disengaged transitions, including the grace delay that lets a tap
# binding sharing the same modifier prefix fire first.
#
# Why this needs to exist at all: bind dictation to Ctrl+Win (hold) and
# hands-free to Ctrl+Win+Space (tap). The instant both Ctrl and Win are down,
# dictation's requirement is already fully satisfied -- but the user might be
# mid-way through pressing Space for hands-free. Firing dictation immediately
# would mean Ctrl+Win+Space can never be pressed without also, briefly,
# starting a dictation. The grace delay gives the tap binding a short window
# to complete before the hold engages; if it does, this generation is
# preempted and the hold never fires at all for that press.
# ---------------------------------------------------------------------------

#: How long a modifier-combo hold waits, once fully satisfied, before it
#: engages -- long enough for a following key press in the same gesture to
#: land (human key-to-key timing is comfortably under 100ms), short enough
#: that a genuine hold does not feel delayed.
#: How long a graced hold waits before engaging, to see whether a bigger
#: gesture sharing its prefix is coming. Deliberately shorter than
#: HotkeyConfig.min_hold_ms (200ms, see config.py) -- should_process already
#: discards any hold shorter than that, so deferring by less than it can
#: never suppress a hold that would otherwise have counted. That is what
#: makes the deferral free rather than a hole at the front of every session.
PREFIX_GRACE_S = 0.18

#: How often, at most, BindingListener checks its own idea of what modifier
#: keys are held against the OS's (see PlatformAdapter.modifier_keys_down).
#: Cheap -- a handful of GetAsyncKeyState calls -- but rate-limited so a hot
#: keyboard is not slowed by it; a phantom stuck modifier or a wedged hold,
#: however it arose, self-heals within one keystroke of this interval
#: instead of persisting until the app restarts.
RESYNC_INTERVAL_S = 1.0


class ModifierHoldGate:
    """One modifier-combo hold binding's engaged/disengaged state.

    No thread, no clock of its own -- the caller drives every transition and
    owns any scheduling, exactly as :class:`TapModeTracker` is caller-clocked.
    This is what makes the prefix-ambiguity logic testable without pynput or
    real timers.
    """

    __slots__ = ("_engaged", "_generation", "_satisfied")

    def __init__(self) -> None:
        self._satisfied = False
        self._engaged = False
        self._generation = 0

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def satisfied(self) -> bool:
        return self._satisfied

    def on_satisfied(self) -> int:
        """Every required modifier just became held at once.

        Returns the generation token the caller must present to
        :meth:`try_engage` for that attempt to still count.
        """
        self._satisfied = True
        self._generation += 1
        return self._generation

    def on_unsatisfied(self) -> bool:
        """A required modifier just let go.

        Returns ``True`` if a disengage should fire now -- only when an
        engage had actually happened for the sequence that just ended, so a
        combo that was preempted or released within its grace window never
        fires a disengage with no matching engage.
        """
        self._satisfied = False
        self._generation += 1
        was_engaged = self._engaged
        self._engaged = False
        return was_engaged

    def preempt(self, generation: int) -> None:
        """A tap binding sharing this prefix fired first.

        Invalidates a still-pending engage for ``generation``, if any, by
        advancing past it. Has no effect on an unrelated (already-superseded)
        generation, and no effect once this generation has already engaged.
        """
        if generation == self._generation and not self._engaged:
            self._generation += 1

    def try_engage(self, generation: int) -> bool:
        """Called once, immediately or after a grace delay.

        Returns ``True`` if the engage should actually fire now: the
        generation is still current, the modifiers are still down, and this
        generation has not already engaged.
        """
        if generation != self._generation or not self._satisfied or self._engaged:
            return False
        self._engaged = True
        return True


def needs_prefix_grace(hold: Binding, others: Iterable[Binding]) -> bool:
    """Whether ``hold`` needs :class:`ModifierHoldGate`'s grace delay because
    it is a physical *prefix* of some other registered binding -- pressing
    its key(s) is also the first step of pressing something else, so
    engaging the instant it is satisfied would fire both.

    Two shapes create this ambiguity:

    * a modifier-combo hold whose full modifier set is also a tap binding's
      prefix (``ctrl+cmd`` under ``ctrl+cmd+space``) -- unchanged from the
      original check, restricted to ``other.shape == "tap"`` so widening
      ``others`` to include every registered binding (below) does not also
      start grading combo-vs-combo pairs, which is a different, untested
      question this fix does not take on.
    * a **bare hold on a sided modifier key** (``ctrl_r``) when *any* other
      registered binding -- tap or combo -- requires that generic modifier
      (``ctrl+shift+t``, ``ctrl+cmd``). There is no way to press the second
      gesture without this key going down first, and unlike a combo it
      cannot be identified by a down-set subset test, only by exact pynput
      key identity -- see :func:`bare_hold_generic_modifier`.

    A bare hold on ``caps_lock``/``f13``-``f18``, or a bare mouse button, is
    never ambiguous this way and this returns ``False`` immediately for
    those.
    """
    if hold.is_modifier_combo:
        return any(
            other.shape == "tap" and hold.modifiers <= other.modifiers for other in others
        )
    generic = bare_hold_generic_modifier(hold)
    if generic is None:
        return False
    return any(generic in other.modifiers for other in others)


# ---------------------------------------------------------------------------
# BindingListener -- the one OS-level hook (keyboard + mouse) for every
# binding in the app: dictation, the voice command, hands-free, and every
# transform. Handlers run on the listener thread, so one that blocks delays
# every subsequent event; callers hand off to a worker, exactly as the rest
# of the app already does.
# ---------------------------------------------------------------------------


def _hold_kind(binding: Binding) -> str:
    """``"key"``, ``"mouse"``, or ``"combo"`` -- which dispatch family a hold
    binding belongs to. Replaces the old ``gate is not None`` overload,
    which meant "is a combo" in half a dozen places and could not be reused
    once bare holds also needed a gate (see :func:`needs_prefix_grace`)."""
    if binding.is_modifier_combo:
        return "combo"
    return "mouse" if binding.mouse else "key"


@dataclass(slots=True)
class _HoldSlot:
    binding: Binding
    on_engaged: Callable[[float], None]
    on_disengaged: Callable[[], None] | None
    tracker: KeyHoldTracker
    kind: str  # "key" | "mouse" | "combo" -- see _hold_kind
    gate: ModifierHoldGate
    needs_grace: bool = False
    pending_generation: int = 0
    #: The true press instant (time.monotonic()), captured before any grace
    #: delay -- threaded through to on_engaged so a deferred engage costs no
    #: audio (see sway.engine.frames_before).
    pending_pressed_at: float = 0.0


@dataclass(slots=True)
class _TapSlot:
    binding: Binding
    on_fire: Callable[[float], None]
    #: Debounces the OS's key-repeat exactly as a hold slot's does. A chord
    #: is a *tap*: it must fire once per physical press, however long the
    #: user leaves the keys down. Without this, Windows' auto-repeat (~31/s
    #: after a ~500ms delay) re-fires it dozens of times for one deliberate
    #: press -- which for a transform meant dozens of concurrent selection
    #: reads and Groq calls, each one clobbering the last's captured
    #: selection, reported to the user as "nothing selected".
    tracker: KeyHoldTracker = field(default_factory=KeyHoldTracker)


class BindingListener:
    """Dispatches every registered :class:`Binding` from one keyboard hook
    and one mouse hook.

    ``rebind()`` replaces the whole registry at once, so a settings change
    takes effect without tearing the OS hooks down and putting them back --
    which on Windows can drop keystrokes in between.
    """

    def __init__(
        self, *, modifier_probe: Callable[[], frozenset[str] | None] | None = None
    ) -> None:
        self._lock = threading.RLock()
        self._hold_slots: dict[str, _HoldSlot] = {}
        self._tap_slots: dict[str, _TapSlot] = {}
        #: Currently-held modifier names and mouse buttons, shared by every
        #: modifier-combo hold and every chord's matching logic. A bare
        #: single-key or single-mouse-button hold does not use these -- it is
        #: matched by exact pynput identity via its own KeyHoldTracker
        #: instead, so two such holds never interfere with each other.
        self._down_mods: set[str] = set()
        self._down_mouse: set[str] = set()
        self._keyboard_listener: keyboard.Listener | None = None
        self._mouse_listener: mouse.Listener | None = None
        self._timers: dict[str, threading.Timer] = {}
        #: Reference-counted so an overlapping paste and selection-read on
        #: different threads compose correctly -- see suspended().
        self._suspend_gate = RefCountedGate()
        #: Best-effort OS query for what modifier keys are actually held,
        #: used only to heal this listener's own state after it drifts from
        #: reality -- see _maybe_resync_locked. None on a platform that
        #: cannot answer (or in a test that supplies none), in which case
        #: resync is simply never attempted.
        self._modifier_probe = modifier_probe
        self._last_resync = 0.0

    @property
    def is_running(self) -> bool:
        return self._keyboard_listener is not None

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Pause dispatch of Sway's own synthetic input for the block.

        Callers wrap Sway's own synthetic Ctrl+C/Ctrl+V (SelectionReader,
        TextInjector) in this, so this listener's hook never mistakes Sway's
        own keystrokes for the user's -- pynput's low-level hook delivers
        Sway's own injected keystrokes exactly like real ones otherwise.
        Scoped to events pynput itself reports as OS-injected (see
        ``_on_key_press``'s ``injected`` parameter, backed by Windows'
        ``LLKHF_INJECTED``), not to every event during the block -- a real
        keystroke that happens to land in this window is still dispatched
        normally, which is what closes the "swallowed release wedges a
        binding forever" failure an earlier, blunter version of this had.

        Reference-counted rather than a plain flag: a dictation paste and a
        transform's selection-read can be in flight on two different worker
        threads at once, and neither's suspension may end early just because
        the other's finished first.
        """
        with self._lock:
            self._suspend_gate.acquire()
        try:
            yield
        finally:
            with self._lock:
                self._suspend_gate.release()

    def rebind(
        self,
        *,
        hold_bindings: Mapping[str, tuple[Binding, Callable[[float], None], Callable[[], None] | None]],
        tap_bindings: Mapping[str, tuple[Binding, Callable[[float], None]]],
    ) -> None:
        """Replace every registered binding at once.

        ``hold_bindings``: id -> (binding, on_engaged, on_disengaged).
        ``on_engaged`` receives the true press instant (``time.monotonic()``
        at the moment the key physically went down, even if the engage
        itself was deferred by prefix grace -- see PREFIX_GRACE_S).
        ``on_disengaged`` may be ``None`` for a hold-shaped binding whose
        action only cares about the engage (e.g. a transform bound to a bare
        mouse button, which fires once rather than running for a duration).
        ``tap_bindings``: id -> (binding, on_fire), where ``on_fire`` also
        receives the press instant.

        Replaces every slot's tracking state, including any hold that is
        currently mid-engage -- its disengage will not fire through the new
        slot. Callers must not rebind while a session is genuinely in flight;
        the engine already follows that discipline for settings changes (see
        :meth:`sway.engine.Engine._apply_settings`), and the activation
        ledger is the backstop if one somehow slips through.

        Any pending prefix-grace timer (see :data:`PREFIX_GRACE_S`) is
        cancelled first, before it can fire against a slot id that now
        belongs to a different binding -- the settings-change window this
        guards is much shorter than a whole session, so it is not covered
        by "don't rebind mid-session" on its own.
        """
        all_bindings = [b for b, _, _ in hold_bindings.values()] + [b for b, _ in tap_bindings.values()]
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._hold_slots = {
                id_: _HoldSlot(
                    binding=binding,
                    on_engaged=on_engaged,
                    on_disengaged=on_disengaged,
                    tracker=KeyHoldTracker(),
                    kind=_hold_kind(binding),
                    gate=ModifierHoldGate(),
                    needs_grace=needs_prefix_grace(binding, all_bindings),
                )
                for id_, (binding, on_engaged, on_disengaged) in hold_bindings.items()
            }
            self._tap_slots = {
                id_: _TapSlot(binding=binding, on_fire=on_fire)
                for id_, (binding, on_fire) in tap_bindings.items()
            }

    def start(self) -> None:
        if self._keyboard_listener is not None:
            logger.warning("binding listener is already running; not starting a second hook")
            return
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press, on_release=self._on_key_release
        )
        self._keyboard_listener.start()
        self._mouse_listener = mouse.Listener(on_click=self._on_mouse_click)
        self._mouse_listener.start()

    def stop(self) -> None:
        """Tear both OS hooks down, and wait for them to actually be down.

        The join matters, exactly as it did for the old ``HotkeyListener``:
        without it, a ``stop()`` immediately followed by a ``start()`` can
        leave the outgoing hook still delivering events while the incoming
        one delivers them too.
        """
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()
            self._down_mods.clear()
            self._down_mouse.clear()
            for slot in self._hold_slots.values():
                slot.tracker = KeyHoldTracker()
                slot.gate = ModifierHoldGate()
            for tap in self._tap_slots.values():
                tap.tracker = KeyHoldTracker()
        keyboard_listener, self._keyboard_listener = self._keyboard_listener, None
        mouse_listener, self._mouse_listener = self._mouse_listener, None
        if keyboard_listener is not None:
            keyboard_listener.stop()
            try:
                keyboard_listener.join(timeout=1.0)
            except RuntimeError:
                pass
        if mouse_listener is not None:
            mouse_listener.stop()
            try:
                mouse_listener.join(timeout=1.0)
            except RuntimeError:
                pass

    # -- keyboard -----------------------------------------------------------

    def _on_key_press(
        self, key: keyboard.Key | keyboard.KeyCode | None, injected: bool = False
    ) -> None:
        # Captured before the lock so queueing behind another event is never
        # charged to the user's press -- this is what a deferred (graced)
        # engage back-dates the ring-buffer read to, so no audio is lost.
        pressed_at = time.monotonic()
        with self._lock:
            if injected and self._suspend_gate.active:
                return  # Sway's own synthetic keystroke -- see suspended()
            self._maybe_resync_locked(pressed_at, pending_modifier=_modifier_name(key))

            # Bare single-key holds, matched by exact pynput identity.
            # Checked first, and unconditionally: a sided hold key like
            # ctrl_r is *also* a generic modifier for the combo/chord
            # bookkeeping below (it is one of the pynput keys MODIFIER_KEYS
            # maps to "ctrl"), and both must be evaluated for the same
            # physical press -- ctrl_r bound bare AND contributing to a
            # ctrl+alt+p chord held elsewhere are not mutually exclusive.
            # Returning early out of this branch (as an earlier version did)
            # meant a bare hold on any modifier-ish key -- ctrl_r, the
            # app's own default -- could never fire at all.
            for slot_id, slot in self._hold_slots.items():
                if slot.kind != "key":
                    continue
                bound_key = KNOWN_KEYS.get(slot.binding.key)
                if bound_key is not None and key == bound_key and not slot.binding.modifiers:
                    if slot.tracker.press():
                        self._begin_hold_locked(slot_id, slot, pressed_at)
                    break

            modifier = _modifier_name(key)
            if modifier is not None:
                already_down = modifier in self._down_mods
                self._down_mods.add(modifier)
                if not already_down:
                    self._reevaluate_combo_holds_locked(pressed_at)
                return

            # Chords: a terminal key press while the required modifiers are
            # exactly the ones currently down.
            down_mods = frozenset(self._down_mods)
            fired_chord_mods: frozenset[str] | None = None
            name = _key_name(key)
            if name is not None and down_mods:
                for slot in self._tap_slots.values():
                    if slot.binding.mouse or slot.binding.key != name:
                        continue
                    if slot.binding.modifiers == down_mods:
                        # Preempt whether or not this press is a repeat: the
                        # gesture is unambiguously the chord's either way, so
                        # a bare hold underneath it must stay cancelled for
                        # as long as the keys are down.
                        fired_chord_mods = down_mods
                        if slot.tracker.press():
                            self._safe_call(slot.on_fire, pressed_at)
                        break

            if fired_chord_mods is not None:
                self._preempt_holds_locked(fired_chord_mods)

    def _on_key_release(
        self, key: keyboard.Key | keyboard.KeyCode | None, injected: bool = False
    ) -> None:
        with self._lock:
            if injected and self._suspend_gate.active:
                return  # Sway's own synthetic keystroke -- see suspended()

            # Same "check the bare hold unconditionally first" reasoning as
            # _on_key_press: a sided key can be both a bound bare-hold key
            # and a contributor to the generic modifier-down set.
            for slot_id, slot in self._hold_slots.items():
                if slot.kind != "key":
                    continue
                bound_key = KNOWN_KEYS.get(slot.binding.key)
                if bound_key is not None and key == bound_key and not slot.binding.modifiers:
                    if slot.tracker.release():
                        self._end_hold_locked(slot_id, slot)
                    break

            modifier = _modifier_name(key)
            if modifier is not None and modifier in self._down_mods:
                self._down_mods.discard(modifier)
                self._reevaluate_combo_holds_locked(time.monotonic())

            # Rearm chords last, so it sees the updated modifier-down set.
            self._rearm_tap_slots_locked(key_name=_key_name(key))

    # -- mouse ----------------------------------------------------------

    def _on_mouse_click(
        self, x: int, y: int, button: mouse.Button, pressed: bool, injected: bool = False
    ) -> None:
        name = _mouse_name(button)
        if name is None:
            return
        pressed_at = time.monotonic()
        with self._lock:
            if injected and self._suspend_gate.active:
                return  # Sway's own synthetic input -- see suspended()

            down_mods = frozenset(self._down_mods)
            if pressed:
                self._down_mouse.add(name)
                # Bare mouse-button holds.
                for slot_id, slot in self._hold_slots.items():
                    if slot.kind != "mouse" or slot.binding.mouse != name:
                        continue
                    if not slot.binding.modifiers:
                        if slot.tracker.press():
                            self._begin_hold_locked(slot_id, slot, pressed_at)
                        return
                # Chords ending in a mouse button.
                fired_chord_mods: frozenset[str] | None = None
                if down_mods:
                    for slot in self._tap_slots.values():
                        if slot.binding.key or slot.binding.mouse != name:
                            continue
                        if slot.binding.modifiers == down_mods:
                            fired_chord_mods = down_mods
                            if slot.tracker.press():
                                self._safe_call(slot.on_fire, pressed_at)
                            break
                if fired_chord_mods is not None:
                    self._preempt_holds_locked(fired_chord_mods)
            else:
                self._down_mouse.discard(name)
                self._rearm_tap_slots_locked(mouse_name=name)
                for slot_id, slot in self._hold_slots.items():
                    if slot.kind != "mouse" or slot.binding.mouse != name:
                        continue
                    if not slot.binding.modifiers and slot.tracker.release():
                        self._end_hold_locked(slot_id, slot)
                        return

    def _rearm_tap_slots_locked(
        self, *, key_name: str | None = None, mouse_name: str | None = None
    ) -> None:
        """Let a chord fire again once its gesture is physically broken.

        A chord fires once per press (see :class:`_TapSlot`'s tracker), so
        something must rearm it. Either half of the gesture coming up does:
        the terminal key/button, *or* any one of the required modifiers.
        Both are accepted deliberately -- you cannot press the chord again
        without releasing one of them, so this cannot wedge, and neither
        release alone has to be the one the hook happens to observe.
        """
        down = frozenset(self._down_mods)
        for slot in self._tap_slots.values():
            binding = slot.binding
            terminal_up = (
                (key_name is not None and not binding.mouse and binding.key == key_name)
                or (mouse_name is not None and binding.mouse == mouse_name)
            )
            if terminal_up or not binding.modifiers <= down:
                slot.tracker.release()

    # -- holds: begin/end/engage, shared by bare keys, mouse buttons, and
    # modifier combos ------------------------------------------------------

    def _begin_hold_locked(self, slot_id: str, slot: _HoldSlot, pressed_at: float) -> None:
        generation = slot.gate.on_satisfied()
        slot.pending_generation = generation
        slot.pending_pressed_at = pressed_at
        if slot.needs_grace:
            self._schedule_engage(slot_id, generation)
        else:
            self._try_engage_locked(slot, generation)

    def _end_hold_locked(self, slot_id: str, slot: _HoldSlot) -> None:
        self._cancel_timer(slot_id)
        if slot.gate.on_unsatisfied():
            self._safe_call(slot.on_disengaged)

    def _try_engage_locked(self, slot: _HoldSlot, generation: int) -> None:
        if slot.kind == "key" and self._superset_combo_satisfied_locked(slot):
            # A combo sharing this bare hold's modifier is already satisfied
            # -- the user's fingers are on the bigger gesture, so let that
            # one win rather than engaging both. Mirrors _preempt_holds_locked
            # for the reverse order (combo engages before this timer fires).
            slot.gate.preempt(generation)
            return
        if not slot.gate.try_engage(generation):
            return
        if slot.kind == "combo":
            self._preempt_holds_locked(slot.binding.modifiers)
        self._safe_call(slot.on_engaged, slot.pending_pressed_at)

    def _superset_combo_satisfied_locked(self, slot: _HoldSlot) -> bool:
        generic = bare_hold_generic_modifier(slot.binding)
        if generic is None:
            return False
        return any(
            other.kind == "combo" and other.gate.satisfied and generic in other.binding.modifiers
            for other in self._hold_slots.values()
        )

    def _preempt_holds_locked(self, mods: frozenset[str]) -> None:
        """A chord fired, or a modifier-combo hold just engaged, on ``mods``
        -- cancel every still-pending graced hold whose gesture that
        gesture's keys already contain."""
        for slot_id, slot in self._hold_slots.items():
            if not slot.needs_grace or slot.gate.engaged or not slot.gate.satisfied:
                continue
            if slot.kind == "combo":
                covered = slot.binding.modifiers <= mods
            elif slot.kind == "key":
                generic = bare_hold_generic_modifier(slot.binding)
                covered = generic is not None and generic in mods
            else:
                covered = False
            if covered:
                self._cancel_timer(slot_id)
                slot.gate.preempt(slot.pending_generation)

    # -- modifier-combo holds --------------------------------------------

    def _reevaluate_combo_holds_locked(self, pressed_at: float) -> None:
        """Re-check every modifier-combo hold against the current down-set.

        Called with the lock held, from both a keyboard press and a keyboard
        release (a combo can become satisfied or unsatisfied on either).
        """
        down = frozenset(self._down_mods)
        for slot_id, slot in self._hold_slots.items():
            if slot.kind != "combo":
                continue
            satisfied = slot.binding.modifiers <= down
            was_satisfied = slot.gate.satisfied
            if satisfied and not was_satisfied:
                self._begin_hold_locked(slot_id, slot, pressed_at)
            elif not satisfied and was_satisfied:
                self._end_hold_locked(slot_id, slot)

    def _schedule_engage(self, slot_id: str, generation: int) -> None:
        self._cancel_timer(slot_id)
        timer = threading.Timer(PREFIX_GRACE_S, self._try_engage_deferred, args=(slot_id, generation))
        timer.daemon = True
        self._timers[slot_id] = timer
        timer.start()

    def _try_engage_deferred(self, slot_id: str, generation: int) -> None:
        with self._lock:
            self._timers.pop(slot_id, None)
            slot = self._hold_slots.get(slot_id)
            if slot is None:
                return
            self._try_engage_locked(slot, generation)

    def _cancel_timer(self, slot_id: str) -> None:
        timer = self._timers.pop(slot_id, None)
        if timer is not None:
            timer.cancel()

    # -- self-healing --------------------------------------------------------

    def _maybe_resync_locked(self, now: float, *, pending_modifier: str | None = None) -> None:
        """Check this listener's own down-modifier state against the OS's
        (see :meth:`sway.platform.PlatformAdapter.modifier_keys_down`), and
        heal any drift found -- a stuck modifier or a wedged bare hold,
        however either arose (a swallowed release, a missed event), self-
        heals within one keystroke of :data:`RESYNC_INTERVAL_S` instead of
        persisting until the app restarts.

        ``pending_modifier`` is the generic modifier of the key currently
        being pressed, if any -- excluded from the "OS reports it down but
        we don't have it yet" comparison, since the OS already reflects a
        physical press by the time this hook sees it, moments before this
        same call's own bookkeeping would add it. Without this exclusion,
        practically every press of a modifier key would look like drift.

        No-op (and cheap to call) when no probe was supplied, or on any
        platform that cannot answer -- see PlatformAdapter's contract.
        """
        if self._modifier_probe is None or now - self._last_resync < RESYNC_INTERVAL_S:
            return
        self._last_resync = now
        actual_sided = self._modifier_probe()
        if actual_sided is None:
            return
        actual_generic = {
            generic
            for sided in actual_sided
            if (generic := _SIDED_HOLD_GENERIC_MODIFIER.get(sided)) is not None
        }
        stale = self._down_mods - actual_generic
        missed = actual_generic - self._down_mods - ({pending_modifier} if pending_modifier else set())
        stuck = [
            (slot_id, slot)
            for slot_id, slot in self._hold_slots.items()
            if slot.kind == "key"
            and slot.gate.satisfied
            and slot.binding.key not in actual_sided
            and bare_hold_generic_modifier(slot.binding) is not None
        ]
        if not (stale or missed or stuck):
            return
        logger.warning(
            "resynced key state against the OS: dropped %s, added %s, released %s",
            sorted(stale) or "-",
            sorted(missed) or "-",
            [slot_id for slot_id, _ in stuck] or "-",
        )
        self._down_mods = set(actual_generic)
        for slot_id, slot in stuck:
            slot.tracker.release()
            self._end_hold_locked(slot_id, slot)
        self._reevaluate_combo_holds_locked(now)

    def _safe_call(self, fn: Callable[..., None] | None, *args: object) -> None:
        # pynput silently kills its listener thread if a callback raises;
        # swallowing here keeps the listener alive for the next attempt
        # instead of leaving the app looking hung.
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            logger.exception("binding callback raised")


def _modifier_name(key: object) -> str | None:
    for name, variants in MODIFIER_KEYS.items():
        if key in variants:
            return name
    return None


#: Maps each sided hold-key name to the generic modifier it also satisfies
#: for combo/chord purposes, per _modifier_name -- e.g. "ctrl_r" -> "ctrl".
#: caps_lock and f13-f18 map to None: they are not modifiers and can never
#: create the conflict needs_prefix_grace's sibling below exists to catch.
_SIDED_HOLD_GENERIC_MODIFIER: dict[str, str | None] = {
    name: _modifier_name(key) for name, key in KNOWN_KEYS.items()
}


def bare_hold_generic_modifier(binding: Binding) -> str | None:
    """The generic modifier a *bare* hold key also satisfies as a
    modifier-down contributor -- ``ctrl_r`` -> ``"ctrl"``. ``None`` for
    anything that cannot create this ambiguity: a combo, a mouse button, or
    a hold key that is not a modifier at all (``caps_lock``, ``f13``-``f18``,
    per :data:`_SIDED_HOLD_GENERIC_MODIFIER`).

    This is what makes a bare hold on a sided modifier key a physical
    *prefix* of any other binding requiring that modifier -- there is no way
    to press ``ctrl+shift+t`` without ``ctrl_r`` (or ``ctrl_l``) going down
    first. Used by :func:`needs_prefix_grace` and by
    :class:`BindingListener`'s own dispatch (:meth:`~BindingListener._preempt_holds_locked`,
    :meth:`~BindingListener._maybe_resync_locked`) to give such a hold the
    same grace-then-preempt treatment a modifier combo already gets, instead
    of engaging unconditionally the instant the key goes down.
    """
    if binding.modifiers or binding.mouse or not binding.key:
        return None
    return _SIDED_HOLD_GENERIC_MODIFIER.get(binding.key)


def binding_advisory(binding: Binding) -> str:
    """A non-blocking note about how ``binding`` will behave in other apps,
    or ``""`` when there is nothing worth saying.

    Sway's hooks are deliberately non-suppressing (see :class:`BindingListener`)
    -- a bound keystroke still reaches whatever application is focused, the
    same as it would with the hook absent. Making them suppressing would
    mean swallowing keys system-wide from every other application, which no
    dictation tool should do. So a shortcut that already means something to
    the focused app does *both* things: this warns about the common case,
    it never rejects the binding.
    """
    if "alt" in binding.modifiers and len(binding.key) == 1:
        return (
            "Alt + a letter also opens menus in many Windows apps -- both will "
            "happen. Ctrl + Shift + a letter is usually free."
        )
    return ""


#: Virtual-key codes for the alphanumeric block, mapped to the character on
#: the keycap: 0x30-0x39 are ASCII '0'-'9' and 0x41-0x5A are 'A'-'Z', by
#: definition and independently of which modifiers happen to be held.
#:
#: Only consulted on Windows. There, Windows itself decides which VK a
#: physical key produces *using the active layout*, so the VK already names
#: the letter printed on the keycap -- exactly how a chord is written. X11
#: reports ASCII-compatible keysyms over the same range, but macOS reports a
#: hardware keycode with an entirely unrelated meaning, so this is trusted
#: only where its semantics are guaranteed.
_VK_ALNUM: dict[int, str] = {
    **{vk: chr(vk) for vk in range(0x30, 0x3A)},  # '0'-'9'
    **{vk: chr(vk + 32) for vk in range(0x41, 0x5B)},  # 'A'-'Z' -> 'a'-'z'
}
_TRUST_VK_ALNUM = sys.platform == "win32"


def _key_name(key: object) -> str | None:
    """The chord-key name for a pressed key, or ``None`` if it cannot be one.

    The virtual key code is consulted *before* ``KeyCode.char``, because on
    Windows ``char`` cannot identify a chord's terminal key at all once Ctrl
    is held. pynput fills ``char`` from ``ToUnicodeEx`` with the real
    modifier state, and Windows answers that with the **control character**:
    Ctrl+P is ``'\\x10'``, not ``'p'``; Ctrl+1 produces no character
    whatsoever; and Ctrl+Alt+P produces an unrelated character that is
    nevertheless alphanumeric. Matching on ``char`` therefore silently
    dropped every chord containing Ctrl -- including all seven shipped
    transforms on Ctrl+Alt+1..7 -- with no error anywhere, which is exactly
    how it went unnoticed. The VK code is unaffected by modifiers.

    ``char`` remains the fallback: it is what identifies a key on the
    platforms where :data:`_VK_ALNUM` is not trustworthy, and it correctly
    handles an unmodified or Shift-only press everywhere.
    """
    if _TRUST_VK_ALNUM:
        vk = getattr(key, "vk", None)
        if isinstance(vk, int) and vk in _VK_ALNUM:
            return _VK_ALNUM[vk]
    char = getattr(key, "char", None)
    if isinstance(char, str) and len(char) == 1 and char.isalnum():
        return char.lower()
    for name, known in CHORD_KEYS.items():
        if key is known:
            return name
    return None


def _mouse_name(button: object) -> str | None:
    for name, known in MOUSE_BUTTONS.items():
        if button is known:
            return name
    return None


__all__ = [
    "CHORD_KEYS",
    "KNOWN_KEYS",
    "MODIFIER_KEYS",
    "MODIFIER_NAMES",
    "MOUSE_BUTTONS",
    "PREFIX_GRACE_S",
    "RESYNC_INTERVAL_S",
    "Binding",
    "BindingError",
    "BindingListener",
    "DoubleTapTracker",
    "KeyHoldTracker",
    "ModifierHoldGate",
    "RefCountedGate",
    "TapModeTracker",
    "bare_hold_generic_modifier",
    "binding_advisory",
    "effective_bindings",
    "find_binding_conflicts",
    "find_binding_list_conflicts",
    "format_binding",
    "needs_prefix_grace",
    "parse_binding",
    "prettify_hotkey",
]
