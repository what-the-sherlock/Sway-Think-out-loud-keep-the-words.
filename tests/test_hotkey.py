"""Tests for the pure logic in sway.hotkey.

CLAUDE.md says not to test the audio callback; the equivalent guidance here
is to test the pure pieces -- binding parsing, the debounce trackers, and the
modifier-combo hold gate's state machine -- and leave the thin pynput.Listener
wrapper (BindingListener's OS-hook plumbing) untested, since it has no logic
of its own beyond delegating to them.
"""

from __future__ import annotations

import sys
import time

import pytest

from sway.hotkey import (
    CHORD_KEYS,
    KNOWN_KEYS,
    MODIFIER_KEYS,
    MOUSE_BUTTONS,
    PREFIX_GRACE_S,
    Binding,
    BindingError,
    BindingListener,
    DoubleTapTracker,
    KeyHoldTracker,
    ModifierHoldGate,
    RefCountedGate,
    TapModeTracker,
    _RESERVED_BINDINGS,
    _key_name,
    bare_hold_generic_modifier,
    binding_advisory,
    effective_bindings,
    find_binding_conflicts,
    find_binding_list_conflicts,
    format_binding,
    needs_prefix_grace,
    parse_binding,
)
from sway.config import (  # noqa: PLC2701 - deliberately checking sync, see below
    _RESERVED_CHORD_SHAPES,
    _VALID_CHORD_KEY_NAMES,
    _VALID_CHORD_MODIFIERS,
    _VALID_HOTKEYS,
    _is_valid_chord_shape,
)


# --------------------------------------------------------------------------
# KeyHoldTracker
# --------------------------------------------------------------------------


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
# parse_binding -- every accepted shape
# --------------------------------------------------------------------------


def test_parse_binding_bare_hold_key():
    b = parse_binding("ctrl_r")
    assert b.modifiers == frozenset()
    assert b.key == "ctrl_r"
    assert b.mouse == ""
    assert b.shape == "hold"


def test_parse_binding_bare_mouse_button():
    b = parse_binding("mouse:middle")
    assert b.modifiers == frozenset()
    assert b.mouse == "middle"
    assert b.shape == "hold"


def test_parse_binding_rejects_left_and_right_mouse():
    for name in ("left", "right"):
        try:
            parse_binding(f"mouse:{name}")
        except BindingError as exc:
            assert "clicking" in str(exc)
        else:
            raise AssertionError(f"mouse:{name} should have been rejected")


def test_parse_binding_rejects_unknown_mouse_button():
    try:
        parse_binding("mouse:scroll")
    except BindingError:
        pass
    else:
        raise AssertionError("mouse:scroll should have been rejected")


def test_parse_binding_modifier_only_combo():
    b = parse_binding("ctrl+cmd")
    assert b.modifiers == frozenset({"ctrl", "cmd"})
    assert b.key == ""
    assert b.mouse == ""
    assert b.shape == "hold"
    assert b.is_modifier_combo is True


def test_parse_binding_modifier_only_combo_is_order_independent():
    assert parse_binding("ctrl+cmd") == parse_binding("cmd+ctrl")


def test_parse_binding_single_modifier_alone_is_rejected():
    # A single bare modifier is ambiguous with the sided hold-key names
    # (ctrl_r/ctrl_l) -- it must be spelled one way or the other.
    try:
        parse_binding("ctrl")
    except BindingError:
        pass
    else:
        raise AssertionError("a single bare modifier should be rejected")


def test_parse_binding_chord_with_letter():
    b = parse_binding("ctrl+alt+p")
    assert b.modifiers == frozenset({"ctrl", "alt"})
    assert b.key == "p"
    assert b.shape == "tap"
    assert b.is_modifier_combo is False


def test_parse_binding_chord_with_mouse():
    b = parse_binding("ctrl+mouse:back")
    assert b.modifiers == frozenset({"ctrl"})
    assert b.mouse == "back"
    assert b.shape == "tap"


def test_parse_binding_chord_with_named_key():
    b = parse_binding("ctrl+cmd+space")
    assert b.modifiers == frozenset({"ctrl", "cmd"})
    assert b.key == "space"
    assert b.shape == "tap"


def test_parse_binding_bare_unmodified_letter_is_rejected():
    try:
        parse_binding("p")
    except BindingError:
        pass
    else:
        raise AssertionError("a bare letter should be rejected")


def test_parse_binding_hold_key_cannot_take_a_modifier():
    try:
        parse_binding("ctrl+ctrl_r")
    except BindingError:
        pass
    else:
        raise AssertionError("a hold-key name combined with a modifier should be rejected")


def test_parse_binding_rejects_reserved_chords():
    for reserved in _RESERVED_BINDINGS:
        try:
            parse_binding(reserved)
        except BindingError:
            pass
        else:
            raise AssertionError(f"{reserved!r} should have been rejected")


def test_parse_binding_case_and_whitespace_insensitive():
    assert parse_binding("Ctrl + Alt + P") == parse_binding("ctrl+alt+p")


def test_parse_binding_empty_string_rejected():
    try:
        parse_binding("")
    except BindingError:
        pass
    else:
        raise AssertionError("empty string should be rejected")


def test_format_binding_returns_canonical_form():
    assert format_binding("alt+ctrl+p") == "ctrl+alt+p"


def test_format_binding_returns_input_unchanged_when_unparseable():
    assert format_binding("not a binding") == "not a binding"


def test_binding_equality_and_hash():
    a = parse_binding("ctrl+alt+p")
    b = parse_binding("alt+ctrl+p")
    assert a == b
    assert hash(a) == hash(b)
    assert a != parse_binding("ctrl+alt+q")


