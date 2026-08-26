"""Tests for reading the current text selection, against a fake PlatformAdapter."""

from __future__ import annotations

import sys
import time

import pytest

from sway.errors import ClipboardError
from sway.selection import DEFAULT_MODIFIER_WAIT_S, SelectionReader, selection_budget_s


class FakePlatform:
    """Simulates Ctrl+C landing the selection on the clipboard synchronously,
    which is enough to exercise SelectionReader's own logic deterministically
    without a real timing-dependent OS round trip.
    """

    name = "fake"

    def __init__(
        self,
        *,
        initial_clipboard: str = "",
        selection: str | None = "",
        fail_get: bool = False,
        fail_set: bool = False,
        blocked: bool = False,
        fail_copies: int = 0,
    ) -> None:
        self.clipboard = initial_clipboard
        self._selection = selection
        self._fail_get = fail_get
        self._fail_set = fail_set
        self._blocked = blocked
        #: How many leading send_copy() calls should behave as if the
        #: keystroke never reached the target app -- for exercising read()'s
        #: one-retry behaviour deterministically.
        self._fail_copies = fail_copies
        self.copy_calls = 0

    def synthetic_input_likely_blocked(self) -> bool:
        return self._blocked

    def user_data_dir(self, app_name: str):
        raise NotImplementedError

    def user_config_dir(self, app_name: str):
        raise NotImplementedError

    def clipboard_get(self) -> str:
        if self._fail_get:
            raise ClipboardError("simulated read failure")
        return self.clipboard

    def clipboard_set(self, text: str) -> None:
        if self._fail_set:
            raise ClipboardError("simulated write failure")
        self.clipboard = text

    def send_paste(self) -> None:
        raise NotImplementedError

    def send_copy(self) -> None:
        self.copy_calls += 1
        if self.copy_calls <= self._fail_copies:
            return  # this attempt behaves as if the keystroke never landed
        if self._selection is not None:
            self.clipboard = self._selection


def test_read_returns_the_selected_text():
    platform = FakePlatform(initial_clipboard="original", selection="hello world")
    reader = SelectionReader(platform)
    assert reader.read() == "hello world"
    assert platform.copy_calls == 1


def test_read_restores_the_original_clipboard():
    platform = FakePlatform(initial_clipboard="original", selection="selected text")
    reader = SelectionReader(platform)
    reader.read()
    assert platform.clipboard == "original"


def test_no_selection_times_out_to_empty_string():
    # send_copy() never changes the clipboard away from the sentinel --
    # nothing was selected.
    platform = FakePlatform(initial_clipboard="original", selection=None)
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == ""
    assert platform.clipboard == "original"  # still restored


def test_empty_selection_string_is_treated_as_a_real_copy():
    # The app copied an empty string (rare, but distinguishable from "no
    # copy happened" only by the sentinel actually changing).
    platform = FakePlatform(initial_clipboard="original", selection="")
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == ""


def test_read_proceeds_even_if_saving_the_old_clipboard_fails():
    # Only the very first read (saving the original, to restore later) fails;
    # the poll reads afterward succeed normally and still find the selection.
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    real_get = platform.clipboard_get
    calls = {"n": 0}

    def flaky_get() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClipboardError("simulated read failure")
        return real_get()

    platform.clipboard_get = flaky_get  # type: ignore[method-assign]
    reader = SelectionReader(platform)
    assert reader.read() == "hello"


def test_read_returns_empty_if_every_clipboard_read_fails():
    platform = FakePlatform(selection="hello", fail_get=True)
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == ""


def test_read_returns_empty_if_the_clipboard_cannot_be_cleared():
    platform = FakePlatform(initial_clipboard="original", selection="hello", fail_set=True)
    reader = SelectionReader(platform)
    assert reader.read() == ""
    assert platform.copy_calls == 0  # never even sent the copy shortcut


def test_restore_failure_does_not_raise():
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    reader = SelectionReader(platform)

    real_set = platform.clipboard_set
    calls = {"n": 0}

    def flaky_set(text: str) -> None:
        calls["n"] += 1
        if calls["n"] > 1:  # let the sentinel-set succeed, fail the restore
            raise ClipboardError("simulated restore failure")
        real_set(text)

    platform.clipboard_set = flaky_set  # type: ignore[method-assign]
    assert reader.read() == "hello"  # still returns the selection


