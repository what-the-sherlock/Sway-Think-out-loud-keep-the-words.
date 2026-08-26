"""Tests for paste-based text injection, against a fake PlatformAdapter."""

from __future__ import annotations

import time

import pytest

from sway.errors import ClipboardError, PasteBlockedError
from sway.inject import TextInjector


class FakePlatform:
    name = "fake"

    def __init__(
        self,
        *,
        initial_clipboard: str = "",
        fail_get: bool = False,
        fail_set: bool = False,
        paste_blocked: bool = False,
    ):
        self.clipboard = initial_clipboard
        self._fail_get = fail_get
        self._fail_set = fail_set
        self._paste_blocked = paste_blocked
        self.set_calls: list[str] = []
        self.paste_calls = 0

    def synthetic_input_likely_blocked(self) -> bool:
        return self._paste_blocked

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
        self.set_calls.append(text)
        self.clipboard = text

    def send_paste(self) -> None:
        self.paste_calls += 1


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met within timeout")


def test_inject_sets_clipboard_and_sends_paste():
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=10_000)  # long enough not to fire yet
    injector.inject("hello world")
    assert platform.clipboard == "hello world"
    assert platform.paste_calls == 1


def test_clipboard_is_restored_after_the_delay():
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=20)
    injector.inject("hello world")
    assert platform.clipboard == "hello world"
    _wait_for(lambda: platform.clipboard == "original")


def test_empty_text_is_a_no_op():
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=10)
    injector.inject("")
    assert platform.paste_calls == 0
    assert platform.clipboard == "original"


def test_paste_proceeds_even_if_reading_the_old_clipboard_fails():
    platform = FakePlatform(fail_get=True)
    injector = TextInjector(platform, restore_delay_ms=10_000)
    injector.inject("hello")
    assert platform.clipboard == "hello"
    assert platform.paste_calls == 1


def test_restore_failure_does_not_raise():
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=10)
    injector.inject("hello")
    platform._fail_set = True  # make the restore attempt fail
    time.sleep(0.05)  # let the timer fire; a raised exception there would be silent but must not crash


def test_negative_restore_delay_is_rejected():
    with pytest.raises(ValueError):
        TextInjector(FakePlatform(), restore_delay_ms=-1)


def test_blocked_paste_raises_without_touching_the_clipboard():
    platform = FakePlatform(initial_clipboard="original", paste_blocked=True)
    injector = TextInjector(platform, restore_delay_ms=10_000)
    with pytest.raises(PasteBlockedError):
        injector.inject("hello world")
    assert platform.clipboard == "original"
    assert platform.paste_calls == 0
    assert platform.set_calls == []


def test_empty_text_is_a_no_op_even_when_paste_is_blocked():
    platform = FakePlatform(initial_clipboard="original", paste_blocked=True)
    injector = TextInjector(platform, restore_delay_ms=10)
    injector.inject("")  # must not raise: nothing to paste, so nothing to block
    assert platform.paste_calls == 0


# -- the restore must not clobber something the user copied in the meantime --


def test_restore_is_skipped_when_the_user_copied_something_else():
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=30)
    injector.inject("dictated text")
    # The user hits Ctrl+C on something of their own before the restore fires.
    platform.clipboard_set("something the user copied")
    time.sleep(0.15)
    assert platform.clipboard == "something the user copied"


def test_restore_still_happens_when_the_clipboard_is_untouched():
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=20)
    injector.inject("dictated text")
    _wait_for(lambda: platform.clipboard == "original")


def test_restore_proceeds_when_the_clipboard_cannot_be_read_back():
    # Failing open matters more than the ownership check here: the user's
    # clipboard should still come back if the read happens to fail.
    platform = FakePlatform(initial_clipboard="original")
    injector = TextInjector(platform, restore_delay_ms=20)
    injector.inject("dictated text")
    platform._fail_get = True
    _wait_for(lambda: platform.clipboard == "original")
