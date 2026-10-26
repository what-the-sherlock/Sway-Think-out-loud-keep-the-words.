"""The routes the new UI depends on, exercised over a real socket.

The existing test_dashboard_server.py covers the original read routes and the
auth model. This file covers what D5/D6 added: the composite reads, the write
routes, and -- most importantly -- that the write routes are still behind the
same token and origin checks as everything else.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from sway.dashboard.server import DashboardServer, EngineControl
from sway.history import KIND_DICTATION, HistoryStore
from sway.settings import SettingsStore


@pytest.fixture
def server(tmp_path):
    history_db = tmp_path / "history.db"
    with HistoryStore.open(history_db) as store:
        store.add(
            kind=KIND_DICTATION,
            text="Hello there world",
            raw_text="hello there world",
            focused_process="slack.exe",
        )

    instance = DashboardServer(
        history_db=history_db,
        latency_db=tmp_path / "latency.db",
        dictionary_db=tmp_path / "dictionary.db",
        typing_wpm=45.0,
        settings=SettingsStore.open(tmp_path),
        control=EngineControl(
            is_listening=lambda: True,
            set_listening=lambda _value: None,
            describe=lambda: {"hotkey": "ctrl_r"},
            test_key=lambda: {"ok": True, "model": "whisper", "latency_ms": 12.0},
            input_level=lambda: 2,
            hotkey_down=lambda: False,
        ),
    )
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


def _url(server, path):
    return f"http://127.0.0.1:{server.port}{path}"


def get(server, path, *, token=None):
    joiner = "&" if "?" in path else "?"
    url = _url(server, f"{path}{joiner}token={token or server.token}")
    with urllib.request.urlopen(url) as response:
        return response.status, json.loads(response.read())


def post(server, path, payload, *, token=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    request = urllib.request.Request(
        _url(server, f"{path}?token={token or server.token}"),
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


# -- reads -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/home", "/api/settings", "/api/dictionary", "/api/devices", "/api/status"],
)
def test_new_read_routes_answer(server, path):
    status, _ = get(server, path)
    assert status == 200


def test_home_is_one_request(server):
    """Home opens the window; four parallel requests on a cold webview is
    exactly where a native app stops feeling native."""
    _, body = get(server, "/api/home")
    assert set(body) >= {"status", "today", "streaks", "recent", "health", "has_data"}
    assert body["has_data"] is True
    assert len(body["recent"]) == 1


def test_settings_payload_shape(server):
    _, body = get(server, "/api/settings")
    assert body["editable"] is True
    assert body["engine"] == {"hotkey": "ctrl_r"}
    assert set(body["key"]) == {"configured", "masked", "source", "keyring_available"}
    assert "settings" in body and "triggers" in body


def test_settings_serves_the_language_catalogue(server):
    """app.js used to hold its own hardcoded language list, which had already
    drifted from the bar's. Serving it is what stops that recurring."""
    from sway import languages

    _, body = get(server, "/api/settings")
    assert [row["code"] for row in body["languages"]] == [
        lang.code for lang in languages.CATALOGUE if lang.in_shortlist
    ]
    assert "mr" in [row["code"] for row in body["languages"]]
    assert all({"code", "label", "endonym"} == set(row) for row in body["languages"])


def test_settings_never_reveals_the_key(server):
    """The masked value is the only form of the key that may cross this line."""
    _, body = get(server, "/api/settings")
    assert "reveal" not in json.dumps(body)
    masked = body["key"]["masked"]
    assert masked == "" or "•" in masked


def test_status_carries_the_live_probes(server):
    _, body = get(server, "/api/status")
    assert body["level"] == 2
    assert body["hotkey_down"] is False


def test_status_survives_a_failing_probe(tmp_path):
    """A probe that throws must degrade, not 500 the screen the user is on."""

    def explode():
        raise RuntimeError("nope")

    instance = DashboardServer(
        history_db=tmp_path / "h.db",
        latency_db=tmp_path / "l.db",
        typing_wpm=45.0,
        control=EngineControl(input_level=explode, hotkey_down=explode),
    )
    instance.start()
    try:
        status, body = get(instance, "/api/status")
        assert status == 200
        assert body["level"] == 0
        assert body["hotkey_down"] is False
    finally:
        instance.stop()


# -- writes ----------------------------------------------------------------


