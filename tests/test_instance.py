"""D1: the running instance's advertisement, and the rules for trusting it.

The security-relevant property here is that a *stale* instance file is
treated as absent. The file names a port; ports get recycled; sending this
app's auth token to whatever process now holds a recycled port would leak a
live credential. These tests pin that behaviour, plus the ordinary
publish/read/withdraw round trip.

No process is ever spawned: liveness is injected, which is why
:func:`flow.instance.read` takes an ``is_running`` parameter at all.
"""

from __future__ import annotations

import json

from sway import instance


def _alive(_pid: int) -> bool:
    return True


def _dead(_pid: int) -> bool:
    return False


def test_publish_then_read_round_trips(tmp_path) -> None:
    written = instance.publish(tmp_path, port=54321, token="tok", url="http://x/?token=tok")
    assert written == instance.instance_path(tmp_path)

    info = instance.read(tmp_path, is_running=_alive)
    assert info is not None
    assert (info.port, info.token, info.url) == (54321, "tok", "http://x/?token=tok")
    assert info.pid > 0


def test_read_returns_none_when_there_is_no_file(tmp_path) -> None:
    assert instance.read(tmp_path, is_running=_alive) is None


def test_a_dead_owner_is_treated_as_no_instance(tmp_path) -> None:
    """The core safety rule.

    A recorded port whose process has exited may belong to something else
    entirely by now, so the advertisement must read as absent rather than as
    'here is where to send the token'.
    """
    instance.publish(tmp_path, port=54321, token="secret", url="http://x")
    assert instance.read(tmp_path, is_running=_dead) is None


def test_liveness_failure_is_treated_as_no_instance(tmp_path) -> None:
    """Fails closed: if liveness cannot be determined, do not trust the file."""

    def _explodes(_pid: int) -> bool:
        raise OSError("cannot query")

    instance.publish(tmp_path, port=54321, token="secret", url="http://x")
    assert instance.read(tmp_path, is_running=_explodes) is None


def test_malformed_files_are_ignored_rather_than_raising(tmp_path) -> None:
    target = instance.instance_path(tmp_path)
    for payload in (
        "not json at all",
        "[]",  # valid JSON, wrong shape
        json.dumps({"pid": 1}),  # missing keys
        json.dumps({"pid": "x", "port": 1, "token": "t", "url": "u"}),  # bad type
        json.dumps({"pid": 1, "port": 0, "token": "t", "url": "u"}),  # nonsense port
        json.dumps({"pid": 1, "port": 5, "token": "", "url": "u"}),  # no token
    ):
        target.write_text(payload, encoding="utf-8")
        assert instance.read(tmp_path, is_running=_alive) is None, payload


def test_withdraw_removes_the_file_and_is_idempotent(tmp_path) -> None:
    instance.publish(tmp_path, port=1, token="t", url="u")
    assert instance.instance_path(tmp_path).exists()
    instance.withdraw(tmp_path)
    assert not instance.instance_path(tmp_path).exists()
    instance.withdraw(tmp_path)  # must not raise on a second call


def test_publish_truncates_rather_than_appending(tmp_path) -> None:
    """A rewritten advertisement must not leave the previous one behind it.

    Appending would produce a file whose JSON parse fails, which read()
    treats as 'no instance' -- so a restart on a new port would silently lose
    the focus behaviour rather than updating it.
    """
    instance.publish(tmp_path, port=11111, token="old-and-longer-token", url="http://old")
    instance.publish(tmp_path, port=22222, token="new", url="http://new")

    info = instance.read(tmp_path, is_running=_alive)
    assert info is not None
    assert info.port == 22222
    assert info.token == "new"
    assert "old" not in instance.instance_path(tmp_path).read_text(encoding="utf-8")


def test_focus_request_to_a_dead_port_reports_unresponsive() -> None:
    """A closed port must not hang the second launch or raise.

    Port 1 on loopback is not listening, so this exercises the connection
    -refused path with the real urllib stack rather than a mock.
    """
    info = instance.InstanceInfo(pid=1, port=1, token="t", url="http://127.0.0.1:1")
    result = instance.request_focus(info, timeout_s=0.25)
    assert result is instance.FocusResult.UNRESPONSIVE
