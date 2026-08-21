"""The dashboard's local HTTP server.

Deliberately built on stdlib ``http.server``. CLAUDE.md rules out FastAPI and
any public listener, and what this needs to do -- hand a browser a handful of
static files and answer a dozen read-only JSON routes -- is squarely inside
what ``ThreadingHTTPServer`` does well. A framework would add a dependency
and a startup cost to serve one user on loopback.

Three things make this safe to run inside a process that can paste text into
whatever window has focus:

* **Loopback only.** The socket binds ``127.0.0.1`` on an ephemeral port, so
  nothing outside the machine can reach it.
* **A per-launch token.** Every request must carry the token minted at
  startup, which is embedded in the URL the browser is opened with. Without
  it, any web page the user visits could otherwise script requests at
  ``localhost`` and read their entire dictation history.
* **Origin checks on writes.** State-changing routes reject cross-origin
  requests outright, which closes the same hole from the other side.

The server runs on its own daemon thread and never blocks the engine. If it
fails to start, the engine logs it and keeps dictating: the dashboard is a
reporting surface, never a prerequisite for the product working.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from flow.dashboard.queries import DashboardQueries, DashboardQueryError, start_of_today

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Windows the UI offers. ``None`` means all time.
RANGE_DAYS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}

#: Cap on a single Activity page, so a pathological ``limit`` cannot make the
#: server materialise the entire history in memory.
MAX_PAGE = 500


@dataclass(frozen=True, slots=True)
class EngineControl:
    """What the dashboard is allowed to do to the running engine.

    Passed in rather than reached for, so the server has no import-time
    dependency on the engine and can be tested with plain callables. Every
    field is optional: a dashboard opened by ``flow dashboard`` with no engine
    running still reports history, it just cannot toggle anything.
    """

    is_listening: Callable[[], bool] | None = None
    set_listening: Callable[[bool], None] | None = None
    #: Live engine facts the databases cannot know: hotkey, mic, model.
    describe: Callable[[], dict[str, Any]] | None = None
    #: Bring the app forward. Called by a *second launch* of Flow rather than
    #: by the UI (see flow.instance), which is why it lives here and not in
    #: the browser-facing routes.
    focus: Callable[[], None] | None = None


class DashboardServer:
    """Serves the dashboard on loopback. Start it, open :attr:`url`, stop it."""

    def __init__(
        self,
        *,
        history_db: Path,
        latency_db: Path,
        typing_wpm: float,
        control: EngineControl | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self._history_db = history_db
        self._latency_db = latency_db
        self._typing_wpm = typing_wpm
        self._control = control or EngineControl()
        self._host = host
        self._port = port
        self._token = secrets.token_urlsafe(24)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> str:
        """Bind, serve on a background thread, and return the URL to open."""
        if self._httpd is not None:
            return self.url

        self._ensure_schema()

        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        # daemon_threads: a browser tab holding a keep-alive connection open
        # must never stop the app from exiting when the user picks Quit.
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="flow-dashboard", daemon=True
        )
        self._thread.start()
        logger.info("dashboard listening on %s", self.url)
        return self.url

    def _ensure_schema(self) -> None:
        """Bring history.db up to the schema this build reads.

        The engine migrates on startup, but ``flow dashboard`` can be run with
        no engine ever having started against this data directory -- and after
        an upgrade, the databases on disk are whatever the *previous* build
        left. Opening the store applies its migrations and closes again;
        without this the dashboard queries every column schema v2 added and
        fails on all of them.

        Best-effort: a migration failure leaves the dashboard reporting empty
        states (see DashboardQueries.has_history) rather than refusing to
        start, because the engine itself is unaffected either way.
        """
        try:
            from flow.history import HistoryStore

            HistoryStore.open(self._history_db).close()
        except Exception:
            logger.warning("could not migrate the history database", exc_info=True)

    def stop(self, *, timeout: float = 2.0) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    @property
    def url(self) -> str:
        """The URL to open, token included -- this is what authenticates the
        browser session, so it must not be logged anywhere shareable."""
        return f"http://{self._host}:{self._port}/?token={self._token}"

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        return self._token

    # -- data --------------------------------------------------------------

    def _queries(self) -> DashboardQueries:
        """A fresh connection per request.

        sqlite3 connections are cheap to open against an already-created file,
        and this keeps the threaded handler free of any cross-thread sharing
        question -- each request owns its connection for its lifetime.
        """
        return DashboardQueries.open(
            self._history_db, self._latency_db, typing_wpm=self._typing_wpm
        )

    def handle_api(self, path: str, params: Mapping[str, list[str]]) -> Any:
        """Route one GET. Raises :class:`KeyError` for an unknown route."""
        with self._queries() as q:
            if path == "/api/overview":
                return self._overview(q, params)
            if path == "/api/activity":
                return self._activity(q, params)
            if path == "/api/session":
                entry_id = _one(params, "id", "")
                detail = q.session_detail(entry_id)
                if detail is None:
                    raise LookupError(entry_id)
                return detail
            if path == "/api/performance":
                since = _since_from(params)
                perf = q.performance(since=since)
                health = q.health()
                perf["health"] = {
                    "status": health.status,
                    "headline": health.headline,
                    "detail": health.detail,
                }
                perf["range"] = _one(params, "range", "30d")
                return perf
            if path == "/api/status":
                return self.status()
        raise KeyError(path)

    def _overview(self, q: DashboardQueries, params: Mapping[str, list[str]]) -> dict[str, Any]:
        range_key = _one(params, "range", "7d")
        days = RANGE_DAYS.get(range_key, 7)
        since = _since_from(params)

        today = q.summary(since=start_of_today())
        window = q.summary(since=since)
        streaks = q.streaks()
        buckets = q.daily_buckets(days if days is not None else max(streaks.span_days, 30))

        return {
            "range": range_key,
            "today": _summary_dict(today),
            "window": _summary_dict(window),
            "all_time": _summary_dict(q.summary()),
            "streaks": {
                "current": streaks.current,
                "longest": streaks.longest,
                "active_days": streaks.active_days,
                "span_days": streaks.span_days,
            },
            "daily": [
                {
                    "day": bucket.day.isoformat(),
                    "words": bucket.words,
                    "sessions": bucket.sessions,
                    "avg_wpm": bucket.avg_wpm,
                }
                for bucket in buckets
            ],
            "apps": q.app_usage(since=since),
            "typing_wpm": self._typing_wpm,
            "has_data": q.session_count() > 0,
        }

    def _activity(self, q: DashboardQueries, params: Mapping[str, list[str]]) -> dict[str, Any]:
        limit = min(_int(params, "limit", 100), MAX_PAGE)
        return {
            "sessions": q.sessions(
                query=_one(params, "q", ""),
                limit=limit,
                offset=max(0, _int(params, "offset", 0)),
                kind=_one(params, "kind", "") or None,
            ),
            "total": q.session_count(),
        }

    def status(self) -> dict[str, Any]:
        """Live engine state. Everything here is unavailable rather than
        guessed when no engine is attached (``flow dashboard`` standalone).
        """
        listening: bool | None = None
        if self._control.is_listening is not None:
            try:
                listening = bool(self._control.is_listening())
            except Exception:
                logger.debug("could not read engine listening state", exc_info=True)
        engine: dict[str, Any] = {}
        if self._control.describe is not None:
            try:
                engine = self._control.describe()
            except Exception:
                logger.debug("could not describe the engine", exc_info=True)
        return {
            "listening": listening,
            "can_toggle": self._control.set_listening is not None,
            "engine": engine,
            "now": time.time(),
            "today": date.today().isoformat(),
        }

    def handle_command(self, path: str, payload: Mapping[str, Any]) -> Any:
        """Route one POST. The only writes the dashboard performs."""
        if path == "/api/listening":
            if self._control.set_listening is None:
                raise PermissionError("no engine attached")
            self._control.set_listening(bool(payload.get("listening", True)))
            return self.status()
        if path == "/api/focus":
            # D1: how a second launch reaches the first. Not called by the
            # dashboard UI -- it is called by another *process* of this app
            # (see flow.instance), which reads the port and token from
            # instance.json. It is a POST behind the same token and origin
            # checks as everything else, so it is no weaker than the
            # listening toggle sitting next to it.
            if self._control.focus is None:
                raise PermissionError("no engine attached")
            self._control.focus()
            return {"focused": True}
        raise KeyError(path)


def _summary_dict(summary: Any) -> dict[str, Any]:
    return {
        "words": summary.words,
        "sessions": summary.sessions,
        "avg_wpm": summary.avg_wpm,
        "time_saved_s": summary.time_saved_s,
        "median_latency_ms": summary.median_latency_ms,
        "measured_sessions": summary.measured_sessions,
    }


def _one(params: Mapping[str, list[str]], key: str, default: str) -> str:
    values = params.get(key)
    return values[0] if values else default


def _int(params: Mapping[str, list[str]], key: str, default: int) -> int:
    try:
        return int(_one(params, key, str(default)))
    except ValueError:
        return default


def _since_from(params: Mapping[str, list[str]]) -> float | None:
    """Translate a ``range`` parameter into an epoch cutoff, or ``None`` for
    all time. An unrecognised value falls back to 7 days rather than erroring:
    a stale bookmark should still render something.
    """
    days = RANGE_DAYS.get(_one(params, "range", "7d"), 7)
    return None if days is None else time.time() - days * 86_400


def _make_handler(server: DashboardServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 so the browser can keep one connection for the whole page
        # load rather than reconnecting per asset.
        protocol_version = "HTTP/1.1"
        server_version = "Flow"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # BaseHTTPRequestHandler writes to stderr, which does not exist in
            # the windowed build; and the request line carries the auth token,
            # which must not reach the log file.
            logger.debug("dashboard %s", format % args)

        # -- auth ----------------------------------------------------------

        def _authorised(self, params: Mapping[str, list[str]]) -> bool:
            supplied = _one(params, "token", "") or (
                self.headers.get("X-Flow-Token") or ""
            )
            # Constant-time: the token is short-lived, but a timing oracle on
            # loopback is cheap to close and cheaper than reasoning about it.
            return secrets.compare_digest(supplied, server.token)

        def _same_origin(self) -> bool:
            """Reject a cross-site POST even if it somehow carried the token."""
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # same-origin fetches from the page omit it
            return urlparse(origin).netloc == f"{server._host}:{server._port}"

        # -- responses -----------------------------------------------------

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The dashboard is live data; a cached Overview would quietly show
            # yesterday's numbers after a dictation.
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json(status, {"error": message})

        # -- routing -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            path = parsed.path

            if path.startswith("/api/"):
                if not self._authorised(params):
                    self._error(HTTPStatus.FORBIDDEN, "invalid or missing token")
                    return
                try:
                    self._json(HTTPStatus.OK, server.handle_api(path, params))
                except LookupError as exc:
                    self._error(HTTPStatus.NOT_FOUND, f"no such session: {exc}")
                except KeyError:
                    self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                except DashboardQueryError as exc:
                    logger.warning("dashboard query failed: %s", exc)
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                except Exception:
                    logger.exception("dashboard request failed")
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
                return

            self._serve_static(path)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if not self._authorised(params) or not self._same_origin():
                self._error(HTTPStatus.FORBIDDEN, "invalid or missing token")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(min(length, 64 * 1024)) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                self._error(HTTPStatus.BAD_REQUEST, "malformed JSON")
                return
            try:
                self._json(HTTPStatus.OK, server.handle_command(parsed.path, payload))
            except PermissionError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            except Exception:
                logger.exception("dashboard command failed")
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

        def _serve_static(self, path: str) -> None:
            relative = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC_DIR / relative).resolve()
            # Containment check: without it, a request for ../../.env would
            # hand out the user's Groq key.
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._error(HTTPStatus.FORBIDDEN, "forbidden")
                return
            if not target.is_file():
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith(("text/", "application/javascript")):
                content_type += "; charset=utf-8"
            self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    return Handler


__all__ = ["RANGE_DAYS", "DashboardServer", "EngineControl"]
