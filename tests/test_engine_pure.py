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
    _Transcript,
    _describe_failure,
    _drop_duplicate_bindings,
    _failure_label,
    frames_before,
    preroll_start_frame,
    should_process,
)
from sway.hotkey import parse_binding
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


# -- prefix-grace back-date ---------------------------------------------------
# A dictation engage deferred by sway.hotkey's prefix grace (up to
# PREFIX_GRACE_S, see BindingListener._begin_hold_locked) must not cost the
# user any audio -- the mic has been writing throughout regardless. Composed
# with preroll_start_frame at the engage site: frames_before reclaims the
# grace delay, then preroll_start_frame reaches back further still.


def test_frames_before_reclaims_the_grace_delay():
    # 180ms (PREFIX_GRACE_S) at 16kHz is 2880 frames.
    assert frames_before(mark=100_000, seconds=0.18, sample_rate=16_000) == 97_120


def test_frames_before_is_a_no_op_for_zero_delay():
    assert frames_before(mark=100_000, seconds=0.0, sample_rate=16_000) == 100_000


def test_frames_before_clamped_at_the_start_of_the_stream():
    assert frames_before(mark=1000, seconds=0.18, sample_rate=16_000) == 0


def test_frames_before_treats_a_negative_delay_as_zero():
    # An undeferred press computes now - press_time == 0 exactly, but a
    # caller must not accidentally seek *forward* on any float imprecision.
    assert frames_before(mark=100_000, seconds=-0.001, sample_rate=16_000) == 100_000


# -- duplicate-binding backstop ----------------------------------------------
# Two actions accidentally bound to the exact same physical gesture: the save
# paths already refuse or flag this, so this is only a defensive backstop --
# but it is also what feeds the registration ledger's "duplicate" status
# (see Engine._rebind_all_bindings), so it must say *which* binding won.


def test_drop_duplicate_bindings_keeps_the_first_and_reports_no_drops_when_disjoint():
    mapping = {"dictation:0": (parse_binding("ctrl_r"), None, None)}
    kept, dropped = _drop_duplicate_bindings(mapping, kind="binding")
    assert kept == mapping
    assert dropped == {}


def test_drop_duplicate_bindings_drops_the_later_registration_and_names_the_winner():
    mapping = {
        "dictation:0": (parse_binding("ctrl_r"), "first", None),
        "hands_free:0": (parse_binding("ctrl_r"), "second", None),
    }
    kept, dropped = _drop_duplicate_bindings(mapping, kind="binding")
    assert list(kept) == ["dictation:0"]
    assert dropped == {"hands_free:0": "dictation:0"}


def test_drop_duplicate_bindings_insertion_order_is_registration_priority():
    """dictation registers before hands_free in Engine._rebind_all_bindings,
    so when both claim the same gesture dictation must be the one kept --
    this pins that the function itself has no independent ordering rule of
    its own; it trusts caller insertion order."""
    mapping = {
        "hands_free:0": (parse_binding("shift_r"), "first-in-dict-order", None),
        "dictation:0": (parse_binding("shift_r"), "second-in-dict-order", None),
    }
    kept, dropped = _drop_duplicate_bindings(mapping, kind="binding")
    assert list(kept) == ["hands_free:0"]  # whichever came first in the mapping wins
    assert dropped == {"dictation:0": "hands_free:0"}


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
    "command_guardrail",
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
    # A guardrail-blocked transform/command (Engine._run_command) or retry
    # (Engine._retry_command) means the model returned nothing usable -- the
    # only honest recovery is trying again, which pressing the shortcut or
    # holding the command key already does. _transform_retry cannot serve
    # this either: it acts on self._pending_transform, which is None on this
    # exact path since nothing was ever presented to a panel.
    "command_guardrail": "the only recovery is retrying, which the shortcut/key already offers",
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
    engine._choose_target_language = lambda value: chosen.append(("target", value))

    engine._popover_choice_handler("lang:en")()
    engine._popover_choice_handler("lang:")()
    assert chosen == ["en", None]


def test_a_target_row_round_trips_to_a_target_language():
    """The output language rides the same popover, on its own prefix, so the
    bar still knows nothing about what a language is."""
    engine = Engine.__new__(Engine)
    chosen = []
    engine._choose_language = lambda value: chosen.append(("language", value))
    engine._choose_device = lambda value: chosen.append(("device", value))
    engine._choose_target_language = chosen.append

    engine._popover_choice_handler("target:mr")()
    engine._popover_choice_handler("target:")()
    assert chosen == ["mr", None]


