"""Tests for the windowed build's entry point (flow.gui).

This is the code path a non-technical recipient of a shared build actually
runs, and every one of its failure modes ends in a dialog rather than a
console message -- so the mapping from "what went wrong" to "which dialog"
is worth pinning down, even though the surrounding Tk/PyInstaller wiring
(like flow.overlay's) is not unit tested.

Everything here runs against a fake platform adapter; no dialog is ever
really shown and no real config is read.
"""

from __future__ import annotations

import pytest

from sway import gui
from sway.errors import ConfigError, SwayError


class FakePlatform:
    name = "fake"

    def __init__(self, *, lock_available: bool = True, held: set[str] | None = None) -> None:
        self._lock_available = lock_available
        #: Names an "other process" already owns, so the legacy double-paste
        #: guard can be exercised without a second real process.
        self._held = held or set()
        self.messages: list[tuple[str, str, bool]] = []
        self.lock_names: list[str] = []

    def show_message(self, title: str, text: str, *, error: bool = False) -> None:
        self.messages.append((title, text, error))

    def acquire_single_instance_lock(self, name: str) -> bool:
        self.lock_names.append(name)
        if name in self._held:
            return False
        return self._lock_available


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    fake = FakePlatform()
    monkeypatch.setattr(gui, "get_platform", lambda: fake)
    return fake


def _patch_cli(monkeypatch: pytest.MonkeyPatch, result):
    """Replace flow.cli.main, recording the argv it was handed.

    ``result`` is either an exit code to return or an exception to raise.
    """
    calls: list[list[str]] = []

    def fake_main(argv=None):
        calls.append(list(argv) if argv is not None else [])
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr("sway.cli.main", fake_main)
    return calls


# -- argv defaulting ---------------------------------------------------------


def test_bare_launch_defaults_to_run(platform: FakePlatform, monkeypatch: pytest.MonkeyPatch):
    # The whole point: a double-clicked icon must start dictating, not print
    # help to a console that does not exist.
    calls = _patch_cli(monkeypatch, 0)
    assert gui.main_windowed([]) == 0
    assert calls == [["run"]]
    assert platform.messages == []


