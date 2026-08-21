"""Tests for the dashboard HTTP server.

Weighted toward the security boundary. This server runs inside a process that
can paste text into any focused window and holds the user's entire dictation
history, so the token check, the origin check, and the static-path containment
check are the three things that must not regress.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from flow.dashboard.server import DashboardServer, EngineControl


@pytest.fixture
def server(tmp_path):
    srv = DashboardServer(
        history_db=tmp_path / "history.db",
        latency_db=tmp_path / "latency.db",
        typing_wpm=45.0,
    )
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def controlled_server(tmp_path):
    state = {"listening": True}
    srv = DashboardServer(
        history_db=tmp_path / "history.db",
        latency_db=tmp_path / "latency.db",
        typing_wpm=45.0,
        control=EngineControl(
            is_listening=lambda: state["listening"],
            set_listening=lambda value: state.__setitem__("listening", value),
            describe=lambda: {"hotkey": "ctrl_r"},
        ),
    )
    srv.start()
    yield srv, state
    srv.stop()


def _get(server: DashboardServer, path: str, *, token: str | None = "valid"):
    url = f"http://127.0.0.1:{server.port}{path}"
    if token is not None:
        actual = server.token if token == "valid" else token
        url += ("&" if "?" in path else "?") + f"token={actual}"
    with urllib.request.urlopen(url) as response:
        return response.status, json.loads(response.read())


def _post(server: DashboardServer, path: str, payload: dict, *, origin: str | None = None):
    url = f"http://{'127.0.0.1'}:{server.port}{path}?token={server.token}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if origin:
        request.add_header("Origin", origin)
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


class TestAuth:
    def test_api_without_a_token_is_forbidden(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(server, "/api/overview", token=None)
        assert excinfo.value.code == 403

    def test_api_with_a_wrong_token_is_forbidden(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(server, "/api/overview", token="not-the-token")
        assert excinfo.value.code == 403

    def test_api_with_the_token_succeeds(self, server) -> None:
        status, _ = _get(server, "/api/overview")
        assert status == 200

    def test_token_also_accepted_as_a_header(self, server) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/status",
            headers={"X-Flow-Token": server.token},
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 200

    def test_each_server_mints_a_distinct_token(self, tmp_path) -> None:
        first = DashboardServer(
            history_db=tmp_path / "h.db", latency_db=tmp_path / "l.db", typing_wpm=45.0
        )
        second = DashboardServer(
            history_db=tmp_path / "h.db", latency_db=tmp_path / "l.db", typing_wpm=45.0
        )
        assert first.token != second.token


class TestStatic:
    def test_serves_the_page_without_a_token(self, server) -> None:
        """The HTML shell carries no data; the token in its URL is what the
        script then uses for every API call."""
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as response:
            assert response.status == 200
            assert "text/html" in response.headers["Content-Type"]

    @pytest.mark.parametrize("path", ["/app.js", "/styles.css"])
    def test_serves_assets(self, server, path) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{path}") as response:
            assert response.status == 200
            assert len(response.read()) > 0

    def test_path_traversal_is_refused(self, server) -> None:
        """Without containment, this would hand out the user's Groq API key."""
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/../../.env")
        assert excinfo.value.code in (403, 404)

    def test_unknown_file_is_not_found(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/nope.txt")
        assert excinfo.value.code == 404

    def test_responses_are_not_cached(self, server) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/") as response:
            assert response.headers["Cache-Control"] == "no-store"


class TestRoutes:
    def test_unknown_endpoint(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(server, "/api/nonsense")
        assert excinfo.value.code == 404

    def test_overview_on_an_empty_install(self, server) -> None:
        _, body = _get(server, "/api/overview?range=7d")
        assert body["has_data"] is False
        assert body["today"]["words"] == 0
        # Absent averages must be null, never 0.
        assert body["today"]["avg_wpm"] is None
        assert len(body["daily"]) == 7

    def test_range_parameter_changes_the_bucket_count(self, server) -> None:
        _, body = _get(server, "/api/overview?range=30d")
        assert len(body["daily"]) == 30

    def test_unknown_range_falls_back_rather_than_erroring(self, server) -> None:
        """A stale bookmark should still render something."""
        status, _ = _get(server, "/api/overview?range=banana")
        assert status == 200

    def test_activity_is_empty_but_well_formed(self, server) -> None:
        _, body = _get(server, "/api/activity")
        assert body == {"sessions": [], "total": 0}

    def test_missing_session_is_404(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(server, "/api/session?id=nope")
        assert excinfo.value.code == 404

    def test_performance_includes_health(self, server) -> None:
        _, body = _get(server, "/api/performance?range=7d")
        assert body["health"]["status"] == "unknown"
        assert body["health"]["detail"]


class TestStatusAndControl:
    def test_status_reports_no_engine_when_unattached(self, server) -> None:
        _, body = _get(server, "/api/status")
        assert body["listening"] is None
        assert body["can_toggle"] is False

    def test_status_reports_engine_state_when_attached(self, controlled_server) -> None:
        srv, _ = controlled_server
        _, body = _get(srv, "/api/status")
        assert body["listening"] is True
        assert body["can_toggle"] is True
        assert body["engine"]["hotkey"] == "ctrl_r"

    def test_toggling_listening_reaches_the_engine(self, controlled_server) -> None:
        srv, state = controlled_server
        _, body = _post(srv, "/api/listening", {"listening": False})
        assert state["listening"] is False
        assert body["listening"] is False

    def test_toggling_without_an_engine_is_a_conflict(self, server) -> None:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(server, "/api/listening", {"listening": False})
        assert excinfo.value.code == 409

    def test_cross_origin_post_is_refused(self, controlled_server) -> None:
        """Closes the hole from the other side: a page the user happens to be
        visiting must not be able to script this endpoint."""
        srv, state = controlled_server
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _post(srv, "/api/listening", {"listening": False}, origin="https://evil.example")
        assert excinfo.value.code == 403
        assert state["listening"] is True

    def test_malformed_json_is_a_bad_request(self, controlled_server) -> None:
        srv, _ = controlled_server
        request = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/listening?token={srv.token}",
            data=b"{not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(request)
        assert excinfo.value.code == 400


class TestLifecycle:
    def test_start_is_idempotent(self, server) -> None:
        assert server.start() == server.url

    def test_stop_then_start_again(self, tmp_path) -> None:
        srv = DashboardServer(
            history_db=tmp_path / "h.db", latency_db=tmp_path / "l.db", typing_wpm=45.0
        )
        srv.start()
        srv.stop()
        srv.start()
        assert _get(srv, "/api/status")[0] == 200
        srv.stop()

    def test_binds_loopback_only(self, server) -> None:
        assert "127.0.0.1" in server.url


class TestLegacySchema:
    """Regression: `flow dashboard` used to read history.db without ever
    migrating it, so every query referenced a schema-v2 column that a v1
    database does not have and the whole page 500'd."""

    @staticmethod
    def _write_v1(path):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE history_entries ("
            " id TEXT PRIMARY KEY, created_at REAL NOT NULL, kind TEXT NOT NULL,"
            " text TEXT NOT NULL, raw_text TEXT NOT NULL, instruction TEXT,"
            " focused_process TEXT);"
        )
        conn.execute(
            "INSERT INTO history_entries VALUES"
            " ('old', 1000.0, 'dictation', 'legacy text', 'legacy raw', NULL, 'code.exe')"
        )
        conn.execute("PRAGMA user_version=1")
        conn.commit()
        conn.close()

    def test_a_v1_database_is_migrated_on_start(self, tmp_path) -> None:
        import sqlite3

        history = tmp_path / "history.db"
        self._write_v1(history)
        srv = DashboardServer(
            history_db=history, latency_db=tmp_path / "latency.db", typing_wpm=45.0
        )
        srv.start()
        try:
            version = sqlite3.connect(history).execute("PRAGMA user_version").fetchone()[0]
            assert version == 2
            status, body = _get(srv, "/api/overview?range=7d")
            assert status == 200
            assert body["all_time"]["sessions"] == 1
            # The word count is recoverable from the stored text; WPM is not.
            assert body["all_time"]["words"] == 2
            assert body["all_time"]["avg_wpm"] is None
        finally:
            srv.stop()

    def test_every_endpoint_serves_against_a_migrated_v1_database(self, tmp_path) -> None:
        history = tmp_path / "history.db"
        self._write_v1(history)
        srv = DashboardServer(
            history_db=history, latency_db=tmp_path / "latency.db", typing_wpm=45.0
        )
        srv.start()
        try:
            for path in ("/api/overview?range=30d", "/api/activity", "/api/performance?range=7d", "/api/status"):
                assert _get(srv, path)[0] == 200, path
        finally:
            srv.stop()

    def test_an_unmigratable_database_degrades_instead_of_failing(self, tmp_path) -> None:
        """If the schema still cannot be read, the dashboard reports empty
        rather than 500 -- a loud failure here would read as the *engine*
        being broken, which it would not be."""
        from flow.dashboard.queries import DashboardQueries

        history = tmp_path / "history.db"
        self._write_v1(history)  # opened directly, without the migration
        with DashboardQueries.open(history, tmp_path / "latency.db") as q:
            assert q.has_history is False
            assert q.summary().sessions == 0
            assert q.sessions() == []
