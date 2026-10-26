# Sway — Technical Specification

**Verified against:** `1e85b9f`, 2026-08-26. Python 3.11+, Windows target, 1391 tests passing.
**Authority:** [`CLAUDE.md`](../CLAUDE.md) > [`PRODUCTION_BRIEF.md`](../PRODUCTION_BRIEF.md) > this document.
**Companion docs:** [Product Brief](product-brief.md) · [PRD](prd.md) · [API Reference](api-reference.md) · [Data & Privacy](data-and-privacy.md) · [Decision Log](decision-log.md) · [Test Plan](test-plan.md)

---

## 1. System overview

Sway is a **single Python process** with no server component, no container, and no
background service. Everything runs in-process behind an explicit threading model,
and at most two network calls per dictation leave the machine — one transcription, plus one cleanup/instruction call when it is not skipped (streaming, off by default, adds one request per chunk).

```
                         ┌──────────────────────────────────────────────┐
                         │              Sway.exe (one process)          │
                         │                                              │
  microphone ──────────► │  PortAudio callback ──► RingBuffer (16k mono) │
   (always open)         │       (no I/O, no alloc)        │            │
                         │                                 │            │
  keyboard / mouse ────► │  BindingListener (pynput)       │            │
   (global hooks)        │       │ engaged / disengaged    │            │
                         │       ▼                         ▼            │
                         │   worker thread ──► VAD ──► FLAC ──► ┐       │
                         │       │                              │       │
                         │       │            NetworkLoop  ◄────┘       │
                         │       │        (one asyncio loop,            │
                         │       │         one httpx HTTP/2 client) ────┼──► api.groq.com
                         │       │                                      │      whisper-large-v3-turbo
                         │       │  ◄── transcript ─────────────────────┼──◄   gpt-oss-20b
                         │       ▼                                      │
                         │   local resolution (snippets, transforms,    │
                         │   corrections, profile) ──► cleanup call     │
                         │       │                                      │
                         │       ▼                                      │
                         │   TextInjector ──► clipboard save/set/       │
                         │                    Ctrl+V/restore ───────────┼──► focused app
                         │                                              │
                         │   SwayBar (Tk+PIL) · TrayIcon (pystray)      │
                         │   DashboardServer (127.0.0.1:ephemeral)      │
                         │   LatencyTracker ──► latency.db              │
                         │   HistoryStore ──► history.db                │
                         │   DictionaryStore ──► dictionary.db          │
                         └──────────────────────────────────────────────┘
```

---

## 2. Threading model

Getting this wrong is the difference between a 600 ms paste and a hang with the
microphone hot, so each thread's contract is stated explicitly.