def test_binding_pretty_labels():
    assert parse_binding("ctrl_r").pretty() == "Right Ctrl"
    assert parse_binding("ctrl+cmd").pretty() == "Ctrl + Win"
    assert parse_binding("ctrl+alt+p").pretty() == "Ctrl + Alt + P"
    assert parse_binding("mouse:middle").pretty() == "Middle Click"
    assert parse_binding("ctrl+mouse:back").pretty() == "Ctrl + Back Button"


# --------------------------------------------------------------------------
# ModifierHoldGate -- pure state machine
# --------------------------------------------------------------------------


def test_gate_engages_immediately_with_no_grace_needed():
    gate = ModifierHoldGate()
    gen = gate.on_satisfied()
    assert gate.try_engage(gen) is True
    assert gate.engaged is True


def test_gate_does_not_engage_twice_for_the_same_generation():
    gate = ModifierHoldGate()
    gen = gate.on_satisfied()
    gate.try_engage(gen)
    assert gate.try_engage(gen) is False


def test_gate_disengage_only_fires_if_it_had_engaged():
    gate = ModifierHoldGate()
    gen = gate.on_satisfied()
    gate.try_engage(gen)
    assert gate.on_unsatisfied() is True


def test_gate_disengage_does_not_fire_if_never_engaged():
    gate = ModifierHoldGate()
    gate.on_satisfied()  # satisfied, but try_engage never called (e.g. preempted)
    assert gate.on_unsatisfied() is False


def test_gate_preempt_blocks_a_pending_engage():
    gate = ModifierHoldGate()
    gen = gate.on_satisfied()
    gate.preempt(gen)
    assert gate.try_engage(gen) is False
    assert gate.engaged is False


def test_gate_preempt_of_a_stale_generation_does_nothing():
    gate = ModifierHoldGate()
    gate.on_satisfied()
    gate.on_unsatisfied()  # generation advances, first attempt abandoned
    gen2 = gate.on_satisfied()
    gate.preempt(1)  # a stale token from the first attempt
    assert gate.try_engage(gen2) is True  # the second attempt is unaffected


def test_gate_release_then_repress_is_a_fresh_sequence():
    gate = ModifierHoldGate()
    gen1 = gate.on_satisfied()
    gate.try_engage(gen1)
    gate.on_unsatisfied()
    gen2 = gate.on_satisfied()
    assert gen2 != gen1
    assert gate.try_engage(gen2) is True


def test_gate_stale_generation_cannot_engage_after_release_and_repress():
    gate = ModifierHoldGate()
    gen1 = gate.on_satisfied()
    gate.on_unsatisfied()  # released before try_engage ever ran (e.g. grace window)
    gate.on_satisfied()
    assert gate.try_engage(gen1) is False


# --------------------------------------------------------------------------
# needs_prefix_grace
# --------------------------------------------------------------------------


def test_needs_prefix_grace_true_when_modifiers_exactly_match_a_tap_binding():
    hold = parse_binding("ctrl+cmd")
    tap = parse_binding("ctrl+cmd+space")
    assert needs_prefix_grace(hold, [tap]) is True


def test_needs_prefix_grace_true_when_hold_modifiers_are_a_strict_subset():
    hold = parse_binding("ctrl+cmd")
    tap = parse_binding("ctrl+cmd+alt+p")
    assert needs_prefix_grace(hold, [tap]) is True


def test_needs_prefix_grace_false_with_no_overlapping_tap_binding():
    hold = parse_binding("ctrl+cmd")
    tap = parse_binding("shift+alt+p")
    assert needs_prefix_grace(hold, [tap]) is False


def test_needs_prefix_grace_false_when_tap_modifiers_are_a_subset_of_hold():
    # The reverse relationship does not create ambiguity: completing the tap
    # binding here needs fewer modifiers than the hold requires, so the hold
    # can never accidentally engage mid-chord.
    hold = parse_binding("ctrl+cmd+shift")
    tap = parse_binding("ctrl+p")
    assert needs_prefix_grace(hold, [tap]) is False


def test_needs_prefix_grace_false_for_a_bare_mouse_hold():
    hold = parse_binding("mouse:middle")
    tap = parse_binding("ctrl+mouse:middle")
    assert needs_prefix_grace(hold, [tap]) is False


def test_needs_prefix_grace_false_with_no_tap_bindings_at_all():
    hold = parse_binding("ctrl+cmd")
    assert needs_prefix_grace(hold, []) is False


# --------------------------------------------------------------------------
# needs_prefix_grace / bare_hold_generic_modifier -- a bare hold on a sided
# modifier key (the app's own default dictation binding, ctrl_r) engages
# unconditionally the instant that key is pressed, with no way to wait and
# see whether more keys are coming. Anything else requiring the same generic
# modifier -- a combo, or a chord -- is therefore a physical *prefix* of that
# other binding and needs the same grace-then-preempt treatment a modifier
# combo already gets (see BindingListener._begin_hold_locked). The
# regression this pins: a transform on ctrl+cmd (Ctrl+Win), or on
# ctrl+shift+t, saved while dictation is still bound to bare ctrl_r used to
# silently also start a dictation recording on every use.
# --------------------------------------------------------------------------


def test_bare_hold_generic_modifier_for_a_sided_modifier_key():
    assert bare_hold_generic_modifier(parse_binding("ctrl_r")) == "ctrl"
    assert bare_hold_generic_modifier(parse_binding("ctrl_l")) == "ctrl"
    assert bare_hold_generic_modifier(parse_binding("shift_r")) == "shift"


