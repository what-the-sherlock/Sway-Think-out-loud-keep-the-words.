"""Tests for the key-repeat debounce state machine.

CLAUDE.md says not to test the audio callback; the equivalent guidance for
the hotkey listener is to test the pure debounce logic (KeyHoldTracker) and
leave the thin pynput.Listener wrapper (HotkeyListener) untested, since it
has no logic of its own beyond delegating to the tracker.
"""

from __future__ import annotations

from sway.hotkey import KNOWN_KEYS, KeyHoldTracker
from sway.config import _VALID_HOTKEYS  # noqa: PLC2701 - deliberately checking sync, see below


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
