"""Tests for reading the current text selection, against a fake PlatformAdapter."""

from __future__ import annotations

import pytest

from flow.errors import ClipboardError
from flow.selection import SelectionReader


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
    ) -> None:
        self.clipboard = initial_clipboard
        self._selection = selection
        self._fail_get = fail_get
        self._fail_set = fail_set
        self.copy_calls = 0

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


@pytest.mark.parametrize("kwargs", [{"timeout_s": 0}, {"timeout_s": -1}, {"poll_interval_s": 0}])
def test_invalid_construction_is_rejected(kwargs: dict):
    with pytest.raises(ValueError):
        SelectionReader(FakePlatform(), **kwargs)