def test_bare_hold_generic_modifier_none_for_a_combo_mouse_or_non_modifier_hold():
    assert bare_hold_generic_modifier(parse_binding("ctrl+cmd")) is None
    assert bare_hold_generic_modifier(parse_binding("mouse:middle")) is None
    assert bare_hold_generic_modifier(parse_binding("caps_lock")) is None
    assert bare_hold_generic_modifier(parse_binding("f13")) is None


def test_needs_prefix_grace_true_for_a_bare_hold_against_a_chord_sharing_its_modifier():
    bare = parse_binding("ctrl_r")
    chord = parse_binding("ctrl+shift+t")
    assert needs_prefix_grace(bare, [chord]) is True


def test_needs_prefix_grace_true_for_a_bare_hold_against_a_combo_sharing_its_modifier():
    bare = parse_binding("ctrl_r")
    combo = parse_binding("ctrl+cmd")
    assert needs_prefix_grace(bare, [combo]) is True


def test_needs_prefix_grace_false_for_a_bare_hold_with_no_overlapping_modifier():
    bare = parse_binding("ctrl_r")
    unrelated = parse_binding("alt+shift+p")
    assert needs_prefix_grace(bare, [unrelated]) is False


def test_needs_prefix_grace_false_for_caps_lock_and_f13_bare_holds():
    # Not modifiers, so they can never be a physical prefix of anything.
    combo = parse_binding("ctrl+cmd")
    assert needs_prefix_grace(parse_binding("caps_lock"), [combo]) is False
    assert needs_prefix_grace(parse_binding("f13"), [combo]) is False


def test_needs_prefix_grace_false_for_a_bare_mouse_hold_against_anything():
    combo = parse_binding("ctrl+cmd")
    assert needs_prefix_grace(parse_binding("mouse:middle"), [combo]) is False


def test_needs_prefix_grace_two_combos_sharing_a_modifier_do_not_need_grace_from_each_other():
    # Holding Ctrl+Alt on the way to Ctrl+Alt+P does not, by itself, engage
    # anything -- only a bare hold, or a combo vs. a genuine tap prefix, has
    # this problem. A combo is never itself the "other" that grades another
    # combo's grace (the combo branch only ever compares against taps).
    combo_a = parse_binding("ctrl+cmd")
    combo_b = parse_binding("ctrl+cmd+alt")
    assert needs_prefix_grace(combo_a, [combo_b]) is False


def test_needs_prefix_grace_identical_binding_does_not_grace_itself():
    # A bare hold's own (empty) modifier set can never satisfy "generic in
    # other.modifiers" for a copy of itself, so no self-exclusion is needed.
    bare = parse_binding("ctrl_r")
    assert needs_prefix_grace(bare, [bare]) is False


# --------------------------------------------------------------------------
# config.py's pynput-free chord-shape validator must never drift from
# sway.hotkey's actual acceptance rules for the (still-supported) plain
# "modifiers + key" chord shape.
# --------------------------------------------------------------------------


def test_chord_modifiers_match_configs_validation_set():
    assert set(MODIFIER_KEYS) == _VALID_CHORD_MODIFIERS


def test_chord_key_names_match_configs_validation_set():
    assert set(CHORD_KEYS) == _VALID_CHORD_KEY_NAMES


def test_reserved_chords_match_configs_validation_set():
    # config's mirror only covers the classic "modifiers + key" reserved
    # shapes (it predates mouse bindings and cannot import pynput to learn
    # about them), so it is a subset of the full reserved-binding set here.
    assert set(_RESERVED_CHORD_SHAPES) <= set(_RESERVED_BINDINGS)


def test_is_valid_chord_shape_agrees_with_parse_binding_for_key_chords():
    candidates = [
        "ctrl+alt+p",
        "alt+ctrl+p",  # order-independent
        "Shift+Z",  # case-independent
        "cmd+space",
        "ctrl+f13",
        "",  # empty
        "p",  # no modifier
        "ctrl+alt",  # no terminal key -- a modifier-only combo, valid as a Binding but not as a plain chord
        "ctrl+нет",  # not alnum / not a known key name
        "ctrl+c",  # reserved
        "ctrl+v",  # reserved
        "cmd+l",  # reserved
        "banana+p",  # unknown modifier
    ]
    for candidate in candidates:
        try:
            binding = parse_binding(candidate)
            expected_valid = binding.mouse == "" and binding.key != "" and binding.shape == "tap"
        except BindingError:
            expected_valid = False
        assert _is_valid_chord_shape(candidate) == expected_valid, candidate


def test_is_valid_chord_shape_rejects_every_reserved_combination():
    for reserved in _RESERVED_CHORD_SHAPES:
        assert _is_valid_chord_shape(reserved) is False


# --------------------------------------------------------------------------
# RefCountedGate
# --------------------------------------------------------------------------


def test_ref_gate_single_acquire_release():
    gate = RefCountedGate()
    assert gate.acquire() is True
    assert gate.active is True
    assert gate.release() is True
    assert gate.active is False


def test_ref_gate_second_acquire_does_not_refire_start():
    gate = RefCountedGate()
    assert gate.acquire() is True
    assert gate.acquire() is False
    assert gate.active is True


def test_ref_gate_stop_only_fires_on_the_last_release():
    gate = RefCountedGate()
    gate.acquire()
    gate.acquire()
    assert gate.release() is False  # one binding still held
    assert gate.active is True
    assert gate.release() is True  # the last one lets go
    assert gate.active is False


def test_ref_gate_release_with_nothing_held_is_a_harmless_no_op():
    gate = RefCountedGate()
    assert gate.release() is False
    assert gate.active is False


def test_ref_gate_handles_out_of_order_acquire_release():
    # Two bindings held, released in an arbitrary order -- the gate does not
    # care which binding acquired or released, only the count.
    gate = RefCountedGate()
    gate.acquire()
    gate.acquire()
    gate.acquire()
    assert gate.release() is False
    assert gate.release() is False
    assert gate.release() is True


