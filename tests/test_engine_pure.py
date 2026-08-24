"""Tests for the pure decision functions in flow.engine.

Per CLAUDE.md the engine's threading/wiring is not unit tested; the functions
here are the parts that are pure arithmetic or pure classification, and they
decide user-visible behaviour (whether a press counts, and how much audio a
dictation actually starts from).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from sway.activation import Activation, ActivationLedger
from sway.bar import RECOVERY, BarState, recovery_for
from sway.engine import (
    Engine,
    _describe_failure,
    _failure_label,
    preroll_start_frame,
    should_process,
)
from sway.errors import AudioError, ClipboardError, PasteBlockedError, SwayError
from sway.groq_client import GroqError, GroqRateLimitError


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


# -- the failure-kind contract ---------------------------------------------
#
# _describe_failure names a failure; sway.bar.RECOVERY decides what the user
# can do about it. They live in different modules and are joined only by a
# string, so nothing but a test stops one from drifting away from the other.

#: Every kind the engine can attach to a bar state, gathered by hand from
#: _describe_failure and from the call sites that pass `kind=` directly.
EMITTED_KINDS = {
    "audio",
    "auth",
    "clipboard",
    "failed",
    "network",
    "no_key",
    "no_speech",
    "paste_blocked",
    "rate_limited",
    "settings",
    "timeout",
    "unexpected",
}

#: Kinds deliberately left without a recovery button, and why. A button that
#: cannot help is worse than no button.
NO_RECOVERY = {
    "rate_limited": "resolves on its own; the capsule counts the wait down",
    "failed": "cause unknown, so there is nothing specific to offer",
    "unexpected": "same, and the detail is in the log file",
    "settings": "the write already failed; retrying the same write is not help",
}


@pytest.mark.parametrize(
    "exc",
    [
        PasteBlockedError("x"),
        ClipboardError("x"),
        AudioError("x"),
        GroqError("Groq rejected this API key"),
        GroqError("connection reset"),
        SwayError("something else"),
    ],
)
def test_every_described_failure_has_a_known_kind(exc):
    assert _describe_failure(exc)["kind"] in EMITTED_KINDS


def test_every_recovery_entry_answers_a_failure_the_engine_can_emit():
    """A recovery for a kind nothing produces is dead code that reads as a
    feature."""
    assert set(RECOVERY) <= EMITTED_KINDS


def test_every_emitted_kind_is_either_recoverable_or_deliberately_not():
    """The point of the test: adding a new failure kind without deciding what
    the user does about it should fail here rather than ship as an error with
    no way out."""
    assert EMITTED_KINDS == set(RECOVERY) | set(NO_RECOVERY)


def test_the_unrecoverable_kinds_really_offer_nothing():
    for kind in NO_RECOVERY:
        assert recovery_for(BarState.ERROR, kind) is None, kind


# -- the bar-state guard ----------------------------------------------------
#
# Engine is far too heavy to build here, and CLAUDE.md says not to test the
# wiring. These drive one method each on a bare instance with only the
# attributes that method touches, which is the part that decides behaviour.


class FakeBar:
    def __init__(self):
        self.states = []
        self.coached = []

    def set_state(self, state, detail="", *, kind=""):
        self.states.append((state, detail, kind))

    def coach(self, text, *, ttl_s=None):
        self.coached.append(text)


def _engine_with(bar, *, hotkey_down=False, listening=True):
    engine = Engine.__new__(Engine)
    engine._bar = bar
    engine._tray = None
    engine._hotkey_down = hotkey_down
    engine._hands_free_active = False
    engine._listening = listening
    return engine


def test_a_finishing_worker_cannot_paint_over_a_live_hold():
    """Press the hotkey again while the previous transcription is still
    running and that worker's "Done - 12 words" would land on top of the
    recording the user is watching themselves make."""
    bar = FakeBar()
    engine = _engine_with(bar, hotkey_down=True)
    engine._set_ui_state(BarState.DONE, detail="12 words")
    engine._set_ui_state(BarState.ERROR, detail="failed", kind="unexpected")
    assert bar.states == []


def test_a_live_hold_can_still_report_itself():
    bar = FakeBar()
    engine = _engine_with(bar, hotkey_down=True)
    engine._set_ui_state(BarState.RECORDING)
    assert bar.states == [(BarState.RECORDING, "", "")]


def test_pausing_wins_even_mid_hold():
    """It is the user telling the app to stop; it cannot be the one message
    the guard swallows."""
    bar = FakeBar()
    engine = _engine_with(bar, hotkey_down=True)
    engine._set_ui_state(BarState.PAUSED)
    assert bar.states == [(BarState.PAUSED, "", "")]


def test_the_kind_reaches_the_bar():
    """It was computed on every error path and then dropped, which is why no
    failure ever offered a way out."""
    bar = FakeBar()
    engine = _engine_with(bar)
    engine._set_ui_state(BarState.ERROR, detail="x", kind="paste_blocked")
    assert bar.states == [(BarState.ERROR, "x", "paste_blocked")]


# -- popover rows round-trip ------------------------------------------------


def test_a_language_row_round_trips_to_a_language():
    engine = Engine.__new__(Engine)
    chosen = []
    engine._choose_language = chosen.append
    engine._choose_device = lambda value: chosen.append(("device", value))

    engine._popover_choice_handler("lang:en")()
    engine._popover_choice_handler("lang:")()
    assert chosen == ["en", None]


def test_a_device_row_round_trips_to_a_device():
    engine = Engine.__new__(Engine)
    chosen = []
    engine._choose_language = lambda value: chosen.append(("language", value))
    engine._choose_device = chosen.append

    engine._popover_choice_handler("dev:Headset (Realtek)")()
    engine._popover_choice_handler("dev:")()
    assert chosen == ["Headset (Realtek)", None]


def test_an_ordinary_action_is_not_mistaken_for_a_popover_row():
    engine = Engine.__new__(Engine)
    for action in ("settings", "history", "paste", "copy", "pause", ""):
        assert engine._popover_choice_handler(action) is None


# -- the tap ladder ---------------------------------------------------------
#
# A too-short hold used to return to idle with no feedback at all, which is
# indistinguishable from the app ignoring the user. This is the ladder that
# replaced it.


class FakeSettings:
    def __init__(self, onboarded):
        self._onboarded = onboarded

    def get(self):
        return type("S", (), {"onboarding_complete": self._onboarded})()


def _tap_engine(bar, *, onboarded, successes=0):
    engine = _engine_with(bar)
    engine._settings = FakeSettings(onboarded)
    engine._config = type(
        "C", (), {"hotkey": type("H", (), {"key": "ctrl_r"})()}
    )()
    engine._last_tap_at = 0.0
    engine._tap_streak = 0
    engine._success_count = successes
    return engine


def test_a_first_run_tap_is_answered():
    bar = FakeBar()
    _tap_engine(bar, onboarded=False)._coach_tap(100.0)
    assert bar.coached == ["Don't tap — hold [[Right Ctrl]] while you speak"]


def test_the_hotkey_is_shown_as_a_key_not_spelled_out():
    bar = FakeBar()
    _tap_engine(bar, onboarded=False)._coach_tap(100.0)
    assert "[[Right Ctrl]]" in bar.coached[0]


def test_a_second_tap_escalates_the_wording():
    bar = FakeBar()
    engine = _tap_engine(bar, onboarded=False)
    engine._coach_tap(100.0)
    engine._coach_tap(102.0)
    assert bar.coached[1] == "Hold [[Right Ctrl]] down, speak, then let go"


def test_taps_far_apart_do_not_escalate():
    bar = FakeBar()
    engine = _tap_engine(bar, onboarded=False)
    engine._coach_tap(100.0)
    engine._coach_tap(200.0)
    assert bar.coached[0] == bar.coached[1]


def test_a_stray_tap_from_an_experienced_user_stays_quiet():
    """Almost always the key being brushed. Explaining the product every time
    that happens is noise."""
    bar = FakeBar()
    _tap_engine(bar, onboarded=True, successes=9)._coach_tap(100.0)
    assert bar.coached == []
    assert bar.states == [(BarState.IDLE, "", "")]


def test_an_experienced_user_who_taps_twice_is_still_helped():
    """A repeat is evidence of someone trying and failing, whoever they
    are."""
    bar = FakeBar()
    engine = _tap_engine(bar, onboarded=True, successes=9)
    engine._coach_tap(100.0)
    engine._coach_tap(101.0)
    assert bar.coached == ["Hold [[Right Ctrl]] down, speak, then let go"]


def test_coaching_retires_once_the_user_can_clearly_do_it():
    bar = FakeBar()
    assert _tap_engine(bar, onboarded=False, successes=3)._should_coach() is False
    assert _tap_engine(bar, onboarded=False, successes=2)._should_coach() is True


def test_unreadable_settings_do_not_break_a_tap():
    bar = FakeBar()
    engine = _tap_engine(bar, onboarded=False)
    engine._settings = type("Broken", (), {"get": staticmethod(lambda: 1 / 0)})()
    engine._coach_tap(100.0)
    assert bar.states == [(BarState.IDLE, "", "")]


# -- the microphone picker --------------------------------------------------
#
# PortAudio enumerates each device once per host API, so one microphone
# arrives three or four times under the same name. The setting stores a
# *name*, which makes those rows not merely redundant but indistinguishable:
# clicking any of them writes the same value.

RAW_DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "default": False},
    {"name": "Microphone Array", "default": True},
    {"name": "Microphone Array", "default": False},  # same device, MME
    {"name": "Microphone Array", "default": False},  # same device, WASAPI
    {"name": "Microphone (Realtek)", "default": False},
    {"name": "", "default": False},  # PortAudio can report a blank name
    {"name": "  Microphone Array  ", "default": False},  # and stray padding
]


def _mic_engine(devices, current=None):
    engine = Engine.__new__(Engine)
    engine._bar = FakeBar()
    engine._settings = type(
        "S", (), {"get": staticmethod(lambda: type("V", (), {"audio_device": current})())}
    )()
    opened = []
    engine._bar.open_popover = lambda anchor, items, width=0: opened.append(items)
    import sway.engine as engine_module

    original = engine_module.input_devices
    engine_module.input_devices = lambda: devices
    try:
        engine._open_microphone_popover()
    finally:
        engine_module.input_devices = original
    return opened[0]


def _device_rows(items):
    return [i for i in items if i.action.startswith("dev:") and i.action != "dev:"]


def test_the_same_device_is_listed_once():
    rows = _device_rows(_mic_engine(RAW_DEVICES))
    names = [row.action for row in rows]
    assert len(names) == len(set(names))
    assert sum("Microphone Array" == row.action[4:] for row in rows) == 1


def test_a_blank_device_name_is_dropped():
    assert all(row.action != "dev:" for row in _device_rows(_mic_engine(RAW_DEVICES)))


def test_the_current_device_is_listed_first_and_ticked():
    """A picker is for switching, and the row being switched *from* belongs
    where it can be seen."""
    rows = _device_rows(_mic_engine(RAW_DEVICES, current="Microphone (Realtek)"))
    assert rows[0].action == "dev:Microphone (Realtek)"
    assert rows[0].checked


def test_the_system_default_leads_when_nothing_is_chosen():
    items = _mic_engine(RAW_DEVICES)
    assert items[0].action == "dev:"
    assert items[0].checked
    assert _device_rows(items)[0].detail == "default"


def test_a_long_device_list_is_capped_and_says_so():
    """Truncating silently would hide a microphone the user owns."""
    many = [{"name": f"Mic {i}", "default": False} for i in range(20)]
    items = _mic_engine(many)
    assert len(_device_rows(items)) == 6
    assert any(i.action == "settings" for i in items)


def test_a_short_device_list_does_not_claim_there_are_more():
    items = _mic_engine([{"name": "Only mic", "default": True}])
    assert not any(i.action == "settings" for i in items)


def test_the_restart_caveat_is_always_present():
    """audio_device is bound into a stream held open for the process's life,
    so the picker has to say when it takes effect."""
    items = _mic_engine(RAW_DEVICES)
    assert items[-1].footer
    assert "restart" in items[-1].label.lower()


def test_no_microphones_is_an_explicit_state_not_an_empty_panel():
    items = _mic_engine([])
    assert any(not i.enabled for i in items)
    assert any(i.action == "settings" for i in items)


def test_a_device_that_cannot_be_enumerated_does_not_raise():
    engine = Engine.__new__(Engine)
    engine._bar = FakeBar()
    opened = []
    engine._bar.open_popover = lambda anchor, items, width=0: opened.append(items)
    engine._settings = type(
        "S", (), {"get": staticmethod(lambda: type("V", (), {"audio_device": None})())}
    )()
    import sway.engine as engine_module

    original = engine_module.input_devices

    def boom():
        raise OSError("no audio backend")

    engine_module.input_devices = boom
    try:
        engine._open_microphone_popover()
    finally:
        engine_module.input_devices = original
    assert opened, "a broken audio backend must still open a panel that says so"
    assert any(not i.enabled for i in opened[0])


# -- duplicate dispatch ------------------------------------------------------
# CLAUDE.md keeps the engine's threading out of the unit tests, and these do
# not test threading: they test the *decision* to dispatch, which is pure and
# is the one that went wrong. The engine is built with __new__ and given only
# the attributes _on_release actually reads, so nothing here opens a mic,
# hooks a key, or talks to the network.
#
# The regression: one hold produced two pastes. `_press_time` and
# `_start_frame` were never cleared, so a second release passed every check and
# re-dispatched the same audio range.


class _Recorder:
    """Stands in for everything _on_release touches, and counts dispatches."""

    def __init__(self) -> None:
        self.dispatched: list[Activation] = []

    def set_level_metering(self, _on: bool) -> None: ...
    def set_level(self, _level: int) -> None: ...


def _release_only_engine(monkeypatch) -> tuple[Engine, _Recorder]:
    engine = Engine.__new__(Engine)
    recorder = _Recorder()

    engine._activations = ActivationLedger()
    engine._hotkey_down = False
    engine._dictation_started_via_chord = False
    engine._capture = recorder
    engine._bar = recorder
    engine._input_level = 0
    engine._press_time = time.monotonic() - 5.0  # a comfortably long hold
    engine._start_frame = 1000
    engine._focused_process = "notepad.exe"
    engine._stream_session = None
    engine._config = SimpleNamespace(
        hotkey=SimpleNamespace(min_hold_ms=200), streaming=SimpleNamespace(enabled=False)
    )
    engine._set_ui_state = lambda *a, **k: None
    engine._coach_tap = lambda _at: None
    engine._start_level_pump = lambda: None
    engine._platform = SimpleNamespace(focused_process_name=lambda: "notepad.exe")
    engine._ring = SimpleNamespace(mark=lambda: 2000)
    engine._preroll_frames = 0

    # Capture the dispatch instead of starting a worker thread.
    class _Thread:
        def __init__(self, *, target=None, args=(), **_kw) -> None:
            self._args = args

        def start(self) -> None:
            recorder.dispatched.append(self._args[-1])

    monkeypatch.setattr("sway.engine.threading.Thread", _Thread)
    return engine, recorder


def test_one_press_and_two_releases_dispatch_once(monkeypatch):
    """The reported duplicate, at the engine boundary."""
    engine, recorder = _release_only_engine(monkeypatch)
    engine._on_press()
    engine._press_time = time.monotonic() - 5.0  # a hold long enough to count
    engine._on_release()
    engine._on_release()
    assert len(recorder.dispatched) == 1


def test_a_release_with_no_press_never_dispatches(monkeypatch):
    """A rival hook, or an event arriving after a rebind, has no press behind
    it. Dispatching it would transcribe whatever `_start_frame` still held."""
    engine, recorder = _release_only_engine(monkeypatch)
    engine._on_release()
    assert recorder.dispatched == []


def test_each_press_and_release_pair_dispatches_once(monkeypatch):
    engine, recorder = _release_only_engine(monkeypatch)
    for _ in range(3):
        engine._on_press()
        engine._press_time = time.monotonic() - 5.0
        engine._on_release()
    assert len(recorder.dispatched) == 3
    assert len({a.id for a in recorder.dispatched}) == 3


def test_a_dispatched_activation_can_only_paste_once(monkeypatch):
    """The injector is the last line of defence: even if two pipelines somehow
    run for one activation, only one of them reaches the cursor."""
    engine, recorder = _release_only_engine(monkeypatch)
    engine._on_press()
    engine._press_time = time.monotonic() - 5.0
    engine._on_release()
    activation = recorder.dispatched[0]

    pasted: list[str] = []
    engine._injector = SimpleNamespace(inject=pasted.append)
    run = SimpleNamespace(annotate=lambda **_k: None)

    assert engine._inject_with_fallback("hello", run, activation) is True
    assert engine._inject_with_fallback("hello", run, activation) is False
    assert pasted == ["hello"]
