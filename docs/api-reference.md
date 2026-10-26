# Sway — Local HTTP API Reference

The dashboard window and the desktop shell talk to the engine over a **loopback-only
HTTP server** running inside the Sway process. This is the complete contract.

**Verified against:** `1e85b9f`, 2026-08-26 (`src/sway/dashboard/server.py`).

> This is **not a public API and never becomes one.** It binds `127.0.0.1` on an
> ephemeral port, exists only while the app is running, and is documented here so the
> UI and the second-launch focus path have a contract to hold each other to.

---

## 1. Transport and addressing

| Property | Value |
|---|---|
| Host | `127.0.0.1` — never `0.0.0.0`, never a named interface |
| Port | **ephemeral**, assigned by the OS at startup, different every launch |
| Server | stdlib `http.server.ThreadingHTTPServer` on a daemon thread |
| Content type | `application/json` on every `/api/` route |
| Discovery | the running instance writes `{port, token, pid}` to `<data_dir>/instance.json` |

If the server fails to start, the engine logs it and keeps dictating. The dashboard is
a reporting surface, never a prerequisite for the product working.

## 2. Authentication

Every `/api/` request must carry the **per-launch token**, minted at startup:

```
X-Sway-Token: <token>
```

…or, for the initial page load only, as a `?token=` query parameter. `app.js` reads it
from the URL once and immediately scrubs it from the address bar with
`history.replaceState`, so a screenshot or a shoulder-surfer never captures it.

- Comparison is **constant-time** (`secrets.compare_digest`). A timing oracle on
  loopback is cheap to close and cheaper than reasoning about.
- The token is never logged. `log_message` is overridden precisely because the
  request line carries it — and because `BaseHTTPRequestHandler` writes to stderr,
  which does not exist in the windowed build.
- Without the token, any web page the user visits could otherwise script requests at
  `localhost` and read their entire dictation history.

**POSTs additionally require same-origin.** An `Origin` header, if present, must match
the server's own `host:port`. Same-origin fetches from the page omit it, which is
accepted; a cross-origin request that sends a mismatched one is rejected outright.

## 3. Status codes

| Code | Meaning |
|---|---|
| `200` | Success. Body is the documented payload. |
| `400` | Malformed JSON, or an `ActionError` — a problem with what was asked for, not a bug. The message is written to be shown to the user. |
| `403` | Invalid or missing token, or a cross-origin write. |
| `404` | Unknown endpoint, or `no such session: <id>`. |
| `500` | Query or internal failure. Logged in full; the response body is deliberately terse. |

Errors are always `{"error": "<message>"}`.

Request bodies are capped at **64 KB**.

---

## 4. GET routes

All read-only. None changes state.

### `GET /api/home`
Everything the Home screen shows, in **one** request. Composite on purpose: Home is
the screen the window opens on, and four parallel requests on a cold WebView is
exactly where a "native" app stops feeling native. One query object serves all of it.

### `GET /api/overview`
Counters and summary figures: dictations today / this week / total, words, estimated
time saved (labelled an estimate), streaks.

### `GET /api/activity`
The recent-dictation list. Supports search and filtering by kind.

### `GET /api/session?id=<entry_id>`
Full detail for one history entry — the pasted text, the raw transcript it came from,
the instruction (for commands), and the joined per-stage timings.
`404 no such session: <id>` when it does not exist.

### `GET /api/performance?range=<30d>`
Latency percentiles and the per-stage breakdown, plus:

- `health` — `{status, headline, detail}`, the engine's own assessment
- `understanding` — corrections detected, transforms fired, snippets substituted,
  model calls avoided
- `range` — echoed back

Stages absent from older runs are reported as **absent**, not as 0 ms.

### `GET /api/status`
The live engine state, polled by the UI:

```json
{
  "listening": true,
  "can_toggle": true,
  "engine": { "...": "describe() output" },
  "now": 1756000000.0,
  "today": "2026-08-26",
  "level": 0.31,
  "hotkey_down": false,
  "bindings_down": {},
  "hands_free_active": false,
  "last_error": null
}
```

Every engine read here is defensive: a failure to describe the engine logs at debug
and returns a default rather than failing the request.

### `GET /api/settings`
The full settings payload plus the enumerations the UI needs to render its pickers —
cleanup styles, context modes, overlay positions, trigger types.

### `GET /api/dictionary`
Every term with its provenance, pin state, hit count, and last use.

### `GET /api/dictionary/export`
The dictionary as a downloadable payload. A **GET**, because it produces a file and
changes nothing; the UI turns it into a download client-side. The server never writes
to disk on the user's behalf.