# --------------------------------------------------------------------------
# effective_bindings
# --------------------------------------------------------------------------


def test_effective_bindings_uses_override_when_present():
    assert effective_bindings(("ctrl+cmd",), "ctrl_r", "ctrl+alt+p") == ("ctrl+cmd",)


def test_effective_bindings_falls_back_to_defaults_when_never_customized():
    assert effective_bindings(None, "ctrl_r", "ctrl+alt+p") == ("ctrl_r", "ctrl+alt+p")


def test_effective_bindings_falls_back_filters_out_unset_defaults():
    assert effective_bindings(None, "ctrl_r", None) == ("ctrl_r",)


def test_effective_bindings_explicit_empty_override_wins_outright():
    # A user who removed every binding gets none -- not config's defaults.
    assert effective_bindings((), "ctrl_r", "ctrl+alt+p") == ()


def test_effective_bindings_with_no_defaults_at_all():
    assert effective_bindings(None) == ()


# --------------------------------------------------------------------------
# find_binding_list_conflicts
# --------------------------------------------------------------------------


def test_find_binding_list_conflicts_none_when_disjoint():
    assert find_binding_list_conflicts({"a": ["ctrl_r"], "b": ["shift_r"]}) == {}


def test_find_binding_list_conflicts_flags_a_shared_binding_even_with_others_distinct():
    conflicts = find_binding_list_conflicts(
        {"a": ["ctrl_r", "ctrl+alt+p"], "b": ["shift_r", "ctrl+alt+p"]}
    )
    assert conflicts == {"a": ["b"], "b": ["a"]}


def test_find_binding_list_conflicts_ignores_empty_lists():
    assert find_binding_list_conflicts({"a": [], "b": []}) == {}


# --------------------------------------------------------------------------
# BindingListener -- one integration-style test of the genuinely risky
# interaction: a settings-triggered rebind arriving while a prefix-grace
# timer is still pending for a slot id that will be reused. Manipulates the
# listener's internal state directly (no real pynput hook is started),
# mirroring how HotkeyListener's own private state was already accessed in
# this file's tests before this rewrite.
# --------------------------------------------------------------------------


def test_rebind_cancels_a_pending_prefix_grace_timer():
    listener = BindingListener()
    hold = parse_binding("ctrl+cmd")
    tap = parse_binding("ctrl+cmd+space")
    fired = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (
                hold,
                lambda t: fired.append("old-engaged"),
                lambda: fired.append("old-disengaged"),
            )
        },
        tap_bindings={"hands_free:0": (tap, lambda t: None)},
    )
    # Both modifiers becoming held should schedule (not immediately fire)
    # the grace-delayed engage, since a tap binding shares the same prefix.
    listener._down_mods = {"ctrl", "cmd"}
    listener._reevaluate_combo_holds_locked(time.monotonic())
    assert "dictation:0" in listener._timers

    # A settings change rebinds mid-grace-window, reusing the same slot id
    # for a different callback.
    listener.rebind(
        hold_bindings={
            "dictation:0": (
                hold,
                lambda t: fired.append("new-engaged"),
                lambda: fired.append("new-disengaged"),
            )
        },
        tap_bindings={},
    )
    assert listener._timers == {}  # the stale timer must not survive the rebind

    time.sleep(0.3)  # past PREFIX_GRACE_S
    assert fired == []  # neither the old nor the new callback fired


def test_a_bare_sided_hold_key_fires_even_though_it_is_also_a_modifier():
    """The regression: ctrl_r (Sway's own default dictation key) is *also* a
    generic modifier by pynput identity (MODIFIER_KEYS["ctrl"] contains it),
    so a naive "if it's a modifier, handle it and return" dispatch order
    means a bare hold on ctrl_r can never reach the bare-hold-key check at
    all -- the single most important gesture in the app, broken for anyone
    using the default. Exercises the real dispatch methods directly (no OS
    hook), matching this file's existing precedent of poking private
    listener state (see test_rebind_cancels_a_pending_prefix_grace_timer).
    No competing binding is registered, so this hold needs no grace and
    engages the instant it is pressed."""
    from pynput import keyboard as kb

    listener = BindingListener()
    binding = parse_binding("ctrl_r")
    engaged = []
    disengaged = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (binding, lambda t: engaged.append(1), lambda: disengaged.append(1))
        },
        tap_bindings={},
    )

    listener._on_key_press(kb.Key.ctrl_r)
    assert engaged == [1]

    listener._on_key_release(kb.Key.ctrl_r)
    assert disengaged == [1]


def test_a_bare_sided_hold_key_still_contributes_to_a_combo_held_elsewhere():
    """The fix must not regress the other direction: ctrl_r bound bare for
    dictation must still count as "ctrl" being down for an unrelated
    modifier-combo binding (e.g. a transform on ctrl+cmd) -- and per the
    grace fix below, engaging that combo preempts the still-pending bare
    hold rather than letting both fire."""
    from pynput import keyboard as kb

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    combo = parse_binding("ctrl+cmd")
    dictation_engaged = []
    combo_engaged = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (dictation, lambda t: dictation_engaged.append(1), lambda: None),
            "transform:x:0": (combo, lambda t: combo_engaged.append(1), lambda: None),
        },
        tap_bindings={},
    )

    listener._on_key_press(kb.Key.ctrl_r)
    listener._on_key_press(kb.Key.cmd)
    time.sleep(PREFIX_GRACE_S + 0.05)
    assert combo_engaged == [1]  # ctrl_r's contribution to "ctrl" completed the combo
    assert dictation_engaged == []  # preempted by the combo winning


