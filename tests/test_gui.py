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

from flow import gui
from flow.errors import ConfigError, FlowError


class FakePlatform:
    name = "fake"

    def __init__(self, *, lock_available: bool = True) -> None:
        self._lock_available = lock_available
        self.messages: list[tuple[str, str, bool]] = []
        self.lock_names: list[str] = []

    def show_message(self, title: str, text: str, *, error: bool = False) -> None:
        self.messages.append((title, text, error))

    def acquire_single_instance_lock(self, name: str) -> bool:
        self.lock_names.append(name)
        return self._lock_available


@pytest.fixture
def platform(monkeypatch: pytest.MonkeyPatch) -> FakePlatform:
    fake = FakePlatform()
    monkeypatch.setattr(gui, "get_platform", lambda: fake)
    # Default to "a key is configured" so tests that aren't about the key
    # don't all have to say so; the key-specific tests override it.
    monkeypatch.setattr(gui, "_has_api_key", lambda: True)
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

    monkeypatch.setattr("flow.cli.main", fake_main)
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

    from flow import instance as instance_module

    class _Config:
        data_dir = tmp_path

    monkeypatch.setattr("flow.config.load_config", lambda *a, **k: _Config())
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
    assert platform.lock_names == [gui._INSTANCE_LOCK_NAME]


# -- the missing-API-key path (the likeliest first run for a shared build) ----


def test_missing_api_key_shows_actionable_setup_instructions(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(gui, "_has_api_key", lambda: False)
    calls = _patch_cli(monkeypatch, 0)

    assert gui.main_windowed([]) == 2
    assert calls == []  # never reaches the CLI; nothing to transcribe with
    _title, text, is_error = platform.messages[0]
    assert is_error is True
    assert "console.groq.com/keys" in text
    assert ".env" in text


def test_a_missing_key_does_not_block_non_run_commands(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    # `flow config` and friends are useful precisely when the key is missing.
    monkeypatch.setattr(gui, "_has_api_key", lambda: False)
    calls = _patch_cli(monkeypatch, 0)
    assert gui.main_windowed(["config"]) == 0
    assert calls == [["config"]]


def test_has_api_key_fails_open_when_the_config_cannot_be_loaded(
    monkeypatch: pytest.MonkeyPatch,
):
    # A broken config is a different problem with its own message; it must
    # not be reported as "you forgot your API key".
    def boom():
        raise ConfigError("unknown option [audio].nope")

    monkeypatch.setattr("flow.config.load_config", boom)
    assert gui._has_api_key() is True


# -- failure reporting -------------------------------------------------------


def test_expected_flow_errors_are_shown_verbatim(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
):
    _patch_cli(monkeypatch, FlowError("could not open the microphone"))
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