@pytest.mark.parametrize(
    "kwargs",
    [{"timeout_s": 0}, {"timeout_s": -1}, {"poll_interval_s": 0}, {"modifier_wait_s": -1}],
)
def test_invalid_construction_is_rejected(kwargs: dict):
    with pytest.raises(ValueError):
        SelectionReader(FakePlatform(), **kwargs)


# -- retry on an empty first attempt ----------------------------------------
#
# A modifier-combo binding (see sway.hotkey.ModifierHoldGate) engages the
# instant its keys are simultaneously down, not on release -- so the
# synthetic Ctrl+C this fires can land while the user is still physically
# holding the binding's keys, which a still-held Windows key in particular
# is documented to interfere with. By a second attempt the user has almost
# always let go, so one retry is worth its added latency on the failure
# path only.


def test_read_retries_once_after_an_empty_first_attempt():
    platform = FakePlatform(initial_clipboard="original", selection="hello world", fail_copies=1)
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == "hello world"
    assert platform.copy_calls == 2


def test_read_gives_up_after_the_retry_also_finds_nothing():
    platform = FakePlatform(initial_clipboard="original", selection=None)
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == ""
    assert platform.copy_calls == 2
    assert platform.clipboard == "original"  # still restored


def test_read_does_not_retry_after_a_successful_first_attempt():
    platform = FakePlatform(initial_clipboard="original", selection="hello world")
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == "hello world"
    assert platform.copy_calls == 1


# -- elevation parity with TextInjector --------------------------------------


def test_read_skips_entirely_when_synthetic_input_is_blocked():
    platform = FakePlatform(initial_clipboard="original", selection="hello world", blocked=True)
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == ""
    assert platform.copy_calls == 0  # never even attempted the copy
    assert platform.clipboard == "original"  # never touched


# -- suspending the caller's own binding listener ----------------------------


def test_send_copy_runs_inside_the_provided_suspend_context():
    entries: list[str] = []

    class RecordingContext:
        def __enter__(self) -> None:
            entries.append("enter")

        def __exit__(self, *exc: object) -> None:
            entries.append("exit")

    def suspend_input() -> RecordingContext:
        return RecordingContext()

    platform = FakePlatform(initial_clipboard="original", selection="hello")
    reader = SelectionReader(platform, suspend_input=suspend_input)
    assert reader.read() == "hello"
    assert entries == ["enter", "exit"]


def test_default_suspend_input_is_a_no_op():
    # No suspend_input passed -- must not raise, and must behave exactly as
    # it did before this parameter existed.
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    reader = SelectionReader(platform)
    assert reader.read() == "hello"


# -- waiting for the user's own held modifiers to clear ----------------------
#
# A chord fires on the key-down of its terminal key, not on release -- so the
# synthetic Ctrl+C this class sends can go out while the user is still
# physically holding the chord's other keys. The target app then receives
# Ctrl+Shift+C (or Ctrl+Alt+C, what every shipped transform's ctrl+alt+N
# shortcut produces) instead of Ctrl+C, which copies nothing anywhere. These
# drive modifier_probe directly rather than adding it to FakePlatform, since
# FakePlatform intentionally does not implement modifier_keys_down (see
# test_a_probe_that_raises_does_not_break_the_read below, which pins that
# this class must keep working against any adapter that lacks it).


class _Probe:
    """A scriptable modifier_keys_down, recording every call."""

    def __init__(self, *results: frozenset[str] | None) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self) -> frozenset[str] | None:
        self.calls += 1
        if self._results:
            return self._results.pop(0) if len(self._results) > 1 else self._results[0]
        return frozenset()


def test_no_probe_never_waits_and_never_probes():
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    reader = SelectionReader(platform, timeout_s=0.05, poll_interval_s=0.01)
    assert reader.read() == "hello"
    assert platform.copy_calls == 1


def test_a_held_shift_delays_the_copy_until_it_clears():
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    order: list[str] = []
    real_send_copy = platform.send_copy
    platform.send_copy = lambda: (order.append("copy"), real_send_copy())[1]  # type: ignore[method-assign]

    probe = _Probe(frozenset({"shift_l"}), frozenset({"shift_l"}), frozenset())

    def probing() -> frozenset[str] | None:
        result = probe()
        order.append(f"probe:{sorted(result)}")
        return result

    reader = SelectionReader(
        platform, timeout_s=0.05, poll_interval_s=0.01, modifier_probe=probing, modifier_wait_s=0.5
    )
    assert reader.read() == "hello"
    assert platform.copy_calls == 1
    # The last probe result immediately before the copy must be "cleared".
    copy_index = order.index("copy")
    assert order[copy_index - 1] == "probe:[]"