def test_target_rows_are_not_mistaken_for_dictation_language_rows():
    """Both prefixes end in a colon; a prefix check in the wrong order would
    silently route every output choice into the input setting."""
    engine = Engine.__new__(Engine)
    chosen = []
    engine._choose_language = lambda value: chosen.append(("language", value))
    engine._choose_device = lambda value: chosen.append(("device", value))
    engine._choose_target_language = lambda value: chosen.append(("target", value))

    engine._popover_choice_handler("target:hi")()
    engine._popover_choice_handler("lang:hi")()
    assert chosen == [("target", "hi"), ("language", "hi")]


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
        return type(
            "S",
            (),
            {"onboarding_complete": self._onboarded, "dictation_bindings": None},
        )()


def _tap_engine(bar, *, onboarded, successes=0):
    engine = _engine_with(bar)
    engine._settings = FakeSettings(onboarded)
    engine._config = type(
        "C", (), {"hotkey": type("H", (), {"key": "ctrl_r", "key_chord": None})()}
    )()
    engine._last_tap_at = 0.0
    engine._tap_streak = 0
    engine._success_count = successes
    return engine


def test_a_first_run_tap_is_answered():
    bar = FakeBar()
    _tap_engine(bar, onboarded=False)._coach_tap(100.0)
    assert bar.coached == ["Don't tap. Hold [[Right Ctrl]] while you speak"]


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
        hotkey=SimpleNamespace(min_hold_ms=200),
        streaming=SimpleNamespace(enabled=False),
        audio=SimpleNamespace(sample_rate=16000),
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


# -- language resolution ----------------------------------------------------
#
# The decisions that pick the STT model, the prompt, and whether to translate
# at all. Pure enough to build an Engine shell for: none of them touch audio,
# the network, or the bar.


def _language_engine(*, language=None, target=None, history=(), streaming=True):
    """An Engine shell wired with just the state the language logic reads."""
    import threading
    from collections import deque

    from sway.groq_client import WHISPER_ACCURATE_MODEL, WHISPER_MODEL

    engine = Engine.__new__(Engine)
    engine._config = SimpleNamespace(
        groq=SimpleNamespace(
            language=language,
            stt_model=WHISPER_MODEL,
            stt_accurate_model=WHISPER_ACCURATE_MODEL,
            timeout_s=15.0,
        ),
        cleanup=SimpleNamespace(
            enabled=True,
            model="openai/gpt-oss-20b",
            skip_min_words=8,
            skip_when_unconfident=True,
        ),
        streaming=SimpleNamespace(enabled=streaming),
    )
    engine._settings = SimpleNamespace(get=lambda: SimpleNamespace(target_language=target))
    engine._language_history = deque(history, maxlen=3)
    engine._language_lock = threading.Lock()
    return engine


def _transcript(text="text", language=None, *, confident=True, speech_ms=5000.0):
    return _Transcript(
        text=text, language=language, confident=confident, speech_ms=speech_ms
    )


def test_an_explicit_language_beats_whispers_guess():
    """Whisper labels Marathi as Hindi. Someone who has said "I speak
    Marathi" must not be overruled by that."""
    engine = _language_engine(language="mr")
    assert engine._language_for_transcript(_transcript(language="hindi")) == "mr"


def test_a_short_clip_ignores_whispers_language_label():
    """Measured misdetections cluster under ~2s: "Hello" came back as Hindi,
    a surname as Korean. A one-word utterance must not repick the language."""
    engine = _language_engine(history=("en", "en"))
    short = _transcript(language="hindi", speech_ms=900.0)
    assert engine._language_for_transcript(short) == "en"


def test_a_long_clip_trusts_whispers_language_label():
    engine = _language_engine(history=("en",))
    assert engine._language_for_transcript(_transcript(language="hindi")) == "hi"


def test_a_short_clip_with_no_history_falls_back_to_the_label():
    """Better than nothing, and still the old behaviour on a fresh session."""
    engine = _language_engine()
    assert engine._language_for_transcript(_transcript(language="hindi", speech_ms=500.0)) == "hi"


def test_the_session_language_is_the_recent_majority():
    engine = _language_engine(history=("hi", "en", "hi"))
    assert engine._session_language() == "hi"


def test_a_genuine_switch_wins_a_tie():
    """Two each: the newer one should take effect now, not in three more
    dictations."""
    engine = _language_engine(history=("en", "mr"))
    assert engine._session_language() == "mr"


def test_remembering_normalises_and_ignores_junk():
    engine = _language_engine()
    engine._remember_language("Hindi")
    engine._remember_language("klingon")
    engine._remember_language(None)
    assert list(engine._language_history) == ["hi"]


def test_indic_languages_get_the_accurate_stt_model():
    engine = _language_engine(language="mr")
    assert engine._stt_model_for("mr").endswith("large-v3")
    assert not engine._stt_model_for("mr").endswith("turbo")


def test_english_keeps_the_fast_stt_model():
    engine = _language_engine()
    assert engine._stt_model_for("en").endswith("turbo")
    assert engine._stt_model_for(None).endswith("turbo")


