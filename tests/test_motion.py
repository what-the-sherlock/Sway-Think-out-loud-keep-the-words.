"""The bar's motion primitives.

These are pure functions of an injected clock, so all of it is testable
without a screen -- which is the whole reason the animation lives in its own
module rather than inside the Tk widget.
"""

from __future__ import annotations

import pytest

from sway.motion import (
    Motion,
    Timer,
    Tween,
    ease_in_out_cubic,
    ease_linear,
    ease_out_back,
    ease_out_cubic,
    lerp,
    lerp_rgb,
    pulse,
    shake_offset,
)


class FakeClock:
    """A hand-wound clock, so a frame can be placed exactly where it matters."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# -- easing ----------------------------------------------------------------

EASINGS = [ease_linear, ease_out_cubic, ease_in_out_cubic, ease_out_back]


@pytest.mark.parametrize("ease", EASINGS)
def test_every_easing_is_anchored_at_both_ends(ease):
    """An easing that does not start at 0 and end at 1 produces a jump at the
    start or the end of every tween using it."""
    assert ease(0.0) == pytest.approx(0.0)
    assert ease(1.0) == pytest.approx(1.0)


@pytest.mark.parametrize("ease", [ease_linear, ease_out_cubic, ease_in_out_cubic])
def test_easings_are_monotonic(ease):
    """Only ease_out_back is allowed to go backwards, and only at the end."""
    values = [ease(i / 40) for i in range(41)]
    assert all(b >= a for a, b in zip(values, values[1:]))


def test_ease_out_cubic_front_loads_its_motion():
    """The point of it: most of the distance is covered early, so the first
    frame after a click already shows movement."""
    assert ease_out_cubic(0.25) > 0.5


def test_ease_in_out_cubic_is_symmetric():
    for t in (0.1, 0.3, 0.5):
        assert ease_in_out_cubic(t) == pytest.approx(1.0 - ease_in_out_cubic(1.0 - t))


def test_ease_out_back_overshoots_then_settles():
    peak = max(ease_out_back(i / 100) for i in range(101))
    assert peak > 1.0, "no overshoot means no rebound"
    assert peak < 1.2, "a large overshoot reads as a bug, not as physics"


# -- interpolation ---------------------------------------------------------


def test_lerp_endpoints():
    assert lerp(10, 20, 0.0) == 10
    assert lerp(10, 20, 1.0) == 20
    assert lerp(10, 20, 0.5) == 15


def test_lerp_rgb_clamps_and_rounds():
    a, b = (0, 0, 0), (255, 100, 50)
    assert lerp_rgb(a, b, -5.0) == a
    assert lerp_rgb(a, b, 5.0) == b
    blended = lerp_rgb(a, b, 0.5)
    assert blended == (128, 50, 25)
    assert all(isinstance(channel, int) for channel in blended)


def test_lerp_rgb_stays_stable_for_a_render_key():
    """Rounded output means a settled colour stops changing, so memoised
    rendering can actually stop repainting."""
    assert lerp_rgb((0, 0, 0), (10, 10, 10), 0.3333) == lerp_rgb(
        (0, 0, 0), (10, 10, 10), 0.3334
    )


# -- pulse and shake -------------------------------------------------------


def test_pulse_is_continuous_and_bounded():
    values = [pulse(i / 100, 0.5) for i in range(200)]
    assert min(values) >= 0.0
    assert max(values) <= 1.0
    # Continuity is the point: the old badge flipped between two values.
    assert all(abs(b - a) < 0.2 for a, b in zip(values, values[1:]))


def test_pulse_survives_a_zero_period():
    assert pulse(1.23, 0.0) == 1.0


def test_shake_starts_and_ends_at_rest():
    assert shake_offset(0.0) == 0.0
    assert shake_offset(0.24) == 0.0
    assert shake_offset(99.0) == 0.0
    assert shake_offset(-1.0) == 0.0


def test_shake_decays():
    """The second half must be quieter than the first, or it reads as a
    permanent vibration rather than as an impact."""
    early = max(abs(shake_offset(i / 1000)) for i in range(0, 120))
    late = max(abs(shake_offset(i / 1000)) for i in range(120, 240))
    assert early > late


def test_shake_never_exceeds_its_amplitude():
    assert all(abs(shake_offset(i / 1000, amplitude=3.0)) <= 3.0 for i in range(240))


# -- Tween -----------------------------------------------------------------


def test_tween_is_clamped_outside_its_window():
    tween = Tween(0.0, 10.0, 1.0, began_at=5.0)
    assert tween.value(0.0) == 0.0
    assert tween.value(99.0) == 10.0


def test_a_zero_duration_tween_is_already_at_its_end():
    tween = Tween(0.0, 10.0, 0.0, began_at=0.0)
    assert tween.value(0.0) == 10.0
    assert tween.done(0.0)


def test_a_late_frame_lands_where_it_should_have():
    """The reason animation is sampled rather than stepped: a poll tick that
    arrives 3 frames late is still in the right place."""
    tween = Tween(0.0, 100.0, 1.0, began_at=0.0, ease=ease_linear)
    assert tween.value(0.75) == pytest.approx(75.0)


# -- Motion ----------------------------------------------------------------


def test_motion_defaults_when_a_name_is_unknown():
    motion = Motion(clock=FakeClock())
    assert motion.value("nothing", 7.0) == 7.0
    assert motion.target("nothing", 7.0) == 7.0
    assert not motion.moving("nothing")
    assert motion.settled()


def test_reissuing_the_same_target_does_not_restart_the_tween():
    """The load-bearing property: _poll calls to() every frame with the
    current target, so a restart here would freeze the value at its start
    forever."""
    clock = FakeClock()
    motion = Motion(clock=clock)
    motion.set("w", 0.0)
    motion.to("w", 100.0, duration_s=1.0, ease=ease_linear)

    clock.now = 0.5
    halfway = motion.value("w")
    motion.to("w", 100.0, duration_s=1.0, ease=ease_linear)
    assert motion.value("w") == halfway

    clock.now = 1.0
    motion.to("w", 100.0, duration_s=1.0, ease=ease_linear)
    assert motion.value("w") == 100.0


def test_retargeting_starts_from_the_current_value_not_the_original():
    """Otherwise a pointer crossing the dot twice produces a visible snap."""
    clock = FakeClock()
    motion = Motion(clock=clock)
    motion.set("w", 0.0)
    motion.to("w", 100.0, duration_s=1.0, ease=ease_linear)

    clock.now = 0.5
    assert motion.value("w") == pytest.approx(50.0)
    motion.to("w", 0.0, duration_s=1.0, ease=ease_linear)
    # No discontinuity across the retarget.
    assert motion.value("w") == pytest.approx(50.0)

    clock.now = 1.0
    assert motion.value("w") == pytest.approx(25.0)


def test_settled_is_the_frame_rate_signal():
    clock = FakeClock()
    motion = Motion(clock=clock)
    motion.set("a", 0.0)
    assert motion.settled()

    motion.to("a", 1.0, duration_s=0.2)
    assert not motion.settled()

    clock.now = 0.2
    assert motion.settled()


def test_settled_accounts_for_every_tween():
    clock = FakeClock()
    motion = Motion(clock=clock)
    motion.set("a", 0.0)
    motion.set("b", 0.0)
    motion.to("a", 1.0, duration_s=0.1)
    motion.to("b", 1.0, duration_s=0.5)
    clock.now = 0.2
    assert not motion.settled(), "one finished tween is not all of them"
    clock.now = 0.5
    assert motion.settled()


def test_targeting_the_current_value_settles_immediately():
    clock = FakeClock()
    motion = Motion(clock=clock)
    motion.set("a", 5.0)
    motion.to("a", 5.0, duration_s=1.0)
    assert motion.settled()
    assert motion.value("a") == 5.0


def test_a_zero_duration_target_snaps():
    motion = Motion(clock=FakeClock())
    motion.set("a", 0.0)
    motion.to("a", 9.0, duration_s=0.0)
    assert motion.value("a") == 9.0
    assert motion.settled()


def test_forget_and_clear_drop_state():
    motion = Motion(clock=FakeClock())
    motion.set("a", 3.0)
    motion.forget("a")
    assert motion.value("a", -1.0) == -1.0

    motion.set("b", 3.0)
    motion.clear()
    assert motion.value("b", -1.0) == -1.0
    assert motion.settled()


def test_forget_of_an_unknown_name_is_not_an_error():
    Motion(clock=FakeClock()).forget("never-existed")


# -- Timer -----------------------------------------------------------------


def test_a_disarmed_timer_never_elapses():
    timer = Timer(clock=FakeClock())
    assert not timer.armed
    assert not timer.elapsed()
    assert timer.remaining() == 0.0


def test_timer_elapses_at_its_deadline():
    clock = FakeClock()
    timer = Timer(clock=clock)
    timer.arm(0.4)
    assert timer.armed
    assert not timer.elapsed()
    assert timer.remaining() == pytest.approx(0.4)

    clock.now = 0.4
    assert timer.elapsed()
    assert timer.remaining() == 0.0

    timer.disarm()
    assert not timer.elapsed()


def test_arm_if_idle_cannot_push_its_own_deadline_forward():
    """A per-frame caller re-arming a dwell would otherwise never let it
    elapse."""
    clock = FakeClock()
    timer = Timer(clock=clock)
    timer.arm_if_idle(0.4)
    clock.now = 0.3
    timer.arm_if_idle(0.4)
    clock.now = 0.4
    assert timer.elapsed()


def test_arm_rejects_a_negative_delay():
    clock = FakeClock()
    timer = Timer(clock=clock)
    timer.arm(-5.0)
    assert timer.elapsed()
