"""Tests for the pure decision functions in flow.engine.

Per CLAUDE.md the engine's threading/wiring is not unit tested; the functions
here are the parts that are pure arithmetic or pure classification, and they
decide user-visible behaviour (whether a press counts, and how much audio a
dictation actually starts from).
"""

from __future__ import annotations

import pytest

from flow.engine import _failure_label, preroll_start_frame, should_process
from flow.groq_client import GroqError, GroqRateLimitError


# -- hold debouncing ---------------------------------------------------------


@pytest.mark.parametrize(
    "held_ms,min_hold_ms,expected",
    [
        (250.0, 200, True),
        (200.0, 200, True),  # exactly at the threshold counts
        (199.9, 200, False),
        (0.0, 200, False),
        (5.0, 0, True),  # a zero threshold accepts everything
    ],
)
def test_should_process(held_ms: float, min_hold_ms: int, expected: bool):
    assert should_process(held_ms, min_hold_ms) is expected


# -- pre-roll ----------------------------------------------------------------


def test_preroll_reaches_back_before_the_press():
    # 300ms of pre-roll at 16kHz is 4800 frames.
    assert preroll_start_frame(mark=100_000, preroll_frames=4800) == 95_200


def test_preroll_is_clamped_at_the_start_of_the_stream():
    # Pressing the hotkey a moment after launch must not produce a negative
    # start frame, which would corrupt the ring buffer's modulo arithmetic.
    assert preroll_start_frame(mark=1000, preroll_frames=4800) == 0
    assert preroll_start_frame(mark=0, preroll_frames=4800) == 0


def test_zero_preroll_is_the_old_behaviour():
    assert preroll_start_frame(mark=12_345, preroll_frames=0) == 12_345


# -- failure labelling -------------------------------------------------------


def test_rate_limit_failure_label_names_the_limit():
    exc = GroqRateLimitError("nope", limit_type="audio-seconds", retry_after_s=3.0)
    assert _failure_label(exc) == "rate_limited:audio-seconds"


def test_rate_limit_failure_label_without_a_known_limit():
    assert _failure_label(GroqRateLimitError("nope")) == "rate_limited:unknown"


def test_timeout_failure_label():
    assert _failure_label(TimeoutError("too slow")) == "timeout"


def test_other_failures_fall_back_to_the_exception_name():
    assert _failure_label(GroqError("boom")) == "GroqError"