def test_streaming_is_skipped_for_languages_turbo_decodes_badly():
    """Chunking a language the decoder already bends toward another one
    compounds the error across fragments that cannot be told apart."""
    assert not _language_engine(language="mr")._streaming_wanted()
    assert not _language_engine(history=("hi", "hi"))._streaming_wanted()
    assert _language_engine(language="en")._streaming_wanted()
    assert not _language_engine(language="en", streaming=False)._streaming_wanted()


# -- the cleanup skip decision ----------------------------------------------


def _skip(engine, transcript, *, style=None, language=None, target=None, transforms=()):
    from sway.corrections import CorrectionScan
    from sway.profiles import CleanupStyle

    return engine._cleanup_skip_reason(
        transcript,
        style or CleanupStyle.CASUAL,
        CorrectionScan(text=transcript.text),
        transforms,
        language=language,
        target_language=target,
    )


def test_a_target_language_forces_a_short_utterance_through_cleanup():
    """Otherwise "short_and_clean" pastes it in the language it was spoken
    in, silently ignoring the setting."""
    engine = _language_engine(target="mr")
    assert _skip(engine, _transcript("Meet me at noon"), language="en", target="mr") == ""


def test_a_short_utterance_still_skips_when_nothing_is_being_translated():
    engine = _language_engine()
    assert _skip(engine, _transcript("Meet me at noon"), language="en") == "short_and_clean"


def test_an_unconfident_transcript_is_never_translated():
    """Rewriting shaky words in the same language is recoverable; rendering
    them into a language the user cannot read back is not."""
    engine = _language_engine(target="hi")
    shaky = _transcript("mumble", confident=False)
    assert _skip(engine, shaky, language="en", target="hi") == "language_uncertain"


def test_the_uncertainty_check_outranks_the_correction_override():
    """A self-correction normally forces cleanup through even when Whisper is
    unsure. That override must not also authorise a translation."""
    from sway.corrections import scan as scan_corrections
    from sway.profiles import CleanupStyle

    engine = _language_engine(target="hi")
    correction = scan_corrections("the meeting is at 7, no, at 8")
    assert correction.detected, "fixture must actually contain a correction"
    reason = engine._cleanup_skip_reason(
        _transcript(correction.text, confident=False),
        CleanupStyle.CASUAL,
        correction,
        (),
        language="en",
        target_language="hi",
    )
    assert reason == "language_uncertain"


def test_a_verbatim_profile_is_never_translated():
    """A terminal or code editor gets the words as transcribed, whatever the
    output language says. Silently translating a shell command is the worst
    outcome in the matrix."""
    from sway.profiles import CleanupStyle

    engine = _language_engine(target="mr")
    reason = _skip(
        engine,
        _transcript("git commit --amend"),
        style=CleanupStyle.VERBATIM,
        language="en",
        target="mr",
    )
    assert reason == "verbatim_profile"


def test_a_same_script_session_prior_beats_whispers_label():
    """Whisper reports every Marathi utterance as Hindi -- it cannot tell the
    two apart at all. Once the session knows the difference, that knowledge
    has to survive contact with the next transcription, or the model's own
    verdict is recorded and then ignored forever."""
    engine = _language_engine(history=("mr", "mr"))
    assert engine._language_for_transcript(_transcript(language="hindi")) == "mr"


def test_a_cross_script_label_still_beats_the_session():
    """Whisper is reliable across scripts, so a genuine switch must take
    effect immediately rather than being held back by history."""
    engine = _language_engine(history=("mr", "mr"))
    assert engine._language_for_transcript(_transcript(language="english")) == "en"


def test_the_session_prior_does_not_invent_a_language():
    engine = _language_engine()
    assert engine._language_for_transcript(_transcript(language=None)) is None


# -- the language popover ---------------------------------------------------


def _popover(language=None, target=None, opener="_open_language_popover"):
    captured = {}
    engine = Engine.__new__(Engine)
    engine._settings = SimpleNamespace(
        get=lambda: SimpleNamespace(language=language, target_language=target)
    )
    engine._bar = SimpleNamespace(
        open_popover=lambda anchor, items, width=0: captured.update(
            anchor=anchor, items=items, width=width
        )
    )
    getattr(engine, opener)()
    return captured


def test_the_language_popover_offers_marathi():
    actions = [item.action for item in _popover()["items"]]
    assert "lang:mr" in actions


def test_the_output_language_is_a_drill_down_not_a_second_list():
    """Both lists inline came to 27 rows -- taller than the screen the
    popover has to open on. The row shows the current value so the drill-down
    still answers the question without being opened."""
    items = _popover(target="hi")["items"]
    assert sum(1 for item in items if item.action.startswith("target:")) == 0
    row = next(item for item in items if item.action == Engine._ACTION_TARGET_MENU)
    assert row.submenu and row.detail == "हिन्दी"


