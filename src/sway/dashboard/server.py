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
import sys
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

from sway.dashboard import actions
from sway.dashboard.actions import ActionError
from sway.dashboard.queries import DashboardQueries, DashboardQueryError, start_of_today
from sway.errors import SwayError
from sway.settings import (
    CLEANUP_STYLES as _CLEANUP_STYLES,
    CONTEXT_MODES as _CONTEXT_MODES,
    OVERLAY_POSITIONS as _OVERLAY_POSITIONS,
    TRIGGER_TYPES as _TRIGGER_TYPES,
    SettingsStore,
    to_dict as settings_to_dict,
)

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Windows the UI offers. ``None`` means all time.
RANGE_DAYS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}

#: Cap on a single Activity page, so a pathological ``limit`` cannot make the
#: server materialise the entire history in memory.
MAX_PAGE = 500


def _input_devices() -> list[dict[str, Any]]:
    """Microphones, or an empty list on a machine with no working backend.

    Imported lazily: ``sounddevice`` loads PortAudio, and ``flow dashboard``
    run standalone has no reason to pay that cost or to fail if the audio
    stack is broken.
    """
    try:
        from sway.audio.capture import input_devices

        return input_devices()
    except Exception:
        logger.warning("could not enumerate input devices", exc_info=True)
        return []


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
    #: Bring the app forward. Called by a *second launch* of Sway rather than
    #: by the UI (see flow.instance), which is why it lives here and not in
    #: the browser-facing routes.
    focus: Callable[[], None] | None = None
    #: Make one cheap authenticated call to Groq and report what happened.
    #: Owned by the engine because it holds the pre-warmed HTTP client; the
    #: server has no business creating a second one.
    test_key: Callable[[], dict[str, Any]] | None = None
    #: Told after any settings write, so the engine can hot-apply what it can
    #: (device, retention, overlay position) and flag what needs a restart.
    on_settings_changed: Callable[[], dict[str, Any]] | None = None
    #: Quit the whole application, cleanly. Exposed so the window's own
    #: control and the tray's Quit item end in exactly the same shutdown.
    quit: Callable[[], None] | None = None
    #: The last error the engine surfaced, or ``None``. Home renders it.
    last_error: Callable[[], dict[str, Any] | None] | None = None
    #: Live mic level as a 0-3 bucket, and whether the hotkey is held right
    #: now. Both exist for onboarding, which has to let the user *observe*
    #: that the mic and the hook work rather than being told they do.
    input_level: Callable[[], int] | None = None
    hotkey_down: Callable[[], bool] | None = None
    #: Whether each *other* configurable activation binding is physically
    #: held right now, keyed "command"/"dictation_chord"/"command_chord" --
    #: the same live-preview purpose as hotkey_down, generalised for the
    #: Shortcuts settings surface (Phase 6) once more than one binding is
    #: user-visible at a time.
    bindings_down: Callable[[], dict[str, bool]] | None = None
    #: Whether a hands-free (tap to start/stop) dictation session is
    #: currently in progress.
    hands_free_active: Callable[[], bool] | None = None


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """``ThreadingHTTPServer`` that does not shout when a client walks away.

    ``socketserver.handle_error`` prints a full traceback straight to stderr
    for any exception that escapes a handler, and a client vanishing mid
    request raises one before our handler is even entered -- while
    ``handle_one_request`` is reading the next keep-alive request line.

    That happens constantly here and is never a fault. The dashboard polls
    (Home every 5s, onboarding every 140-220ms), and the window navigates,
    hides and closes underneath those polls -- so there is nearly always a
    request in flight when the connection goes. Printing a traceback for each
    one buries the failures that do matter.
    """

    def handle_error(self, request, client_address):  # noqa: D102 - stdlib naming
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError):
            logger.debug("dashboard client %s disconnected: %s", client_address, exc)
            return
        super().handle_error(request, client_address)


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
        dictionary_db: Path | None = None,
        settings: SettingsStore | None = None,
        log_file: Path | None = None,
    ) -> None:
        self._history_db = history_db
        self._latency_db = latency_db
        self._typing_wpm = typing_wpm
        self._control = control or EngineControl()
        self._host = host
        self._port = port
        self._dictionary_db = dictionary_db
        #: Read fresh on every /api/diagnostics request (see
        #: actions.recent_diagnostics) rather than cached, so this is never
        #: the thing that's stale when the user is looking at it.
        self._log_file = log_file
        #: Optional so ``flow dashboard`` standalone still reports history
        #: without being able to change anything. Every write route checks
        #: for it and returns a clear "not available" rather than crashing.
        self._settings = settings
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
        self._httpd = _QuietThreadingHTTPServer((self._host, self._port), handler)
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
            from sway.history import HistoryStore

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
                perf["understanding"] = q.understanding(since=since)
                return perf
            if path == "/api/status":
                return self.status()
            if path == "/api/home":
                return self._home(q)
            if path == "/api/settings":
                return self.settings_payload()
            if path == "/api/dictionary":
                return self._dictionary()
            if path == "/api/devices":
                return {"devices": _input_devices()}
            if path == "/api/diagnostics":
                return actions.recent_diagnostics(self._log_file, limit=min(_int(params, "limit", 50), 200))
            if path == "/api/dictionary/export":
                # A GET, because it produces a file and changes nothing. The
                # UI turns it into a download client-side; the server never
                # writes to disk on the user's behalf.
                return actions.dictionary_export(self._require_dictionary())
        raise KeyError(path)

    # -- composite reads for the new UI ------------------------------------

    def _home(self, q: DashboardQueries) -> dict[str, Any]:
        """Everything the Home screen shows, in one request.

        Composite on purpose: Home is the screen the window opens on, and
        four parallel requests on a cold WebView is exactly where a "native"
        app stops feeling native. One query object serves all of it.
        """
        today = q.summary(since=start_of_today())
        streaks = q.streaks()
        recent = q.sessions(limit=8)
        health = q.health()

        return {
            "status": self.status(),
            "today": _summary_dict(today),
            "all_time": _summary_dict(q.summary()),
            "streaks": {"current": streaks.current, "longest": streaks.longest},
            "recent": recent,
            "health": {
                "status": health.status,
                "headline": health.headline,
                "detail": health.detail,
            },
            "typing_wpm": self._typing_wpm,
            "has_data": q.session_count() > 0,
        }

    def settings_payload(self) -> dict[str, Any]:
        """Current settings, engine facts, and key status.

        Assembled rather than dumped: the UI needs to distinguish "the user
        set this" from "this is what the engine resolved", and only this
        layer can see both.
        """
        settings = self._settings.get() if self._settings is not None else None
        engine: dict[str, Any] = {}
        if self._control.describe is not None:
            try:
                engine = self._control.describe()
            except Exception:
                logger.debug("could not describe the engine", exc_info=True)

        payload: dict[str, Any] = {
            "editable": self._settings is not None,
            "engine": engine,
            "key": actions.key_status().as_dict(),
            "overlay_positions": list(_OVERLAY_POSITIONS),
            "cleanup_styles": list(_CLEANUP_STYLES),
            "trigger_types": list(_TRIGGER_TYPES),
            "context_modes": list(_CONTEXT_MODES),
        }
        if settings is not None:
            payload["settings"] = settings_to_dict(settings)
            payload["triggers"] = actions.trigger_view(settings)
            payload["transforms"] = actions.transform_view(settings)
            payload["activation"] = actions.activation_view(settings, engine)
            payload["app_rules"] = [
                {"process": r.process, "style": r.style} for r in settings.app_rules
            ]
        return payload

    def _dictionary(self) -> dict[str, Any]:
        if self._dictionary_db is None:
            return {"available": False, "terms": []}
        try:
            from sway.dictionary import DictionaryStore

            with DictionaryStore.open(self._dictionary_db) as store:
                return {"available": True, **actions.dictionary_payload(store)}
        except Exception as exc:
            logger.warning("could not read the dictionary: %s", exc)
            return {"available": False, "terms": [], "entries": [], "error": str(exc)}

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
            "level": _safe_call(self._control.input_level, 0),
            "hotkey_down": _safe_call(self._control.hotkey_down, False),
            "bindings_down": _safe_call(self._control.bindings_down, {}),
            "hands_free_active": _safe_call(self._control.hands_free_active, False),
            "last_error": _safe_call(self._control.last_error, None),
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

        # -- settings and the things stored alongside them -----------------
        if path == "/api/settings":
            actions.update_settings(self._require_settings(), payload)
            return {**self.settings_payload(), "applied": self._notify_settings_changed()}
        if path == "/api/triggers/save":
            actions.save_trigger(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/triggers/delete":
            actions.delete_trigger(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/transforms/save":
            actions.save_transform(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/transforms/delete":
            actions.delete_transform(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/transforms/reset":
            actions.reset_transforms(self._require_settings())
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/shortcuts/save":
            actions.save_activation_binding(
                self._require_settings(), payload, effective=_safe_call(self._control.describe, {})
            )
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/shortcuts/reset":
            actions.reset_activation_binding(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/app-rules/save":
            actions.save_app_rule(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()
        if path == "/api/app-rules/delete":
            actions.delete_app_rule(self._require_settings(), payload)
            self._notify_settings_changed()
            return self.settings_payload()

        # -- the API key ---------------------------------------------------
        if path == "/api/key":
            result = actions.set_key(payload)
            self._notify_settings_changed()
            return result
        if path == "/api/key/test":
            if self._control.test_key is None:
                raise PermissionError("no engine attached")
            return self._control.test_key()

        # -- dictionary ----------------------------------------------------
        if path == "/api/dictionary/add":
            return actions.dictionary_add(self._require_dictionary(), payload)
        if path == "/api/dictionary/remove":
            return actions.dictionary_remove(self._require_dictionary(), payload)
        if path == "/api/dictionary/pin":
            return actions.dictionary_pin(self._require_dictionary(), payload)
        if path == "/api/dictionary/import":
            return actions.dictionary_import(self._require_dictionary(), payload)

        # -- history -------------------------------------------------------
        if path == "/api/history/delete":
            return actions.history_delete(self._history_db, payload)
        if path == "/api/history/delete-all":
            return actions.history_delete_all(self._history_db, payload)

        # -- lifecycle -----------------------------------------------------
        if path == "/api/quit":
            if self._control.quit is None:
                raise PermissionError("no engine attached")
            # Answer before shutting down: the caller is the UI, and a
            # connection reset mid-response renders as an error toast on a
            # window that is about to close anyway.
            self._control.quit()
            return {"quitting": True}

        raise KeyError(path)

    # -- helpers for the write routes --------------------------------------

    def _require_settings(self) -> SettingsStore:
        if self._settings is None:
            raise PermissionError("settings are not editable from this dashboard")
        return self._settings

    def _require_dictionary(self) -> Path:
        if self._dictionary_db is None:
            raise PermissionError("the dictionary is not available from this dashboard")
        return self._dictionary_db

    def _notify_settings_changed(self) -> dict[str, Any]:
        """Let the engine hot-apply what it can. Never fails the request.

        A settings write has already succeeded by the time this runs, so an
        engine that cannot apply it must not turn a saved setting into an
        error the user reads as "not saved".
        """
        if self._control.on_settings_changed is None:
            return {}
        try:
            return self._control.on_settings_changed() or {}
        except Exception:
            logger.exception("engine failed to apply changed settings")
            return {}


def _safe_call(fn: Callable[[], Any] | None, default: Any) -> Any:
    """Call an optional engine probe, or fall back.

    ``/api/status`` is polled several times a second by the onboarding meters,
    so one failing probe must degrade to its default rather than turning the
    whole status route into a 500 and freezing the screen the user is on.
    """
    if fn is None:
        return default
    try:
        return fn()
    except Exception:
        logger.debug("engine status probe failed", exc_info=True)
        return default


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
        server_version = "Sway"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            # BaseHTTPRequestHandler writes to stderr, which does not exist in
            # the windowed build; and the request line carries the auth token,
            # which must not reach the log file.
            logger.debug("dashboard %s", format % args)

        # -- auth ----------------------------------------------------------

        def _authorised(self, params: Mapping[str, list[str]]) -> bool:
            supplied = _one(params, "token", "") or (
                self.headers.get("X-Sway-Token") or ""
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

        def _client_gone(self, exc: Exception) -> None:
            """Note a disconnect and say nothing further on a dead socket.

            The dashboard polls: Home every 5s, onboarding every 140-220ms.
            So there is almost always a request in flight when the window
            navigates, is hidden, or is closed -- and the webview drops the
            connection mid-response. That surfaced as a full ``ConnectionAborted``
            traceback logged at ERROR, and then a *second*, uglier one from
            socketserver, because the handler answered the failure by trying
            to write an error response down the same dead socket.

            Neither is a fault. The client left; there is nobody to tell.
            """
            logger.debug("dashboard client disconnected mid-response: %s", exc)
            # Stop socketserver from trying to reuse a connection that is gone.
            self.close_connection = True

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
                except ConnectionError as exc:
                    # Ordered before the handlers below: a disconnect is not a
                    # query failure, and answering it would raise again.
                    self._client_gone(exc)
                except LookupError as exc:
                    self._error(HTTPStatus.NOT_FOUND, f"no such session: {exc}")
                except KeyError:
                    self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
                except DashboardQueryError as exc:
                    logger.warning("dashboard query failed: %s", exc)
                    self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
                except Exception:
                    logger.exception("dashboard request failed")
                    self._safe_error()
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
            except ConnectionError as exc:
                self._client_gone(exc)
            except ActionError as exc:
                # A problem with what was asked for, not a bug. The message is
                # already written for a human, so it goes straight to the UI.
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except PermissionError as exc:
                self._error(HTTPStatus.CONFLICT, str(exc))
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            except SwayError as exc:
                # Expected store-level failures (a locked database, a full
                # disk) already carry a user-facing message.
                logger.warning("dashboard command failed: %s", exc)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            except Exception:
                logger.exception("dashboard command failed")
                self._safe_error()

        def _safe_error(self) -> None:
            """Report an internal error, unless the client has already gone.

            Every error path needs this: the failure that put us here is very
            often the write itself, and a second write on the same socket
            raises straight out of the handler and into socketserver's own
            traceback.
            """
            try:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
            except ConnectionError as exc:
                self._client_gone(exc)

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
            try:
                self._send(HTTPStatus.OK, target.read_bytes(), content_type)
            except ConnectionError as exc:
                # A reload or a navigation abandons in-flight asset requests
                # just as readily as API ones.
                self._client_gone(exc)

    return Handler


__all__ = ["RANGE_DAYS", "DashboardServer", "EngineControl"]