### `GET /api/devices`
Available input devices, for the microphone picker.

### `GET /api/diagnostics?limit=<50>`
Recent log lines for the About / troubleshooting view. `limit` is clamped to 200.

---

## 5. POST routes

Every write. All require token **and** same-origin.

### Lifecycle

| Route | Body | Returns |
|---|---|---|
| `POST /api/listening` | `{"listening": bool}` | the `/api/status` payload |
| `POST /api/focus` | `{}` | `{"focused": true}` |
| `POST /api/quit` | `{}` | `{"quitting": true}` |

`/api/focus` is **not called by the dashboard UI.** It is called by another *process*
of this app: a second launch that failed to take the single-instance mutex reads the
port and token from `instance.json` and POSTs here, then exits 0. It sits behind the
same token and origin checks as everything else, so it is no weaker than the listening
toggle next to it.

`/api/quit` answers *before* shutting down — the caller is the UI, and a connection
reset mid-response renders as an error toast on a window that is about to close
anyway.

Each of these returns `403` if no engine is attached.

### Settings and content

All of these apply the change, notify the engine, and return the **full settings
payload**, so the UI never has to re-fetch to stay consistent.

| Route | Body |
|---|---|
| `POST /api/settings` | partial settings object; returns `applied` alongside the payload, saying whether the change took effect live |
| `POST /api/triggers/save` | one snippet/date trigger |
| `POST /api/triggers/delete` | `{"id": ...}` |
| `POST /api/transforms/save` | one transform (name, instruction, description, phrase, shortcuts, examples, expectations) |
| `POST /api/transforms/delete` | `{"id": ...}` |
| `POST /api/transforms/reset` | `{}` — restores the shipped set |
| `POST /api/shortcuts/save` | one activation binding, validated against the currently effective bindings for conflicts |
| `POST /api/shortcuts/reset` | `{"action": ...}` |
| `POST /api/app-rules/save` | one per-app cleanup rule |
| `POST /api/app-rules/delete` | `{"id": ...}` |

Validation happens in `dashboard/actions.py`, not in the route. An invalid value
raises `ActionError` → `400` with a message written to be shown to the user.

### API key

| Route | Body | Returns |
|---|---|---|
| `POST /api/key` | `{"key": "..."}` | key status |
| `POST /api/key/test` | `{}` | the result of one cheap live call |

`/api/key` writes to Windows Credential Manager, never to a file. `/api/key/test`
requires an attached engine.

### Dictionary

| Route | Body |
|---|---|
| `POST /api/dictionary/add` | `{"terms": [...]}` |
| `POST /api/dictionary/remove` | `{"terms": [...]}` |
| `POST /api/dictionary/pin` | `{"term": ..., "pinned": bool}` |
| `POST /api/dictionary/import` | an exported payload |

### History

| Route | Body |
|---|---|
| `POST /api/history/delete` | `{"id": ...}` |
| `POST /api/history/delete-all` | confirmation payload |

`delete-all` genuinely wipes the rows — it is not a UI-level hide.

---

## 6. Static routes

Anything not under `/api/` is served from `src/sway/dashboard/static/`:
`index.html`, `app.js`, `styles.css`. Text and JavaScript responses are served with an
explicit charset.

---

## 7. Client-side rules

Two rules in `app.js` that matter more than style, restated here because they are part
of the contract:

1. **Every string that came from the user, from a transcript, or from Groq is escaped
   before it reaches `innerHTML`.** Transcripts are arbitrary text the app pastes into
   other applications; treating them as markup would be a self-inflicted XSS in a
   window that can also read the user's history.
2. **The token never leaves the fetch calls.** It is not put in the address bar, not
   logged, and not rendered.

A network failure from `fetch` has exactly one cause worth reporting — the engine
going away underneath the page — and is surfaced as *"Sway stopped responding."*
rather than a transport error.

---

## 8. What is deliberately absent

- **No FastAPI, no framework.** Serving a handful of static files and a dozen
  read-only JSON routes to one user on loopback is squarely inside what
  `ThreadingHTTPServer` does well. A framework would add a dependency and a startup
  cost for nothing.
- **No WebSocket, no SSE yet.** The UI polls. Live updates without a manual refresh
  are a known partial (PRD FR-9.11).
- **No pywebview JS bridge.** Keeping the loopback socket preserves opening the
  dashboard in a real browser with devtools during development, which a webview
  cannot offer, and the second-launch focus path already depends on the socket. See
  [ADR-005](decision-log.md).
- **No authentication beyond the launch token.** There are no users, no sessions, and
  no accounts — by design.