def test_the_drill_down_row_reads_same_as_spoken_by_default():
    items = _popover()["items"]
    row = next(item for item in items if item.action == Engine._ACTION_TARGET_MENU)
    assert row.detail == "Same as spoken"


def test_the_target_popover_leads_with_same_as_spoken_and_can_go_back():
    items = _popover(opener="_open_target_popover")["items"]
    assert items[0].label == "Back"
    same_as_spoken = next(item for item in items if item.action == "target:")
    assert same_as_spoken.checked, "the default must show as the current value"


@pytest.mark.parametrize(
    "opener", ["_open_language_popover", "_open_target_popover"]
)
def test_neither_popover_is_taller_than_a_laptop_screen(opener):
    """A popover taller than the display opens off-screen and cannot be
    used. 864px is a common laptop height; leave room for the bar itself."""
    from sway.bar import render_menu

    rendered = render_menu(_popover(language="mr", target="hi", opener=opener)["items"])
    image = rendered[0] if isinstance(rendered, tuple) else rendered
    assert image.size[1] < 800


# -- the accurate-STT retry -------------------------------------------------
#
# The Auto-mode recovery path. Turbo reported one speaker's Marathi as
# english, then indonesian, then arabic, then urdu -- each one confidently,
# and each one pasted. These pin when the full model gets a second look.


def _retry_engine(*, language=None, history=(), submitted=None):
    from concurrent.futures import Future

    engine = _language_engine(language=language, history=history)

    def submit(coro):
        coro.close()  # never actually awaited in these tests
        future: Future = Future()
        future.set_result(submitted)
        return future

    engine._network = SimpleNamespace(submit=submit)
    engine._groq = SimpleNamespace(transcribe=lambda *a, **k: _Coro())
    return engine


class _Coro:
    """A stand-in for the coroutine GroqClient.transcribe returns."""

    def close(self):
        return None


def _result(text="x", language=None, *, confident=True):
    from sway.groq_client import TranscriptionResult

    return TranscriptionResult(
        text=text,
        language=language,
        avg_logprob=0.0 if confident else -2.0,
    )


def _retry(engine, result, speech_ms=2000.0):
    return engine._retry_on_accurate_stt(
        result, b"flac", prompt_bias="", used_model="whisper-large-v3-turbo",
        speech_ms=speech_ms,
    )


def test_an_unsupported_detected_language_triggers_a_retry():
    """Whisper naming a language Sway has no profile for is evidence the
    detection is junk, not a fact about the speaker. This is the exact
    failure that pasted Urdu for a Marathi speaker."""
    better = _result("मला घरी जायचं आहे", "marathi")
    engine = _retry_engine(submitted=better)
    assert _retry(engine, _result("ملاع ادعاء", "urdu")) is better


def test_a_known_hard_language_triggers_a_retry():
    better = _result("मला घरी जायचं आहे", "marathi")
    engine = _retry_engine(submitted=better)
    assert _retry(engine, _result("मला घरी", "hindi")) is better


def test_a_low_confidence_decode_triggers_a_retry():
    better = _result("clearer text", "english")
    engine = _retry_engine(submitted=better)
    assert _retry(engine, _result("mush", "english", confident=False)) is better


def test_a_confident_supported_language_is_left_alone():
    """The common case must not pay for a second transcription."""
    engine = _retry_engine(submitted=_result("other", "english"))
    assert _retry(engine, _result("hello there", "english")) is None


def test_an_explicit_language_setting_suppresses_the_retry():
    """The user already said what they speak; the first call used the right
    model and hint, so a second one buys nothing."""
    engine = _retry_engine(language="mr", submitted=_result("other", "marathi"))
    assert _retry(engine, _result("मला", "hindi")) is None


def test_a_long_clip_is_not_retried():
    """A second full transcription of a long clip costs more than the error
    it fixes, and doubles the spend against a daily audio quota."""
    engine = _retry_engine(submitted=_result("other", "marathi"))
    assert _retry(engine, _result("x", "urdu"), speech_ms=9000.0) is None


def test_the_retry_hints_the_session_language_when_detection_is_junk():
    """Nothing in the response can be trusted, so the best available hint is
    what the last few dictations actually were."""
    captured = {}
    engine = _retry_engine(history=("mr", "mr"), submitted=_result("ok", "marathi"))
    engine._groq = SimpleNamespace(
        transcribe=lambda *a, **k: (captured.update(k), _Coro())[1]
    )
    _retry(engine, _result("ملاع", "urdu"))
    assert captured["language"] == "mr"
    assert captured["model"].endswith("large-v3")


def test_an_empty_retry_result_keeps_the_original():
    engine = _retry_engine(submitted=_result("   ", "marathi"))
    assert _retry(engine, _result("something", "urdu")) is None