def test_explicit_arguments_are_passed_through_untouched(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    calls = _patch_cli(monkeypatch, 0)
    assert gui.main_windowed(["history", "list"]) == 0
    assert calls == [["history", "list"]]


# -- single instance ---------------------------------------------------------


def _blocked_second_launch(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """A second launch that cannot take the lock, isolated from the machine.

    ``data_dir`` is redirected at a tmp_path: without this the test reads the
    *real* ``%LOCALAPPDATA%\\Flow\\instance.json``, so it passes or fails
    depending on whether the developer happens to have Flow running -- which
    is exactly how this test first broke.
    """
    fake = FakePlatform(lock_available=False)
    monkeypatch.setattr(gui, "get_platform", lambda: fake)

    from sway import instance as instance_module

    class _Config:
        data_dir = tmp_path

    monkeypatch.setattr("sway.config.load_config", lambda *a, **k: _Config())
    return fake, instance_module


def test_second_instance_with_no_advertisement_explains_itself(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """No instance.json: an older first instance, or one still starting."""
    fake, _ = _blocked_second_launch(monkeypatch, tmp_path)
    calls = _patch_cli(monkeypatch, 0)

    # Exit code 0, not an error: a second launch is a user mistake, not a
    # failure, and the first instance is still running fine.
    assert gui.main_windowed([]) == 0
    assert calls == []  # the CLI must never run for a blocked second instance
    title, text, is_error = fake.messages[0]
    assert "already running" in text.lower()
    assert is_error is False


def test_second_instance_that_focuses_the_first_says_nothing_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """Silence is the success case.

    The first instance has come forward, so a dialog on top of it saying
    "already running" would be pure noise.
    """
    fake, instance_module = _blocked_second_launch(monkeypatch, tmp_path)
    calls = _patch_cli(monkeypatch, 0)

    info = instance_module.InstanceInfo(pid=4321, port=5000, token="t", url="http://x")
    monkeypatch.setattr(instance_module, "read", lambda *a, **k: info)
    monkeypatch.setattr(
        instance_module, "request_focus", lambda *a, **k: instance_module.FocusResult.FOCUSED
    )

    assert gui.main_windowed([]) == 0
    assert calls == []
    assert fake.messages == []


def test_second_instance_reports_an_unresponsive_first_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    """Alive and advertised but not answering: the one case the user must act
    on, so it is an error dialog naming the process -- and never a silent
    kill, since this also covers a first instance merely busy mid-dictation.
    """
    fake, instance_module = _blocked_second_launch(monkeypatch, tmp_path)
    calls = _patch_cli(monkeypatch, 0)

    info = instance_module.InstanceInfo(pid=4321, port=5000, token="t", url="http://x")
    monkeypatch.setattr(instance_module, "read", lambda *a, **k: info)
    monkeypatch.setattr(
        instance_module,
        "request_focus",
        lambda *a, **k: instance_module.FocusResult.UNRESPONSIVE,
    )

    assert gui.main_windowed([]) == 1
    assert calls == []
    title, text, is_error = fake.messages[0]
    assert "4321" in text
    assert "not responding" in text.lower()
    assert is_error is True


def test_the_instance_lock_is_taken_before_anything_else(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    _patch_cli(monkeypatch, 0)
    gui.main_windowed([])
    # Both names, in order: the current one, then the pre-rename one whose
    # holder would be an old build running its own hotkey hook.
    assert platform.lock_names == [
        gui._INSTANCE_LOCK_NAME,
        gui._LEGACY_INSTANCE_LOCK_NAME,
    ]


def test_a_pre_rename_build_still_running_blocks_startup(
    monkeypatch: pytest.MonkeyPatch,
):
    """The double-paste guard.

    A pre-rename Flow.exe holds the *old* mutex, not the new one. Without
    this check both builds would start, both would hook the hotkey, and every
    dictation would paste twice.
    """
    fake = FakePlatform(held={gui._LEGACY_INSTANCE_LOCK_NAME})
    monkeypatch.setattr(gui, "get_platform", lambda: fake)
    calls = _patch_cli(monkeypatch, 0)

    assert gui.main_windowed([]) == 1
    assert calls == []  # never reaches the engine
    _title, text, is_error = fake.messages[0]
    assert is_error is True
    assert "older copy" in text
    assert "twice" in text


# -- the missing-API-key path (the likeliest first run for a shared build) ----


def test_a_missing_api_key_no_longer_blocks_launch(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    """The old behaviour was a dead end: a dialog telling the user to make a
    .env file in Notepad. Onboarding now runs inside the app window, so the
    app must start without a key rather than refusing to."""
    calls = _patch_cli(monkeypatch, 0)

    assert gui.main_windowed([]) == 0
    assert calls == [["run"]]
    # Nothing to tell the user here -- the window asks them itself.
    assert platform.messages == []


# -- failure reporting -------------------------------------------------------


def test_expected_flow_errors_are_shown_verbatim(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    _patch_cli(monkeypatch, SwayError("could not open the microphone"))
    assert gui.main_windowed([]) == 1
    _title, text, is_error = platform.messages[0]
    assert "could not open the microphone" in text
    assert is_error is True


def test_unexpected_exceptions_are_reported_rather_than_crashing(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    _patch_cli(monkeypatch, RuntimeError("a genuine bug"))
    # Must not propagate: an unhandled exception in a windowed build is an
    # invisible crash, which is the exact failure this entry point exists to
    # prevent.
    assert gui.main_windowed([]) == 1
    _title, text, is_error = platform.messages[0]
    assert "a genuine bug" in text
    assert is_error is True


def test_a_nonzero_exit_code_is_surfaced_with_the_log_location(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    # cli.main handled the error itself and wrote the detail to a stderr that
    # does not exist in a windowed build, so the dialog must at least say
    # where to look.
    _patch_cli(monkeypatch, 3)
    assert gui.main_windowed([]) == 3
    _title, text, is_error = platform.messages[0]
    assert "3" in text
    assert is_error is True


def test_a_successful_run_shows_no_dialog_at_all(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    _patch_cli(monkeypatch, 0)
    assert gui.main_windowed([]) == 0
    assert platform.messages == []
