"""Tests for the key-repeat debounce state machine.

CLAUDE.md says not to test the audio callback; the equivalent guidance for
the hotkey listener is to test the pure debounce logic (KeyHoldTracker) and
leave the thin pynput.Listener wrapper (HotkeyListener) untested, since it
has no logic of its own beyond delegating to the tracker.
"""

from __future__ import annotations

from pynput import keyboard

from sway.hotkey import (
    CHORD_KEYS,
    CHORD_MODIFIERS,
    KNOWN_KEYS,
    DoubleTapTracker,
    HotkeyListener,
    KeyHoldTracker,
    TapModeTracker,
    find_binding_conflicts,
    parse_chord,
)
from sway.config import (  # noqa: PLC2701 - deliberately checking sync, see below
    _RESERVED_CHORD_SHAPES,
    _VALID_CHORD_KEY_NAMES,
    _VALID_CHORD_MODIFIERS,
    _VALID_HOTKEYS,
    _is_valid_chord_shape,
)
from sway.hotkey import _RESERVED_CHORDS  # noqa: PLC2701 - deliberately checking sync, see below


def test_first_press_fires():
    tracker = KeyHoldTracker()
    assert tracker.press() is True


def test_os_key_repeat_does_not_refire_press():
    tracker = KeyHoldTracker()
    assert tracker.press() is True
    assert tracker.press() is False
    assert tracker.press() is False


def test_release_after_press_fires_once():
    tracker = KeyHoldTracker()
    tracker.press()
    assert tracker.release() is True
    assert tracker.release() is False


def test_release_without_a_prior_press_does_not_fire():
    tracker = KeyHoldTracker()
    assert tracker.release() is False


def test_press_release_press_cycle():
    tracker = KeyHoldTracker()
    assert tracker.press() is True
    assert tracker.release() is True
    assert tracker.press() is True
    assert tracker.release() is True


def test_known_keys_match_configs_validation_set():
    # config.py duplicates this set (as plain strings) rather than importing
    # pynput; this test is the tripwire that catches the two drifting apart.
    assert set(KNOWN_KEYS) == _VALID_HOTKEYS


# --------------------------------------------------------------------------
# HotkeyListener.rebind -- pure attribute swap, no pynput.Listener involved,
# so worth testing directly despite the module's "thin wrapper, untested"
# philosophy above (that philosophy is about the pynput plumbing, not this).
# --------------------------------------------------------------------------


def test_rebind_swaps_the_bound_key():
    listener = HotkeyListener(key=keyboard.Key.ctrl_r, on_press=lambda: None, on_release=lambda: None)
    listener.rebind(keyboard.Key.alt_l)
    assert listener._key is keyboard.Key.alt_l


def test_rebind_resets_the_hold_tracker():
    listener = HotkeyListener(key=keyboard.Key.ctrl_r, on_press=lambda: None, on_release=lambda: None)
    # Simulate a hold in flight under the old key.
    listener._tracker.press()
    listener.rebind(keyboard.Key.alt_l)
    # A fresh tracker: the abandoned hold under the old key must not still
    # be "held" under the new one.
    assert listener._tracker.press() is True


# --------------------------------------------------------------------------
# TapModeTracker
# --------------------------------------------------------------------------


def test_tap_mode_first_tap_starts():
    tracker = TapModeTracker(max_active_s=60)
    assert tracker.tap(0.0) == "start"
    assert tracker.active is True


def test_tap_mode_second_tap_stops():
    tracker = TapModeTracker(max_active_s=60)
    tracker.tap(0.0)
    assert tracker.tap(1.0) == "stop"
    assert tracker.active is False


def test_tap_mode_tap_after_timeout_reports_stop_timeout():
    tracker = TapModeTracker(max_active_s=10)
    tracker.tap(0.0)
    assert tracker.tap(20.0) == "stop_timeout"
    assert tracker.active is False


def test_tap_mode_check_timeout_force_closes_an_overrun_session():
    tracker = TapModeTracker(max_active_s=10)
    tracker.tap(0.0)
    assert tracker.check_timeout(20.0) is True
    assert tracker.active is False


def test_tap_mode_check_timeout_is_idempotent():
    tracker = TapModeTracker(max_active_s=10)
    tracker.tap(0.0)
    tracker.check_timeout(20.0)
    assert tracker.check_timeout(21.0) is False


def test_tap_mode_check_timeout_does_nothing_while_inactive():
    tracker = TapModeTracker(max_active_s=10)
    assert tracker.check_timeout(1000.0) is False


# --------------------------------------------------------------------------
# DoubleTapTracker
# --------------------------------------------------------------------------