# --------------------------------------------------------------------------
# BindingListener -- prefix grace extended to bare hold keys. A bare hold on
# a sided modifier key (ctrl_r) is now graced, exactly like a modifier
# combo, whenever some other registered binding needs the same generic
# modifier -- see needs_prefix_grace and bare_hold_generic_modifier above.
# --------------------------------------------------------------------------


def test_bare_hold_held_past_grace_engages_with_the_true_press_instant():
    from pynput import keyboard as kb

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    transform = parse_binding("ctrl+shift+t")
    engaged = []
    listener.rebind(
        hold_bindings={"dictation:0": (dictation, lambda t: engaged.append(t), lambda: None)},
        tap_bindings={"transform:x:0": (transform, lambda t: None)},
    )

    before = time.monotonic()
    listener._on_key_press(kb.Key.ctrl_r)
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert len(engaged) == 1
    # pressed_at is the moment the key physically went down, not when the
    # (deferred) engage actually ran.
    assert before <= engaged[0] < before + PREFIX_GRACE_S


def test_a_chord_sharing_the_bare_holds_modifier_fires_without_engaging_it():
    """The core regression this session fixes: Ctrl+Shift+T must run the
    transform without also starting dictation, even though dictation is a
    bare hold on ctrl_r, the chord's first key."""
    from pynput import keyboard as kb
    from pynput.keyboard import KeyCode

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    transform = parse_binding("ctrl+shift+t")
    dictation_engaged = []
    dictation_disengaged = []
    transform_fired = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (
                dictation,
                lambda t: dictation_engaged.append(t),
                lambda: dictation_disengaged.append(1),
            )
        },
        tap_bindings={"transform:x:0": (transform, lambda t: transform_fired.append(t))},
    )

    listener._on_key_press(kb.Key.ctrl_r)
    listener._on_key_press(kb.Key.shift)
    listener._on_key_press(KeyCode.from_char("t"))
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert transform_fired
    assert dictation_engaged == []

    listener._on_key_release(KeyCode.from_char("t"))
    listener._on_key_release(kb.Key.shift)
    listener._on_key_release(kb.Key.ctrl_r)
    assert dictation_disengaged == []  # no engage means no disengage either
    assert listener._timers == {}


def test_bare_hold_released_inside_grace_fires_nothing_and_leaves_no_timer():
    """A fast tap of ctrl_r (below min_hold_ms anyway, see should_process)
    must not leave a dangling timer or an orphan disengage."""
    from pynput import keyboard as kb

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    transform = parse_binding("ctrl+shift+t")
    engaged = []
    disengaged = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (dictation, lambda t: engaged.append(1), lambda: disengaged.append(1))
        },
        tap_bindings={"transform:x:0": (transform, lambda t: None)},
    )

    listener._on_key_press(kb.Key.ctrl_r)
    listener._on_key_release(kb.Key.ctrl_r)
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert engaged == []
    assert disengaged == []
    assert listener._timers == {}


def test_bare_hold_key_repeat_engages_only_once():
    from pynput import keyboard as kb

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    transform = parse_binding("ctrl+shift+t")
    engaged = []
    listener.rebind(
        hold_bindings={"dictation:0": (dictation, lambda t: engaged.append(1), lambda: None)},
        tap_bindings={"transform:x:0": (transform, lambda t: None)},
    )

    listener._on_key_press(kb.Key.ctrl_r)  # OS key-repeat: same key, no release between
    listener._on_key_press(kb.Key.ctrl_r)
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert engaged == [1]
    listener._on_key_release(kb.Key.ctrl_r)


def test_bare_hold_vs_combo_reverse_press_order_still_lets_the_combo_win():
    """cmd pressed first, then ctrl_r -- the combo must still win over the
    bare hold regardless of which key went down first."""
    from pynput import keyboard as kb

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    combo = parse_binding("ctrl+cmd")
    dictation_engaged = []
    combo_engaged = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (dictation, lambda t: dictation_engaged.append(1), lambda: None),
            "transform:x:0": (combo, lambda t: combo_engaged.append(1), lambda: None),
        },
        tap_bindings={},
    )

    listener._on_key_press(kb.Key.cmd)
    listener._on_key_press(kb.Key.ctrl_r)
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert combo_engaged == [1]
    assert dictation_engaged == []


def test_two_graced_holds_pending_only_the_superset_engages_either_timer_order():
    """dictation=ctrl_r (bare) and command=ctrl+alt (combo) both need grace
    against a ctrl+alt+p chord. Holding all three keys should engage only
    the combo (the superset), never the bare hold underneath it -- checked
    with the bare hold's own timer firing first, the harder ordering."""
    from pynput import keyboard as kb
    from pynput.keyboard import KeyCode

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    combo = parse_binding("ctrl+alt")
    chord = parse_binding("ctrl+alt+p")
    dictation_engaged = []
    combo_engaged = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (dictation, lambda t: dictation_engaged.append(1), lambda: None),
            "command:0": (combo, lambda t: combo_engaged.append(1), lambda: None),
        },
        tap_bindings={"transform:x:0": (chord, lambda t: None)},
    )

    listener._on_key_press(kb.Key.ctrl_r)  # schedules the bare hold's grace timer first
    listener._on_key_press(kb.Key.alt)  # schedules the combo's grace timer second
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert combo_engaged == [1]
    assert dictation_engaged == []  # preempted once the superset combo engaged

    listener._on_key_release(kb.Key.alt)
    listener._on_key_release(kb.Key.ctrl_r)