def test_settings_write_round_trips(server):
    status, body = post(server, "/api/settings", {"launch_at_login": True})
    assert status == 200
    assert body["settings"]["launch_at_login"] is True


def test_invalid_settings_write_is_a_400_with_a_readable_message(server):
    status, body = post(server, "/api/settings", {"overlay_position": "nowhere"})
    assert status == 400
    assert "unknown overlay position" in body["error"]


def test_trigger_lifecycle_over_http(server):
    _, created = post(server, "/api/triggers/save", {"phrase": "my email", "payload": "a@b.co"})
    assert len(created["triggers"]) == 1
    trigger_id = created["triggers"][0]["id"]

    _, deleted = post(server, "/api/triggers/delete", {"id": trigger_id})
    assert deleted["triggers"] == []


def test_dictionary_write_round_trips(server):
    status, body = post(server, "/api/dictionary/add", {"terms": ["Groq"]})
    assert status == 200
    assert body["terms"] == ["Groq"]

    _, listed = get(server, "/api/dictionary")
    assert listed["terms"] == ["Groq"]


def test_delete_all_needs_confirmation_over_http(server):
    assert post(server, "/api/history/delete-all", {})[0] == 400
    status, body = post(server, "/api/history/delete-all", {"confirm": True})
    assert status == 200
    assert body["deleted"] == 1


def test_key_test_is_delegated_to_the_engine(server):
    status, body = post(server, "/api/key/test", {})
    assert status == 200
    assert body["ok"] is True


# -- the security model still holds ---------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/home", "/api/settings", "/api/dictionary", "/api/devices"],
)
def test_reads_require_the_token(server, path):
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(_url(server, path))
    assert caught.value.code == 403


@pytest.mark.parametrize(
    "path",
    ["/api/settings", "/api/triggers/save", "/api/dictionary/add", "/api/history/delete-all"],
)
def test_writes_require_the_token(server, path):
    assert post(server, path, {}, token="wrong-token")[0] == 403


def test_writes_reject_a_cross_origin_request(server):
    """Closes the hole from the other side: a page that somehow learned the
    token still cannot drive these routes."""
    status, _ = post(
        server, "/api/settings", {"launch_at_login": True}, origin="https://evil.example"
    )
    assert status == 403


def test_same_origin_write_is_allowed(server):
    status, _ = post(
        server,
        "/api/settings",
        {"launch_at_login": True},
        origin=f"http://127.0.0.1:{server.port}",
    )
    assert status == 200


# -- degradation without an engine ----------------------------------------


def test_writes_are_refused_when_settings_are_not_attached(tmp_path):
    """`flow dashboard` standalone reports history but changes nothing."""
    instance = DashboardServer(
        history_db=tmp_path / "h.db", latency_db=tmp_path / "l.db", typing_wpm=45.0
    )
    instance.start()
    try:
        status, body = post(instance, "/api/settings", {"launch_at_login": True})
        assert status == 409
        assert "not editable" in body["error"]

        _, payload = get(instance, "/api/settings")
        assert payload["editable"] is False
    finally:
        instance.stop()


def test_dictionary_reports_unavailable_without_a_database(tmp_path):
    instance = DashboardServer(
        history_db=tmp_path / "h.db", latency_db=tmp_path / "l.db", typing_wpm=45.0
    )
    instance.start()
    try:
        _, body = get(instance, "/api/dictionary")
        assert body["available"] is False
        assert body["terms"] == []
    finally:
        instance.stop()


def test_voice_profile_route(server):
    _, body = get(server, "/api/voice")
    assert body["dictations"] == 1
    assert len(body["hours"]) == 24
    assert "understanding" in body


def test_settings_serve_the_style_groups(server):
    _, body = get(server, "/api/settings")
    assert [g["category"] for g in body["style_groups"]] == ["chat", "mail", "terminal", "code", "other"]


def test_performance_carries_the_latency_target(server):
    _, body = get(server, "/api/performance")
    assert body["target_ms"] == 1000.0


def test_overview_heatmap_spans_26_weeks_whatever_the_range(server):
    _, body = get(server, "/api/overview?range=7d")
    assert len(body["heatmap"]) == 182
    assert len(body["daily"]) == 7
    assert sum(day["sessions"] for day in body["heatmap"]) == 1
