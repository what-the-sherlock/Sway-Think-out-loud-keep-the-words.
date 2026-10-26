"""Easing and time-sampled tweens for the floating bar.

Pure: no Tk, no PIL, no global clock. That is deliberate, and it is the same
idiom :mod:`sway.orb` uses -- animation is a *function
of wall clock*, sampled once per frame, rather than a timer that owns state
and has to be started, stopped and cancelled. A dropped frame or a paused
poll loop cannot desynchronise anything, because nothing is being stepped.

Consequences worth stating, because they are why this module is shaped this
way:

* Every value is recomputed from ``(began_at, duration, start, end)``, so a
  frame that arrives late lands where it should have, not one step further
  along.
* :meth:`Motion.settled` lets the caller drop its frame rate the moment
  nothing is moving, which matters here: CLAUDE.md puts the bar's repaint
  cost inside the dictation latency budget, so 60fps is something to enter
  briefly and leave again, not to run all day.
* It is unit-testable against an injected clock, without a screen.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = [
    "ACCENT_S",
    "BUTTON_FILL_S",
    "COLLAPSE_S",
    "DOT_HOVER_S",
    "DOT_UNHOVER_S",
    "FADE_OUT_S",
    "LABEL_FADE_S",
    "LEVEL_S",
    "PANEL_CLOSE_S",
    "PANEL_OPEN_S",
    "PRESS_DOWN_S",
    "PRESS_UP_S",
    "SHAKE_S",
    "TOOLTIP_DELAY_S",
    "TOOLTIP_S",
    "WIDTH_S",
    "Motion",
    "Timer",
    "Tween",
    "cubic_bezier",
    "ease_enter",
    "ease_exit",
    "ease_in_out_cubic",
    "ease_linear",
    "ease_out_back",
    "ease_out_cubic",
    "ease_standard",
    "lerp",
    "lerp_rgb",
    "pulse",
    "shake_offset",
]


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------
#
# One table, in seconds, so the product's sense of pace can be read and
# adjusted in one place rather than being scattered as literals through the
# renderer. Everything sits in the 90-260ms band: below ~80ms a transition
# reads as a glitch rather than as motion, and above ~300ms it reads as the
# interface being slow.

DOT_HOVER_S = 0.14
DOT_UNHOVER_S = 0.18
PRESS_DOWN_S = 0.09
PRESS_UP_S = 0.16
WIDTH_S = 0.17
COLLAPSE_S = 0.20
PANEL_OPEN_S = 0.16
PANEL_CLOSE_S = 0.11
FADE_OUT_S = 0.26
BUTTON_FILL_S = 0.12
LEVEL_S = 0.09
TOOLTIP_S = 0.12
#: How long the pointer must rest on a control before its tooltip appears.
#: Long enough that merely crossing the bar never spawns one; short enough
#: that deliberately pausing feels answered.
TOOLTIP_DELAY_S = 0.6
SHAKE_S = 0.24
#: Accent colour cross-fade between states (DESIGN.md §8.2, §10.2).
ACCENT_S = 0.15
#: State-label cross-fade (DESIGN.md §10.2). Named here so the bar and the
#: dashboard (styles.css) share one table.
LABEL_FADE_S = 0.12


# ---------------------------------------------------------------------------
# The three curves (DESIGN.md §10.1)
# ---------------------------------------------------------------------------
#
# The same cubic-beziers styles.css uses as --ease-standard / --ease-enter /
# --ease-exit, so the bar and the dashboard move with one character.


def cubic_bezier(x1: float, y1: float, x2: float, y2: float) -> Callable[[float], float]:
    """An easing function equal to CSS ``cubic-bezier(x1, y1, x2, y2)``.

    Solves the curve's x(s) = t for s (Newton steps, bisection fallback), then
    returns y(s). Endpoints are exact: f(0) == 0 and f(1) == 1.
    """

    def bez(a: float, b: float, s: float) -> float:
        return 3 * a * (1 - s) ** 2 * s + 3 * b * (1 - s) * s * s + s ** 3

    def bez_dx(s: float) -> float:
        return 3 * x1 * (1 - s) ** 2 + 6 * (x2 - x1) * (1 - s) * s + 3 * (1 - x2) * s * s

    def ease(t: float) -> float:
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        s = t
        for _ in range(8):
            dx = bez_dx(s)
            if abs(dx) < 1e-6:
                break
            s -= (bez(x1, x2, s) - t) / dx
        if not 0.0 <= s <= 1.0 or abs(bez(x1, x2, s) - t) > 1e-5:
            lo, hi = 0.0, 1.0
            for _ in range(40):
                s = (lo + hi) / 2
                if bez(x1, x2, s) < t:
                    lo = s
                else:
                    hi = s
        return bez(y1, y2, s)

    return ease


#: State changes on screen: colours, toggles, widths.
ease_standard = cubic_bezier(0.2, 0.0, 0.0, 1.0)
#: Things arriving: menus, popovers.
ease_enter = cubic_bezier(0.16, 1.0, 0.3, 1.0)
#: Things leaving. Always shorter than the matching enter.
ease_exit = cubic_bezier(0.4, 0.0, 1.0, 1.0)


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------


def ease_linear(t: float) -> float:
    return t


def ease_out_cubic(t: float) -> float:
    """Fast departure, soft arrival.

    The default for anything the user just caused: it acknowledges the input
    on the very first frame instead of ramping into it.
    """
    return 1.0 - (1.0 - t) ** 3


def ease_in_out_cubic(t: float) -> float:
    """Eased at both ends.

    For motion the user did *not* just cause -- a collapse after a dwell, a
    fade-out -- where an instant departure reads as the element being yanked
    away rather than leaving.
    """
    if t < 0.5:
        return 4.0 * t * t * t
    return 1.0 - ((-2.0 * t + 2.0) ** 3) / 2.0


def ease_out_back(t: float, overshoot: float = 1.70158) -> float:
    """Overshoots slightly, then settles.

    Used only on a press *release*, where the small rebound is the whole
    reason a button feels physical. Deliberately not used anywhere a size
    overshoot would cause a window resize.
    """
    c3 = overshoot + 1.0
    u = t - 1.0
    return 1.0 + c3 * u * u * u + overshoot * u * u


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_rgb(
    a: tuple[int, int, int], b: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    """Blend two RGB triples.

    Clamped, and rounded to whole channels so the result can take part in a
    render-memoisation key without a settled colour jittering forever.
    """
    t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
    return (
        round(lerp(a[0], b[0], t)),
        round(lerp(a[1], b[1], t)),
        round(lerp(a[2], b[2], t)),
    )


def pulse(now_s: float, period_s: float) -> float:
    """A continuous 0..1 sine over ``period_s``.

    The recording badge used a two-frame alpha flip, which at 500ms reads as
    a blink -- an alarm. A sine over the same period reads as breathing,
    which is what "I am listening" should look like.
    """
    if period_s <= 0.0:
        return 1.0
    return 0.5 + 0.5 * math.sin(2.0 * math.pi * (now_s / period_s))


def shake_offset(
    elapsed_s: float,
    *,
    duration_s: float = SHAKE_S,
    amplitude: float = 3.0,
    cycles: float = 3.0,
) -> float:
    """A decaying horizontal wobble, in pixels, for the error entrance.

    Returns 0.0 outside the window, so a caller can sample it every frame
    unconditionally. Deliberately the only shake in the product: this is the
    one moment where attention has to be taken rather than invited.
    """
    if duration_s <= 0.0 or elapsed_s < 0.0 or elapsed_s >= duration_s:
        return 0.0
    t = elapsed_s / duration_s
    return math.sin(2.0 * math.pi * cycles * t) * amplitude * (1.0 - t)


# ---------------------------------------------------------------------------
# Tweens
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tween:
    """One value moving from ``start`` to ``end``, sampled by wall clock."""

    start: float
    end: float
    duration_s: float
    began_at: float
    ease: Callable[[float], float] = ease_out_cubic

    def value(self, now: float) -> float:
        """Clamped at both ends, so sampling early or late is always safe."""
        if self.duration_s <= 0.0:
            return self.end
        t = (now - self.began_at) / self.duration_s
        if t <= 0.0:
            return self.start
        if t >= 1.0:
            return self.end
        return self.start + (self.end - self.start) * self.ease(t)

    def done(self, now: float) -> bool:
        return now >= self.began_at + self.duration_s


class Motion:
    """A named set of tweens, all sampled from one clock.

    The important behaviour is in :meth:`to`, which is written to be called
    *every frame* with the current target. Re-issuing the same target is a
    no-op, and issuing a new one mid-flight retargets from wherever the value
    has actually reached -- never from the original start. Without the first,
    a per-frame caller would restart the tween forever and it would never
    advance; without the second, a pointer crossing the bar twice in quick
    succession produces a visible snap.
    """

    __slots__ = ("_clock", "_tweens")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._tweens: dict[str, Tween] = {}

    def set(self, name: str, value: float) -> None:
        """Snap to ``value`` with no motion (initial state, or a hard reset)."""
        self._tweens[name] = Tween(value, value, 0.0, self._clock())

    def to(
        self,
        name: str,
        target: float,
        *,
        duration_s: float,
        ease: Callable[[float], float] = ease_out_cubic,
    ) -> None:
        """Move ``name`` toward ``target``. Idempotent for a repeated target."""
        existing = self._tweens.get(name)
        if existing is not None and existing.end == target:
            return
        now = self._clock()
        current = existing.value(now) if existing is not None else target
        if current == target or duration_s <= 0.0:
            self._tweens[name] = Tween(target, target, 0.0, now)
            return
        self._tweens[name] = Tween(current, target, duration_s, now, ease)

    def value(self, name: str, default: float = 0.0) -> float:
        tween = self._tweens.get(name)
        return default if tween is None else tween.value(self._clock())

    def target(self, name: str, default: float = 0.0) -> float:
        """Where ``name`` is heading, ignoring where it currently is."""
        tween = self._tweens.get(name)
        return default if tween is None else tween.end

    def moving(self, name: str) -> bool:
        tween = self._tweens.get(name)
        return tween is not None and not tween.done(self._clock())

    def settled(self) -> bool:
        """True when nothing is in flight -- the signal to drop frame rate."""
        now = self._clock()
        return all(tween.done(now) for tween in self._tweens.values())

    def forget(self, name: str) -> None:
        """Drop a tween entirely, so the next :meth:`to` starts from scratch."""
        self._tweens.pop(name, None)

    def clear(self) -> None:
        self._tweens.clear()


@dataclass(slots=True)
class Timer:
    """A one-shot deadline on the same clock, for dwells and holds.

    Kept distinct from :class:`Tween` because a dwell has no value, only a
    "has it elapsed yet". Collapsing the two would invite dwell logic to be
    written as a tween nobody reads.
    """

    clock: Callable[[], float] = time.monotonic
    _deadline: float | None = field(default=None, init=False)

    def arm(self, seconds: float) -> None:
        self._deadline = self.clock() + max(0.0, seconds)

    def arm_if_idle(self, seconds: float) -> None:
        """Arm only if not already armed, so a per-frame caller cannot keep
        pushing its own deadline into the future."""
        if self._deadline is None:
            self.arm(seconds)

    def disarm(self) -> None:
        self._deadline = None

    @property
    def armed(self) -> bool:
        return self._deadline is not None

    def elapsed(self) -> bool:
        """True once armed *and* past the deadline. False when disarmed."""
        return self._deadline is not None and self.clock() >= self._deadline

    def remaining(self) -> float:
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - self.clock())