# --------------------------------------------------------------------------
# BindingListener.suspended() -- Sway's own synthetic Ctrl+C/Ctrl+V must
# never be mistaken for a real keystroke by its own listener. pynput 1.8's
# low-level hook reports whether an event was OS-injected (LLKHF_INJECTED on
# Windows), so suspended() only ignores events the caller passes
# injected=True for -- a real keystroke that happens to land inside the
# window is dispatched normally. See test_suspended_processes_a_real_release
# below for the regression an earlier, blunter version of this had: ignoring
# *everything* during the window, which could swallow a real release and
# wedge a binding permanently (KeyHoldTracker._held stuck True forever).
# --------------------------------------------------------------------------


def test_suspended_blocks_a_synthetic_keystroke_from_corrupting_combo_state():
    from pynput import keyboard as kb

    listener = BindingListener()
    combo = parse_binding("ctrl+cmd")
    engaged: list[int] = []
    disengaged: list[int] = []
    listener.rebind(
        hold_bindings={
            "transform:x:0": (combo, lambda t: engaged.append(1), lambda: disengaged.append(1))
        },
        tap_bindings={},
    )

    # The user holds Ctrl+Win: the combo engages.
    listener._on_key_press(kb.Key.ctrl_r)
    listener._on_key_press(kb.Key.cmd)
    assert engaged == [1]

    # Sway's own send_copy() fires while the user is still physically
    # holding both keys down -- the exact synthetic sequence platform.
    # windows.send_copy() sends, via the *generic* Key.ctrl, reported
    # injected=True the way the real OS hook would.
    from pynput.keyboard import KeyCode

    with listener.suspended():
        listener._on_key_press(kb.Key.ctrl, injected=True)
        listener._on_key_press(KeyCode.from_char("c"), injected=True)
        listener._on_key_release(KeyCode.from_char("c"), injected=True)
        listener._on_key_release(kb.Key.ctrl, injected=True)
    assert disengaged == []  # must not have been corrupted into disengaging

    # The user's real release still works normally afterward.
    listener._on_key_release(kb.Key.cmd)
    assert disengaged == [1]


def test_suspended_prevents_a_spurious_unrelated_chord_firing():
    """The sharper version of the same bug: a synthetic 'c' press while
    suspended must not be allowed to fire an unrelated binding that happens
    to share whatever modifiers are genuinely held plus 'c'."""
    from pynput import keyboard as kb
    from pynput.keyboard import KeyCode

    listener = BindingListener()
    unrelated = parse_binding("ctrl+alt+c")
    fired: list[int] = []
    listener.rebind(
        hold_bindings={}, tap_bindings={"transform:y:0": (unrelated, lambda t: fired.append(1))}
    )

    listener._on_key_press(kb.Key.ctrl)
    listener._on_key_press(kb.Key.alt)

    with listener.suspended():
        listener._on_key_press(KeyCode.from_char("c"), injected=True)
    assert fired == []  # would have fired if dispatch ran while suspended

    # Confirm the binding is genuinely reachable outside the suspend window,
    # so the assertion above is meaningful rather than trivially true.
    listener._on_key_press(KeyCode.from_char("c"))
    assert fired == [1]


def test_suspended_is_reference_counted_for_overlapping_callers():
    """A dictation paste and a transform's selection-read can be in flight
    on two different worker threads at once; neither's suspension may end
    early just because the other finished first."""
    from pynput import keyboard as kb

    listener = BindingListener()
    combo = parse_binding("ctrl+cmd")
    engaged: list[int] = []
    listener.rebind(
        hold_bindings={"transform:x:0": (combo, lambda t: engaged.append(1), lambda: None)},
        tap_bindings={},
    )

    cm1 = listener.suspended()
    cm2 = listener.suspended()
    cm1.__enter__()
    cm2.__enter__()
    cm1.__exit__(None, None, None)  # first caller finishes...
    # ...but the second is still active, so dispatch must still be paused.
    listener._on_key_press(kb.Key.ctrl_r, injected=True)
    listener._on_key_press(kb.Key.cmd, injected=True)
    assert engaged == []
    cm2.__exit__(None, None, None)

    listener._on_key_release(kb.Key.ctrl_r, injected=True)
    listener._on_key_release(kb.Key.cmd, injected=True)
    listener._on_key_press(kb.Key.ctrl_r)
    listener._on_key_press(kb.Key.cmd)
    assert engaged == [1]  # normal dispatch resumes once fully unsuspended


def test_suspended_processes_a_real_release_landing_inside_the_window():
    """The wedge regression: a REAL key release (injected=False, the
    default) that happens to land inside a suspended() window -- because
    Sway's own synthetic Ctrl+C fired while the user's fingers were still on
    the keys -- must still be processed normally. An earlier, blunter
    suspended() ignored every event during the window regardless of origin,
    which left KeyHoldTracker._held stuck True forever: the bound action
    could never engage again for the rest of the process's life, and no log
    line ever explained why."""
    from pynput import keyboard as kb
    from pynput.keyboard import KeyCode

    listener = BindingListener()
    dictation = parse_binding("ctrl_r")
    engaged: list[int] = []
    disengaged: list[int] = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (dictation, lambda t: engaged.append(1), lambda: disengaged.append(1))
        },
        tap_bindings={},
    )

    listener._on_key_press(kb.Key.ctrl_r)  # a real press
    assert engaged == [1]

    with listener.suspended():
        listener._on_key_press(kb.Key.ctrl, injected=True)
        listener._on_key_press(KeyCode.from_char("c"), injected=True)
        listener._on_key_release(KeyCode.from_char("c"), injected=True)
        listener._on_key_release(kb.Key.ctrl, injected=True)
        listener._on_key_release(kb.Key.ctrl_r, injected=False)  # the user's REAL release

    assert disengaged == [1]  # processed, not swallowed
    assert listener._hold_slots["dictation:0"].tracker._held is False

    # Not wedged: pressing again engages normally.
    listener._on_key_press(kb.Key.ctrl_r)
    assert engaged == [1, 1]