def test_double_tap_lone_tap_does_nothing():
    tracker = DoubleTapTracker(max_active_s=60, window_s=0.35)
    assert tracker.tap(0.0) is None
    assert tracker.active is False


def test_double_tap_within_window_starts():
    tracker = DoubleTapTracker(max_active_s=60, window_s=0.35)
    tracker.tap(0.0)
    assert tracker.tap(0.2) == "start"
    assert tracker.active is True


def test_double_tap_outside_window_is_treated_as_a_fresh_first_tap():
    tracker = DoubleTapTracker(max_active_s=60, window_s=0.35)
    tracker.tap(0.0)
    assert tracker.tap(1.0) is None  # too slow to pair with the first tap
    assert tracker.tap(1.2) == "start"  # pairs with the *second* tap instead


def test_double_tap_second_pair_stops():
    tracker = DoubleTapTracker(max_active_s=60, window_s=0.35)
    tracker.tap(0.0)
    tracker.tap(0.2)  # start
    tracker.tap(5.0)
    assert tracker.tap(5.2) == "stop"


def test_double_tap_check_timeout_delegates():
    tracker = DoubleTapTracker(max_active_s=10, window_s=0.35)
    tracker.tap(0.0)
    tracker.tap(0.2)  # start
    assert tracker.check_timeout(20.0) is True
    assert tracker.active is False


# --------------------------------------------------------------------------
# find_binding_conflicts
# --------------------------------------------------------------------------


def test_find_binding_conflicts_reports_nothing_when_all_distinct():
    assert find_binding_conflicts({"dictation": "ctrl_r", "command": "shift_r"}) == {}


def test_find_binding_conflicts_is_symmetric():
    conflicts = find_binding_conflicts({"dictation": "ctrl_r", "hands_free": "ctrl_r"})
    assert conflicts == {"dictation": ["hands_free"], "hands_free": ["dictation"]}


def test_find_binding_conflicts_reports_a_three_way_collision():
    conflicts = find_binding_conflicts({"a": "ctrl_r", "b": "ctrl_r", "c": "ctrl_r"})
    assert set(conflicts) == {"a", "b", "c"}
    assert conflicts["a"] == ["b", "c"]


def test_find_binding_conflicts_does_not_confuse_a_hold_key_with_a_chord():
    # "ctrl_r" (a hold-key name) and "ctrl+r" (a chord string) are different
    # strings and must never be reported as colliding.
    assert find_binding_conflicts({"dictation": "ctrl_r", "transform": "ctrl+r"}) == {}


# --------------------------------------------------------------------------
# Reserved chords -- must be rejected by parse_chord with a specific reason.
# --------------------------------------------------------------------------


def test_parse_chord_rejects_reserved_combinations():
    for reserved in _RESERVED_CHORDS:
        try:
            parse_chord(reserved)
        except Exception as exc:  # ChordError
            assert "ChordError" in type(exc).__name__
        else:
            raise AssertionError(f"{reserved!r} should have been rejected")


# --------------------------------------------------------------------------
# config.py's pynput-free chord-shape validator must never drift from
# hotkey.parse_chord's actual acceptance rules.
# --------------------------------------------------------------------------


def test_chord_modifiers_match_configs_validation_set():
    assert set(CHORD_MODIFIERS) == _VALID_CHORD_MODIFIERS


def test_chord_key_names_match_configs_validation_set():
    assert set(CHORD_KEYS) == _VALID_CHORD_KEY_NAMES


def test_reserved_chords_match_configs_validation_set():
    assert set(_RESERVED_CHORDS) == _RESERVED_CHORD_SHAPES


def test_is_valid_chord_shape_agrees_with_parse_chord():
    candidates = [
        "ctrl+alt+p",
        "alt+ctrl+p",  # order-independent
        "Shift+Z",  # case-independent
        "cmd+space",
        "ctrl+f13",
        "",  # empty
        "p",  # no modifier
        "ctrl+alt",  # no terminal key
        "ctrl+нет",  # not alnum / not a known key name
        "ctrl+c",  # reserved
        "ctrl+v",  # reserved
        "cmd+l",  # reserved
        "banana+p",  # unknown modifier
    ]
    for candidate in candidates:
        try:
            parse_chord(candidate)
            expected_valid = True
        except Exception:
            expected_valid = False
        assert _is_valid_chord_shape(candidate) == expected_valid, candidate


def test_is_valid_chord_shape_rejects_every_reserved_combination():
    for reserved in _RESERVED_CHORD_SHAPES:
        assert _is_valid_chord_shape(reserved) is False