| Thread | Owner | Contract |
|---|---|---|
| **Main** | `webview.start()` | `pywebview` refuses to run anywhere else and blocks until the last window closes. Started once at launch with the window created but hidden, because it cannot be started twice. Falls back to "main thread does nothing special" when pywebview/WebView2 is absent. |
| **Audio callback** | PortAudio, via `sounddevice.RawInputStream` | A bounds-checked memcpy into the ring buffer. **No I/O, no logging, no meaningful allocation, ever.** Never opens or closes the stream. |
| **Keyboard listener** | `pynput.keyboard.Listener` | One OS hook for *every* binding at once. Turns physical events into two abstract signals per binding: *engaged* and *disengaged*. Hands every disengage to a fresh worker immediately so it is free for the next event. |
| **Mouse listener** | `pynput.mouse.Listener` | Same, for mouse-button bindings. Same already-vendored dependency, no new package. |
| **Per-dictation worker** | `Engine` | One short-lived thread per activation. Runs VAD, encode, the network waits, local resolution, and the paste. Blocking here is fine; blocking the listener is not. |
| **Network loop** | `NetworkLoop` | One `asyncio` loop on one dedicated thread. Every Groq request is a coroutine submitted here. One persistent `httpx.AsyncClient(http2=True)`, pre-warmed with a HEAD request at startup. |
| **Streaming worker** | `ChunkStreamer` | Started on press, stopped on release, when `[streaming].enabled`. Cuts overlapping windows out of the ring buffer, VAD-trims and encodes inline, fires transcription without blocking. |
| **Bar / Tk** | `SwayBar.run()` | Tk objects are touched **only** on this thread. `set_state`, `set_level`, `set_hint`, `open_popover`, and `run_on_ui_thread` are the thread-safe doors in. Adaptive frame rate: ~60 fps while something moves or a dictation is live, 150 ms when resting. |
| **Tray** | `pystray` | Its own background thread (supported on Windows; a macOS adapter will have to reconcile this with Tk's main-thread requirement). Menu clicks call back into `Engine`. |
| **Dashboard** | `ThreadingHTTPServer` | Daemon thread, plus one per request. Each request opens its own SQLite connection for its lifetime, so no cross-thread sharing question arises. Never blocks the engine; if it fails to start, the engine logs it and keeps dictating. |
| **Latency writer** | `LatencyTracker` | Owns the SQLite connection. Callers only append to a **bounded** in-memory queue that drops on overflow — telemetry that can stall the dictation path is worse than telemetry with a hole in it. Drops are counted and logged. |
| **Timers** | `threading.Timer` | The 150 ms clipboard restore, hands-free timeout, activation timeout. Never a blocking sleep on a path the user is waiting on. |

**The rule that costs the most bugs:** the bar must never take focus. Paste targets
the *foreground* window, so a bar that activates — even for one frame — sends the
user's words into the bar. This is why the bar is PIL-composited Tk with
`WS_EX_TRANSPARENT | WS_EX_NOACTIVATE` rather than a WebView surface, which would
have been far easier to style. See [ADR-004](decision-log.md).

---

## 3. Module map

```
src/sway/
  engine.py            the wiring: capture → VAD → encode → transcribe → resolve →
                       cleanup → inject, plus commands, transforms, history, bar,
                       tray, dashboard, and settings application
  cli.py               argparse entry (run / devices / dictionary / history /
                       demo / report / config)
  config.py            TOML + env + .env layering, Secret, strict validation
  settings.py          the user-editable layer: settings.json, atomic writes,
                       per-field validation, unknown-key round-trip
  credentials.py       keyring → SWAY_GROQ_API_KEY → GROQ_API_KEY resolution
  instance.py          instance.json: how a second launch reaches the first

  audio/
    ring_buffer.py     allocation-free circular PCM buffer          (pure, tested)
    capture.py         sounddevice wrapper                          (thin, untested)
    vad.py             WebRTC VAD silence trimming                  (pure, tested)
    flac.py            PCM → FLAC via soundfile                     (pure, tested)

  hotkey.py            Binding parsing + one listener for every binding
  selection.py         reads the selection via a Ctrl+C round trip
  inject.py            clipboard save / set / Ctrl+V / restore
  net_loop.py          one asyncio loop, one dedicated thread
  groq_client.py       persistent HTTP/2 client, retry/backoff, rate-limit headers

  cleanup.py           cleanup prompts, skip heuristic, output guardrails (pure)
  corrections.py       self-correction detection — a router, never a rewriter (pure)
  profiles.py          process name → CleanupStyle                   (pure, tested)
  triggers.py          zero-cost snippet/date substitution           (pure, tested)
  transforms.py        Transform model, directive block, conflicts   (pure, tested)
  commands.py          voice-command prompt + message builder        (pure, tested)
  streaming.py         chunk clock + transcript stitching (pure) + ChunkStreamer
  dictionary.py        personal dictionary store + prompt-bias builder
  history.py           dictation/command history store
  analytics.py         words, WPM, time-saved estimate               (pure, tested)
  diff.py              word-level before/after diff via difflib      (pure, tested)

  bar.py               the floating bar: one renderer, dot → capsule
  orb.py               the activity orb, ported geometry-exact from Thinking Orbs
  motion.py            sampled (never stepped) animation curves      (pure, tested)
  branding.py          shared mark + palette for bar, tray, and window icons
  window.py            pywebview wrapper, with a browser fallback
  tray.py              pystray icon + menu
  history_window.py    the Tk history window
  gui.py               windowed entry point, single-instance mutex

  dashboard/
    server.py          stdlib ThreadingHTTPServer, token + origin checks
    queries.py         every read: summaries, percentiles, streaks, health
    actions.py         every write: settings, transforms, triggers, dictionary
    static/            index.html, app.js, styles.css

  latency/
    models.py          Stage enum, StageTiming, RunRecord
    tracker.py         non-blocking recording, bounded queue, writer thread
    store.py           SQLite schema and writes
    report.py          percentile summary + console table

  platform/
    __init__.py        the PlatformAdapter Protocol
    windows.py         the real implementation
    posix.py           best-effort, untested
```

---

## 4. The dictation pipeline

### 4.1 Stage sequence

Every stage boundary emits a timing event. The `Stage` enum is the canonical
spelling; the tracker also accepts arbitrary names so later work can add stages
without editing the enum.

| Stage | What it measures |
|---|---|
| `hotkey_hold` | key press → key release (how long the user spoke) |
| `dispatch` | release → the worker thread actually starts running |
| `capture` | drain the ring buffer into a contiguous frame |
| `vad` | trim leading/trailing silence |
| `encode` | FLAC, 16 kHz mono |
| `stt_first_byte` | request handed to httpx → response *headers* back |
| `transcribe` | the whole Whisper call (with streaming on, just the tail) |
| `merge` | stitch overlapping chunk transcripts into one |
| `selection` | Ctrl+C round trip to read the text selection |
| `command` | Groq applies a spoken instruction to a selection |
| `llm_first_token` | cleanup request sent → first content delta |
| `cleanup` | the whole streamed cleanup response |
| `inject` | `inject_start` → `inject_complete`: clipboard save / set / Ctrl+V |
| `TOTAL` | synthetic wall-to-wall row, never a real stage |

The 150 ms clipboard *restore* runs on a timer after `inject` closes, so it has never
counted against user-visible latency.

### 4.2 Schema stability

The persisted form of a stage is its **string value**, never its position in the
enum. Members may be inserted in pipeline order without touching stored data; only
the report's column ordering changes. What must never happen is renaming a value,
which would orphan every historical row carrying the old spelling.

A run recorded before a stage existed has **no row** for it. The report must render
that as a lower `Count`, never as a 0 ms measurement — folding a missing stage in as
zero would drag every percentile down and manufacture an improvement that never
happened. This is not hypothetical: the `cleanup` span originally wrapped the skip
branch as well as the model call, so 13 of 46 recorded spans were 0 ms skips, and
blending them inverted the signal — *improving the skip rate made the cleanup
percentile drop without the model call getting any faster*. The span now opens only
when the model is actually called, and skips are recorded as metadata.

### 4.3 Local resolution, before any model call

Between transcription and cleanup, four things happen entirely in-process — no
tokens, no network:

1. **Snippet substitution.** Configured phrases become stored text or today's date.
   Whole-phrase, word-boundary anchored, longest first, trailing punctuation
   stripped (Whisper punctuates aggressively). The raw pre-substitution transcript is
   preserved.
2. **Transform phrase matching.** Fired transforms are excised from the transcript
   and their ids recorded.
3. **Correction scan.** Detects a spoken self-correction. This is a *router, not a
   rewriter* — it never deletes a word. It exists to do two things the model cannot
   do from inside the pipeline: force the model to run (a five-word correction would
   otherwise hit the skip heuristic), and tell the guardrails which entities the
   speaker themselves withdrew, so the correct rewrite is not rejected for losing a
   number.
4. **Profile resolution.** The focused process name, read at press time, chooses
   casual / formal / verbatim, layered with any user per-app rule.

### 4.4 The one-call rule

If only local actions fired, the model is skipped entirely. Otherwise **one**
chat-completion request carries the cleanup system prompt *and* every fired
directive. Never one call per transform — that multiplies both latency and the
free-tier quota.

Prompt ordering is deliberate: the stable part first (system prompt + cleanup rules,
byte-identical across requests), the variable part last (directives, app label,
transcript). If Groq prompt caching applies to the cleanup model, this ordering is
what makes it pay.

Context travels **by reference, not by volume**: the focused app is a short label,
never window contents.

### 4.5 Skip heuristic

The model is bypassed when:

- the transcript is under `skip_min_words` (default 8) **and** contains no filler
  markers, checked against a language-specific list chosen from the resolved
  language (which is why transcription requests `verbose_json`). The threshold
  is scaled by `Language.word_ratio`: Hindi and Marathi express the same
  meaning in roughly two thirds the whitespace tokens, so the unscaled English
  bar skipped cleanup for essentially every Indic dictation;
- Whisper itself reported low confidence (`skip_when_unconfident`);
- the app profile is verbatim;
- an output language is set but the transcript is not confident
  (`language_uncertain`) — nothing is translated and the raw words are pasted;
- cleanup is disabled outright.

An output language, when set, forces the call through the first heuristic: a
short, clean utterance still has to be translated.

### The language layer

Everything that varies by language lives in one table, `sway/languages.py`:
the ISO code, the name Whisper reports, both display names, the scripts, the
filler words, the token ratio, the register guidance, whether the language
needs the accurate STT model, and whether it shows in the bar's shortlist.
Adding a language is one row — the bar shortlist, the dashboard's selects, and
the filler heuristic all derive from it, and a test asserts they cannot drift
apart again. They previously did: the dashboard kept its own list, and Marathi
was in neither.

Three decisions come off that table:

1. **Which Whisper.** `whisper-large-v3-turbo` by default; the full
   `whisper-large-v3` for languages the distilled decoder handles badly. Turbo
   decodes Marathi through a Hindi prior — wrong label, corrupted text (`आहे`
   returned as `है`) — which no downstream stage can repair. Measured at
   +140 ms median, paid only by those languages. Chunked streaming is also
   bypassed for them: fragmenting speech the decoder already mishandles makes
   it worse and the errors are then stitched together.
2. **Which language the transcript is in.** An explicit setting wins. Otherwise
   Whisper's label, *except* under ~2 s of speech where it is close to random
   (measured: "Hello" → hindi, a surname → korean) and the session's recent
   language is used instead. When the language is in any doubt, the cleanup
   call — which is being made anyway — returns a one-line `#lang:` header with
   its own read; that improves the *next* dictation, since the verdict arrives
   after this one is already decoded. A missing or malformed header degrades to
   no information, never to a wrong answer.
3. **How it should read.** `Language.register_hint` is what separates output a
   person would type from a textbook translation: keep the English words the
   speaker used in Latin script, prefer colloquial verb endings. Script is
   handled separately — by the translate directive when translating, and by a
   preservation directive when not, so romanized Hinglish stays romanized.

All of it is appended *after* the byte-identical style prefix, so Groq's prompt
caching still hits, and a caller that passes none of it gets exactly the prompt
this build has always sent.

A detected self-correction overrides the first two — those are exactly the cases they
get wrong — but never the last two.

Measured skip rate: **54%** of 26 labelled runs. Composition matters: 8 of 14 skips
were the verbatim profile (dictating into a terminal), and the *text-based* heuristic
fired only 5 times in 26.

### 4.6 Output guardrails

An instruction-following model is not a contract. Everything the system prompt asks
for is re-checked against the raw transcript, and any failure falls back to pasting
the raw transcript:

- **Length.** A rewrite that grew or shrank far beyond a tidy-up is rejected. Fired
  transforms adjust this: `may_shorten` for Summarize, `may_lengthen` for the email
  transform, so a correct output is not thrown away.
- **Entities.** Names, numbers, and dates present in the transcript must survive.
  Entities the correction scan says the speaker retracted are exempt.
- **Emptiness.** A rewrite that emptied itself is rejected.
- **Hallucination.** Repeated-phrase loops are detected and rejected.

The user always gets their own words; the worst case is that they get them untidied.

### 4.7 Injection

Save the clipboard → set ours → send Ctrl+V → restore after 150 ms. Never simulate
typing: keystroke simulation is unreliable across apps with input masking,
formatting, and IME handling, and every fast dictation tool pastes instead.

The restore is **skipped if the clipboard changed** in the meantime, so raising the
delay for slow Electron apps only ever costs a longer window of holding the dictated
text.

If the focused window is confidently running elevated, Windows' UIPI silently drops
synthetic input into it — so this is detected beforehand and the text is left on the
clipboard with an explicit "elevated window — copied instead" rather than the app
appearing to do nothing.

---

## 5. Activation model

A `Binding` is one physical gesture: a single held key (`ctrl_r`), two or more
modifiers held together with nothing else (`ctrl+cmd`), a modifier chord ending in a
key (`ctrl+alt+p`), a mouse button alone (`mouse:middle`), or a chord ending in a
mouse button (`ctrl+mouse:back`). Its shape — hold or tap — is **derived from that
shorthand**, not configured separately. That is what collapses the old "one primary
hold key plus one optional alternate chord" pair into a single list an action can
hold any number of bindings in.

`BindingListener` is the one OS-level hook pair (keyboard + mouse) that turns
physical events into two abstract signals per registered binding: **engaged** and
**disengaged**. What those mean is entirely up to the caller — dictation and voice
commands wire both for genuine hold-to-talk; hands-free and transforms use them
differently. `pynput >= 1.8` is required for the `injected` flag, backed by Windows'
own `LLKHF_INJECTED` bit, which lets the listener tell its own synthetic Ctrl+C and
Ctrl+V apart from a real keystroke without a blind window.

`keyboard` is forbidden: it needs administrator privileges on Windows.

---

## 6. Data model

All state is SQLite in `<data_dir>`, resolved by `PlatformAdapter.user_data_dir()` —
`%LOCALAPPDATA%\Sway` on Windows, `$XDG_DATA_HOME/sway` elsewhere.

```
<data_dir>/
  latency.db       stage timings and run records
  history.db       transcripts, plus the corrections table
  dictionary.db    personal vocabulary and learning candidates
  settings.json    the user-editable layer
  instance.json    live port + token for second-launch focus (deleted on quit)
  sway.log         rotating log
```

### 6.1 `latency.db`

```sql
CREATE TABLE runs (
    id          TEXT    PRIMARY KEY,
    started_at  REAL    NOT NULL,
    total_ms    REAL    NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT,
    metadata    TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE stage_timings (
    run_id      TEXT    NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,   -- start order; stable even when spans overlap
    stage       TEXT    NOT NULL,
    started_at  REAL    NOT NULL,   -- wall clock, for correlation only
    duration_ms REAL    NOT NULL,   -- monotonic, so an NTP step cannot distort it
    ok          INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;
```

`metadata` carries labels — `words`, `characters`, `audio_ms`, `speech_ms`,
`cleanup_skip_reason` — and **never transcript text**. This is an audited invariant,
not a convention.

### 6.2 `history.db`

```sql
CREATE TABLE history_entries (
    id              TEXT    PRIMARY KEY,
    created_at      REAL    NOT NULL,
    kind            TEXT    NOT NULL,   -- 'dictation' | 'command'
    text            TEXT    NOT NULL,   -- what was pasted
    raw_text        TEXT    NOT NULL,   -- the transcript it came from
    instruction     TEXT,               -- commands only
    focused_process TEXT,
    -- v2, all nullable with no default: an existing row reports "not available"
    -- rather than a fabricated zero
    run_id          TEXT,               -- joins to latency.db
    word_count      INTEGER,
    character_count INTEGER,
    audio_ms        REAL,
    speech_ms       REAL,
    wpm             REAL,
    time_saved_s    REAL,
    language        TEXT,
    status          TEXT
);

CREATE TABLE corrections (
    id         TEXT PRIMARY KEY,
    entry_id   TEXT REFERENCES history_entries(id) ON DELETE CASCADE,
    original   TEXT NOT NULL,
    corrected  TEXT NOT NULL,
    created_at REAL NOT NULL
);
```

Search is a plain `LIKE` scan, not FTS5: a personal history is at most a few thousand
rows, far below where FTS would matter, and it avoids depending on an SQLite build
option that is not guaranteed present everywhere.

### 6.3 `dictionary.db`

```sql
CREATE TABLE dictionary_entries (
    term         TEXT    PRIMARY KEY,
    added_at     REAL    NOT NULL,
    source       TEXT    NOT NULL DEFAULT 'manual',   -- manual | learned
    pinned       INTEGER NOT NULL DEFAULT 0,
    hits         INTEGER NOT NULL DEFAULT 0,
    last_used_at REAL
);

CREATE TABLE learning_candidates (
    term          TEXT    PRIMARY KEY,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL    NOT NULL,
    last_seen_at  REAL    NOT NULL
);
```

Every v2 column was added with a default, so existing rows migrate to "added by hand,
never used" — which is exactly what they were.

### 6.4 Migrations

Additive only. New columns are nullable-with-no-default (so a pre-existing row honestly
reports "not measured") or defaulted (so it migrates to the truth about itself).
Renames are forbidden. Every store checks its own version and applies its own
migration on open.

---

## 7. Configuration and settings

Two deliberately separate layers, because they serve different people.

**`sway.config`** — the engine's startup configuration. Frozen, read once, validated
strictly (unknown keys are a hard error). Precedence: environment (`SWAY_<SECTION>_<OPTION>`,
with the legacy `FLOW_*` prefix still accepted but logged as deprecated) → `config.toml`
→ defaults. A developer editing TOML expects it to win.

**`sway.settings`** — the surface a normal user edits from the UI, persisted to
`settings.json`. Different rules:

- **Never bricks startup.** A corrupt or hand-mangled file falls back to defaults and
  logs. Every value is validated individually, and an out-of-range one is replaced on
  its own rather than discarding the whole file — losing one bad field is
  recoverable, losing a user's entire transform library because a boolean was
  misspelled is not.
- **Safe to write while running.** Writes go to a temp file in the same directory and
  are atomically replaced, so an interrupted write leaves the previous file intact.
- **Round-trips unknown keys.** A file written by a newer build keeps the fields an
  older build does not understand.

`apply_to_config()` is the single place the overlay happens. `settings.json` overrides
`config.toml` **only** for the fields listed in `Settings`; `None` consistently means
"not overridden".

### API key resolution

1. **Windows Credential Manager** via `keyring` — where a normal user's key ends up.
2. **`SWAY_GROQ_API_KEY`** (legacy `FLOW_GROQ_API_KEY` still accepted) — an explicit developer override. Deliberately *not* the
   bare `GROQ_API_KEY`, so a key exported for another tool in the same shell cannot
   silently outrank the configured one.
3. **`GROQ_API_KEY`** from the environment or `.env`.

A key found only at level 3 is **copied up** into the keyring on first run and `.env`
is left exactly as it was. `keyring` is an optional import: if it is missing or the
platform has no usable backend, every function degrades to "the keyring holds
nothing" and the environment path keeps working. The app must never fail to start
because a credential *store* is unavailable.

The key is wrapped in a `Secret` that refuses to render itself in logs, reprs, or
tracebacks.

---

## 8. The dashboard server

Full route contract: [API Reference](api-reference.md).

Built on stdlib `http.server.ThreadingHTTPServer`. A framework would add a dependency
and a startup cost to serve one user on loopback, and `CLAUDE.md` rules out FastAPI
and any public listener outright.

Three things make it safe inside a process that can paste into whatever window has
focus:

1. **Loopback only.** Binds `127.0.0.1` on an **ephemeral port**, so nothing off the
   machine can reach it.
2. **A per-launch token.** Every request must carry the token minted at startup.
   Without it, any web page the user visits could script requests at `localhost` and
   read their entire dictation history.
3. **Origin checks on writes.** State-changing routes reject cross-origin requests
   outright, closing the same hole from the other side.

The token is taken from the URL once by `app.js` and immediately scrubbed from the
address bar, so a screenshot never captures it. It is never logged and never rendered.

Every string that came from the user, from a transcript, or from Groq is escaped
before it reaches `innerHTML`. Transcripts are arbitrary text the app pastes into
other applications; treating them as markup would be a self-inflicted XSS in a window
that can also read the user's history.

### Second-launch focus

The running instance owns a named mutex *and* this server. It writes the port and
token to `instance.json`. A second launch that fails to take the mutex reads that
file and POSTs `/api/focus`, then exits 0. No named pipe is invented for this.

`instance.json` holds a **live credential**, so: created with an owner-only mode
(honestly noted as close to a no-op against real Windows ACLs — the directory's
inherited ACL is what protects it), rewritten never appended, deleted on clean
shutdown, and **a stale file is treated as absent** — the recorded PID must be alive
before the port is trusted, because by then it could belong to an unrelated process.
A hung first instance must not hang the second: a ~1 s connect timeout, then an
explicit message, never a silent kill and never a fall-through to a rival hotkey hook.

---

## 9. Error handling

| Condition | Behaviour |
|---|---|
| Transient network error, 5xx | One retry with full-jitter backoff |
| **429** | **Never retried.** The specific limit hit is read from `x-ratelimit-*` headers and surfaced. The free tier's 30 req/min is too small to burn on blind retries. |
| 401 / invalid key | No retry. Explicit message; onboarding path offered. |
| Timeout | Request cancelled, run recorded as failed, bar shows error |
| No speech detected | Dropped before encode. No upload, no quota spent. |
| Cleanup output unsafe | Discarded; raw transcript pasted |
| Nothing selected (command/transform) | Bar reports it; nothing is sent to Groq |
| Elevated foreground window | Text left on the clipboard with an explicit message |
| Tray backend unavailable | Logged; app keeps running without an icon |
| WebView2 / pywebview absent | Falls back to the default browser |
| Corrupt `settings.json` | Per-field fallback to defaults; app starts |
| Latency queue full | Records dropped, counted, and logged. Measurement never stalls the pipeline. |
| Any tracker failure | Logged and swallowed. Measurement never takes the app down. |
| Uncaught exception | Traceback logged, native notification, clean shutdown — never a zombie hotkey hook with the mic hot |

A failed run is still recorded (so `sway report` shows it) but is excluded from timing
statistics.

---

## 10. Latency budget

Measured, not assumed. Source: [`d0-audit.md`](d0-audit.md), 75 historical runs.

```
Stage        Count    p50    p90     p99   mean     max
capture         69    2.8    9.3    36.3    4.0    36.3
vad             69   10.1   19.2    51.3   10.3    51.3
encode          68   31.8   44.7    71.5   27.9    71.5
transcribe      68  404.9  557.4   585.7  392.2   585.7
inject          68   17.9   26.5    40.4   17.9    40.4
TOTAL           69  622.6  885.2  1479.0  599.8  1479.0

cleanup (real model calls only, n=32)   p50 238.0   p90 728.5   max 1118.1
```

Three conclusions that shape the work:

**`transcribe` is a floor, not a tail.** p50 405 ms, p99 585 ms, max 585 ms — an
unusually tight distribution. Groq is being consistent. STT cannot be made less
variable; it can only be made *lower*, and only by changing model. That makes the
three-model benchmark the only lever on the largest single stage, and it moves the
median rather than the tail.

**Cleanup's spread is jitter, not length.** On real model calls, input length versus
duration gives r = +0.385, **r² = 0.149** — length explains about 15% of the variance.
The slowest call (1118 ms) had 260 input characters; an 888-character input finished
in 625 ms. Since the delay is largely *before* the first token, incremental injection
would hide it rather than remove it — which argues against incremental injection on
cost/benefit grounds independently of its interaction problems.

**The tail is the problem and cleanup owns it.** p50 623 ms is inside the 1000 ms
budget; p99 1479 ms is not. The indicated fix is a whole-pipeline timeout budget with
raw-transcript fallback, plus a tighter skip heuristic — a skip removes the whole
238 ms span rather than concealing it.

---

## 11. Packaging

PyInstaller **onedir** (`packaging/sway.spec`) into `dist/`, with two executables
sharing one COLLECT: a windowed `Sway.exe` (`console=False`) and a console
`sway-cli.exe`.

onedir is deliberate. onefile self-extracts to `%TEMP%` on **every** launch and pays
that cost each time; onedir pays first-touch disk once. Measured: source
`sway config` 376/293/279 ms; exe `sway-cli config` 1249/298/379 ms — the 1249 ms is
first-touch disk on a 92 MB bundle, and warm runs match source.

`scripts/build_app.py` is the one-command path: run PyInstaller, place shortcuts,
delete `build\`. Deleting `build\` is not tidiness — it contains a `Sway.exe` that is
only a bootloader stub, which fails with *"Failed to load Python DLL python311.dll"*
because the `_internal\` runtime it needs was written to `dist\`. It sits one folder
away and looks identical in Explorer.

**Known source-vs-exe divergences**, measured rather than assumed:

1. `sys.stdout` and `sys.stderr` are `None` in the windowed build. `configure_logging`
   skips the console handler when `sys.stderr is None`; every path must survive this,
   including third-party output (pystray and PortAudio both write to stderr on some
   failures).
2. `.env` discovery differs by launch method. From a foreign cwd the exe finds it via
   `bundle_dir()` and source does not — correct and deliberate, so Start-menu launches
   work, but it means a source-run reproduction of an exe bug can silently differ.
3. `scripts/build_app.py` copies `.env` next to the executable so the built app works
   standalone. `dist/` is gitignored so it never reaches the repo, but shipping a zip
   would ship the key. The keyring migration is the real fix.
4. Bundle 92 MB: `numpy.libs` 21 MB, `PIL` 13 MB, `libcrypto` 7.7 MB, `python311.dll`
   5.6 MB, Tcl/Tk ~7.8 MB. numpy is used in exactly one place — viewing raw PCM as
   int16 before FLAC encoding — and is a candidate for `array`/`memoryview`.

---

## 12. Platform isolation

Every OS-specific call sits behind one `PlatformAdapter` Protocol:

```
user_data_dir            user_config_dir
clipboard_get            clipboard_set
send_paste               send_copy
focused_process_name     synthetic_input_likely_blocked
show_message             open_url
acquire_single_instance_lock   process_is_running
set_launch_at_login      launches_at_login
animations_enabled       modifier_keys_down
```

Windows is implemented for real, via `ctypes` into `user32`/`kernel32` — no clipboard
dependency, no `pywin32`. It also enables per-monitor DPI awareness at import time,
*before* any window is created; without it Windows bitmap-scales the whole window on
any non-100% display and blurs exactly the anti-aliasing the bar exists to produce.

The POSIX adapter is best-effort and untested — `focused_process_name()` returns
`None`, so profile selection falls back to the default. It exists so macOS/Linux can
be added as a second implementation without touching the rest of the app.

---

## 13. Dependencies

Every one is justified in a comment in `pyproject.toml` before it is added.

| Package | Why it cannot be stdlib |
|---|---|
| `sounddevice` | The only maintained, wheel-distributed way to open a persistent callback-based input stream on Windows without a C compiler. `RawInputStream` hands the callback a raw buffer, which is what an allocation-free ring buffer needs. |
| `numpy` | Views raw PCM as int16 before FLAC encoding. Already transitive via `soundfile`; declared because it is imported directly. **21 MB of the bundle for one use — a removal candidate.** |
| `webrtcvad-wheels` | WebRTC's VAD. The original package has no prebuilt wheels for recent Python/Windows. |
| `soundfile` | libsndfile bindings — the only realistic way to write a real FLAC file without shelling out to ffmpeg. |
| `pynput` | Global hooks and synthetic Ctrl+V without administrator privileges. `>=1.8` for the `injected` flag. |
| `httpx[http2]` | The persistent HTTP/2 client `CLAUDE.md` specifies by name. |
| `pystray` | One tray API across Windows/macOS/Linux, matching the platform-isolation goal rather than hand-rolling `Shell_NotifyIcon`. |
| `pillow` | pystray's only supported icon path, and the bar's compositor — Tk's canvas primitives have no anti-aliasing at all. |
| `keyring` | No stdlib access to the Windows vault. ~2 MB. Degrades gracefully. |
| `pywebview` *(optional)* | A real OS window over WebView2. ~7 MB with pythonnet/clr_loader/bottle. Soft dependency: absence costs chrome, not function. |
| `pytest` *(dev)*, `pyinstaller` *(build)* | Test runner; release builds only. |

---

## 14. Testing

Full strategy: [Test Plan](test-plan.md). The rule from `CLAUDE.md`: **do not test the
audio callback** — test the transcript-processing pure functions instead.

1391 tests across 46 files. The pure modules (`cleanup`, `corrections`, `triggers`,
`transforms`, `profiles`, `analytics`, `diff`, `streaming`, `motion`, `orb`, the ring
buffer, the VAD trimmer, the FLAC encoder) are directly unit-tested with no I/O, no
network, and no Tk. Stores and the tracker are tested against real SQLite. Injection
and selection are tested against a fake platform adapter. The orb is verified against
frozen golden vectors published upstream, so the port is checked as numbers rather
than as an impression.

---

## 15. Extension points

- **A new OS** — implement `PlatformAdapter`. The macOS constraint to plan for is
  that `pystray` needs the main thread there, which currently belongs to the webview.
- **A new transform type** — `Transform` is a declarative record with output
  expectations; adding a behaviour must never require touching the pipeline.
- **A new pipeline stage** — add a `Stage` member in pipeline order. Additive only;
  never rename a value.
- **A new dashboard panel** — a read goes in `queries.py`, a write in `actions.py`,
  and the route in `server.py`. Reads open their own connection per request.
- **A new model** — `[cleanup].model` and `[commands].model` are configuration, not
  constants. This has already been exercised once, when `llama-3.1-8b-instant` was
  retired and began 404ing.