# --------------------------------------------------------------------------
# BindingListener._maybe_resync_locked -- the safety net so a drifted or
# wedged state (however it arose) self-heals instead of persisting until the
# app restarts. Rate-limited via RESYNC_INTERVAL_S, and skipped entirely
# with no probe supplied (the default in every test above).
# --------------------------------------------------------------------------


def test_resync_clears_a_phantom_modifier_and_closes_a_stuck_hold():
    from pynput import keyboard as kb

    probe_result: dict[str, frozenset[str]] = {"held": frozenset()}
    listener = BindingListener(modifier_probe=lambda: probe_result["held"])
    dictation = parse_binding("ctrl_r")
    disengaged: list[int] = []
    listener.rebind(
        hold_bindings={"dictation:0": (dictation, lambda t: None, lambda: disengaged.append(1))},
        tap_bindings={},
    )

    # Simulate a wedge directly: mark the hold as engaged with no matching
    # release, as an earlier swallowed-release bug would leave it.
    slot = listener._hold_slots["dictation:0"]
    slot.tracker.press()
    generation = slot.gate.on_satisfied()
    slot.gate.try_engage(generation)
    assert slot.gate.engaged is True

    # The OS now reports nothing held. The next keypress must self-heal.
    listener._on_key_press(kb.Key.f13)  # an unrelated key, just to trigger the check
    assert disengaged == [1]
    assert slot.gate.satisfied is False
    assert slot.tracker._held is False


def test_resync_is_rate_limited():
    from pynput import keyboard as kb

    calls: list[int] = []

    def probe() -> frozenset[str]:
        calls.append(1)
        return frozenset()

    listener = BindingListener(modifier_probe=probe)
    listener.rebind(hold_bindings={}, tap_bindings={})
    listener._on_key_press(kb.Key.f13)
    listener._on_key_press(kb.Key.f14)
    assert len(calls) == 1  # the second call is within RESYNC_INTERVAL_S


def test_resync_does_not_flag_the_very_key_being_pressed_as_drift():
    """A probe queried at press time already reflects the OS's physical
    state for the key just pressed, moments before this call's own
    bookkeeping would add it -- that must not look like drift and log a
    spurious warning on practically every keypress."""
    from pynput import keyboard as kb

    listener = BindingListener(modifier_probe=lambda: frozenset({"ctrl_r"}))
    listener.rebind(hold_bindings={}, tap_bindings={})
    listener._on_key_press(kb.Key.ctrl_r)
    assert listener._down_mods == {"ctrl"}  # normal bookkeeping, no resync interference


def test_resync_no_op_with_no_probe_supplied():
    from pynput import keyboard as kb

    listener = BindingListener()  # no modifier_probe
    listener.rebind(hold_bindings={}, tap_bindings={})
    listener._on_key_press(kb.Key.f13)  # must not raise


# --------------------------------------------------------------------------
# binding_advisory -- a non-blocking note, never a rejection. Alt+letter
# also triggers menu mnemonics in most Windows apps, since BindingListener's
# hooks are deliberately non-suppressing.
# --------------------------------------------------------------------------


def test_binding_advisory_warns_about_alt_plus_a_letter():
    assert "menu" in binding_advisory(parse_binding("alt+f")).lower()


def test_binding_advisory_empty_for_ctrl_shift_combinations():
    assert binding_advisory(parse_binding("ctrl+shift+t")) == ""


def test_binding_advisory_empty_for_a_bare_hold_and_a_mouse_binding():
    assert binding_advisory(parse_binding("ctrl_r")) == ""
    assert binding_advisory(parse_binding("mouse:middle")) == ""


# --------------------------------------------------------------------------
# _key_name -- identifying a chord's terminal key. The regression these pin:
# pynput fills KeyCode.char from Windows' ToUnicodeEx *with the real modifier
# state*, so with Ctrl held the character is the control character (Ctrl+P is
# '\x10'), Ctrl+<digit> has no character at all, and Ctrl+Alt+<letter> has an
# unrelated one that is still alphanumeric. Matching on char therefore
# silently dropped every chord containing Ctrl -- including all seven shipped
# transforms on Ctrl+Alt+1..7 -- with no error logged anywhere. The char
# values below are the ones ToUnicodeEx actually returns on this platform.
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="VK codes are only trusted on Windows")
@pytest.mark.parametrize(
    "label,char,vk,expected",
    [
        ("unmodified", "p", 0x50, "p"),
        ("shift only", "P", 0x50, "p"),
        ("ctrl", "\x10", 0x50, "p"),
        ("ctrl+shift", "\x10", 0x50, "p"),
        ("ctrl+shift+t", "\x14", 0x54, "t"),
        ("ctrl+digit (no char at all)", None, 0x31, "1"),
        ("ctrl+alt (an unrelated alnum char)", "�", 0x50, "p"),
    ],
)
def test_key_name_identifies_the_keycap_letter_whatever_the_modifiers(label, char, vk, expected):
    from pynput.keyboard import KeyCode

    assert _key_name(KeyCode(char=char, vk=vk)) == expected


def test_key_name_still_resolves_a_named_chord_key():
    from pynput import keyboard as kb

    assert _key_name(kb.Key.space) == "space"
    assert _key_name(kb.Key.f5) == "f5"


def test_key_name_is_none_for_a_key_that_cannot_terminate_a_chord():
    from pynput import keyboard as kb

    assert _key_name(kb.Key.ctrl_r) is None