def test_a_held_ctrl_does_not_delay_the_copy():
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    probe = _Probe(frozenset({"ctrl_l", "ctrl_r"}))
    reader = SelectionReader(
        platform, timeout_s=0.05, poll_interval_s=0.01, modifier_probe=probe, modifier_wait_s=0.5
    )
    assert reader.read() == "hello"
    assert platform.copy_calls == 1
    assert probe.calls == 1  # one check, found nothing *blocking*, copied immediately


def test_a_modifier_that_never_clears_still_sends_the_copy_and_does_not_hang():
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    probe = _Probe(frozenset({"shift_l"}))
    reader = SelectionReader(
        platform, timeout_s=0.02, poll_interval_s=0.01, modifier_probe=probe, modifier_wait_s=0.03
    )
    assert reader.read() == "hello"
    assert platform.copy_calls == 1


def test_the_modifier_budget_is_shared_not_repaid_per_attempt():
    # Both attempts fail outright (selection=None); the modifier never
    # clears, so if the budget were re-paid per attempt, read() would spend
    # roughly 2x modifier_wait_s waiting. Instead attempt 2 must find the
    # shared deadline already expired and proceed with (at most) a single
    # extra check, not a fresh full wait. Asserted on elapsed wall-clock
    # time rather than an exact probe-call count, since the latter is
    # sensitive to scheduler jitter around each poll_interval_s sleep.
    platform = FakePlatform(initial_clipboard="original", selection=None)
    probe = _Probe(frozenset({"shift_l"}))
    modifier_wait_s = 0.05
    reader = SelectionReader(
        platform,
        timeout_s=0.01,
        poll_interval_s=0.01,
        modifier_probe=probe,
        modifier_wait_s=modifier_wait_s,
    )
    started = time.monotonic()
    assert reader.read() == ""
    elapsed = time.monotonic() - started
    assert platform.copy_calls == 2
    # One wait (~0.05s) plus two poll timeouts (~0.02s) plus slack, well
    # under what two independent full waits (~0.10s) plus the polls would
    # take -- the discriminating margin between "shared" and "re-paid".
    assert elapsed < 1.5 * modifier_wait_s + 2 * 0.01 + 0.1


def test_a_probe_returning_none_never_waits():
    platform = FakePlatform(initial_clipboard="original", selection="hello")
    reader = SelectionReader(
        platform, timeout_s=0.05, poll_interval_s=0.01, modifier_probe=lambda: None, modifier_wait_s=0.5
    )
    assert reader.read() == "hello"
    assert platform.copy_calls == 1


def test_a_probe_that_raises_does_not_break_the_read():
    def raising() -> frozenset[str]:
        raise RuntimeError("simulated platform failure")

    platform = FakePlatform(initial_clipboard="original", selection="hello")
    reader = SelectionReader(
        platform, timeout_s=0.05, poll_interval_s=0.01, modifier_probe=raising, modifier_wait_s=0.5
    )
    assert reader.read() == "hello"
    assert platform.copy_calls == 1


def test_selection_budget_covers_the_readers_own_worst_case():
    from sway.selection import _CLIPBOARD_SLACK_S, _SETTLE_AFTER_RELEASE_S

    for timeout_s in (0.3, 1.0):
        assert selection_budget_s(timeout_s) >= (
            DEFAULT_MODIFIER_WAIT_S + _SETTLE_AFTER_RELEASE_S + 2 * timeout_s
        )
        # And it is not infinitely generous either -- the slack is bounded.
        assert selection_budget_s(timeout_s) <= (
            DEFAULT_MODIFIER_WAIT_S + _SETTLE_AFTER_RELEASE_S + 2 * timeout_s + _CLIPBOARD_SLACK_S
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only probe")
def test_the_blocking_modifier_names_match_the_platform_probes_vocabulary():
    """A typo in _BLOCKING_MODIFIERS (e.g. "shift_left") would make the
    entire wait a silent no-op forever, since it would never match anything
    the real platform probe actually returns."""
    from sway.platform.windows import _MODIFIER_VK_CODES
    from sway.selection import _BLOCKING_MODIFIERS

    assert _BLOCKING_MODIFIERS <= frozenset(_MODIFIER_VK_CODES) | {"cmd"}