def test_a_ctrl_chord_fires_end_to_end_with_the_char_windows_really_delivers():
    """The user-reported failure, end to end: Ctrl+Shift+P over a selection
    did nothing at all -- the binding registered fine and the hook saw the
    keys, but _key_name could not name the terminal key, so no tap slot ever
    matched. Drives the dispatch path with the exact control character the
    OS delivers, and asserts dictation (bare ctrl_r) stays out of it."""
    from pynput import keyboard as kb
    from pynput.keyboard import KeyCode

    listener = BindingListener()
    fired: list[int] = []
    engaged: list[int] = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (parse_binding("ctrl_r"), lambda t: engaged.append(1), lambda: None)
        },
        tap_bindings={"transform:email:0": (parse_binding("ctrl+shift+p"), lambda t: fired.append(1))},
    )

    listener._on_key_press(kb.Key.ctrl_l)
    listener._on_key_press(kb.Key.shift)
    listener._on_key_press(KeyCode(char="\x10", vk=0x50))  # what Windows really sends
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert fired == [1]
    assert engaged == []


def test_a_shipped_builtin_transform_shortcut_fires():
    """Ctrl+Alt+1, the shape all seven shipped transforms use. Ctrl+<digit>
    produces no character at all on Windows, so this never once worked."""
    from pynput import keyboard as kb
    from pynput.keyboard import KeyCode

    listener = BindingListener()
    fired: list[int] = []
    listener.rebind(
        hold_bindings={},
        tap_bindings={"transform:polish:0": (parse_binding("ctrl+alt+1"), lambda t: fired.append(1))},
    )

    listener._on_key_press(kb.Key.ctrl_l)
    listener._on_key_press(kb.Key.alt_l)
    listener._on_key_press(KeyCode(char=None, vk=0x31))
    assert fired == [1]


# --------------------------------------------------------------------------
# Chords fire once per physical press. Windows auto-repeats a held key at
# ~31/s after a ~500ms delay, so holding a chord for two seconds used to
# fire it ~46 times. For a transform that meant ~46 concurrent selection
# reads and Groq calls, each resetting the last's captured selection --
# surfacing to the user as "nothing selected" after their own held key had
# already typed "pppppp" over the text they meant to transform.
# --------------------------------------------------------------------------


def _ctrl_p():
    """The KeyCode Windows really delivers for P with Ctrl held."""
    from pynput.keyboard import KeyCode

    return KeyCode(char="\x10", vk=0x50)


def test_a_held_chord_fires_once_however_long_it_is_held():
    from pynput import keyboard as kb

    listener = BindingListener()
    fired: list[int] = []
    listener.rebind(
        hold_bindings={},
        tap_bindings={"transform:x:0": (parse_binding("ctrl+shift+p"), lambda t: fired.append(1))},
    )

    listener._on_key_press(kb.Key.ctrl_l)
    listener._on_key_press(kb.Key.shift)
    for _ in range(46):  # ~2 seconds of Windows auto-repeat
        listener._on_key_press(_ctrl_p())
    assert fired == [1]


def test_a_chord_rearms_when_its_terminal_key_comes_up():
    from pynput import keyboard as kb

    listener = BindingListener()
    fired: list[int] = []
    listener.rebind(
        hold_bindings={},
        tap_bindings={"transform:x:0": (parse_binding("ctrl+shift+p"), lambda t: fired.append(1))},
    )

    listener._on_key_press(kb.Key.ctrl_l)
    listener._on_key_press(kb.Key.shift)
    listener._on_key_press(_ctrl_p())
    listener._on_key_release(_ctrl_p())  # modifiers still held, as when repeating a shortcut
    listener._on_key_press(_ctrl_p())
    assert fired == [1, 1]


def test_a_chord_rearms_when_a_modifier_comes_up_even_if_the_key_release_is_missed():
    """Either half of the gesture breaking rearms it, so a release the hook
    never saw cannot wedge the chord off for the rest of the session."""
    from pynput import keyboard as kb

    listener = BindingListener()
    fired: list[int] = []
    listener.rebind(
        hold_bindings={},
        tap_bindings={"transform:x:0": (parse_binding("ctrl+shift+p"), lambda t: fired.append(1))},
    )

    listener._on_key_press(kb.Key.ctrl_l)
    listener._on_key_press(kb.Key.shift)
    listener._on_key_press(_ctrl_p())
    assert fired == [1]

    listener._on_key_release(kb.Key.ctrl_l)  # note: no release for P at all
    listener._on_key_press(kb.Key.ctrl_l)
    listener._on_key_press(_ctrl_p())
    assert fired == [1, 1]


def test_a_repeating_chord_keeps_preempting_the_bare_hold_underneath_it():
    """The repeat guard must not let dictation slip through on repeat 2+:
    the gesture is still unambiguously the chord's, so the bare hold it
    shares a modifier with stays cancelled for as long as the keys are down."""
    from pynput import keyboard as kb

    listener = BindingListener()
    fired: list[int] = []
    engaged: list[int] = []
    listener.rebind(
        hold_bindings={
            "dictation:0": (parse_binding("ctrl_r"), lambda t: engaged.append(1), lambda: None)
        },
        tap_bindings={"transform:x:0": (parse_binding("ctrl+shift+p"), lambda t: fired.append(1))},
    )

    listener._on_key_press(kb.Key.ctrl_r)
    listener._on_key_press(kb.Key.shift)
    for _ in range(20):
        listener._on_key_press(_ctrl_p())
    time.sleep(PREFIX_GRACE_S + 0.08)
    assert fired == [1]
    assert engaged == []


def test_mouse_buttons_exclude_left_and_right():
    assert "left" not in MOUSE_BUTTONS
    assert "right" not in MOUSE_BUTTONS
    assert set(MOUSE_BUTTONS) == {"middle", "back", "forward"}
