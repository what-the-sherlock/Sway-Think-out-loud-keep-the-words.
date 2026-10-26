# Sway — Roadmap and Phase Status

**Verified against:** `1e85b9f`, 2026-08-26.

---

## Read this first: two numbering schemes

They are **not** the same, and conflating them causes real confusion.

- **`CLAUDE.md` Phases 0–6** describe how the *engine* was built. All shipped. History.
- **`PRODUCTION_BRIEF.md` phases D0–D8** ("D" for desktop) are the productisation
  work: turning a prototype `.exe` into an app.

Every reference below is qualified. A bare phase number is a bug.

---

## Engine phases (CLAUDE.md 0–6) — all shipped

| Phase | What it delivered | Evidence |
|---|---|---|
| **0** | Project skeleton, config loading, `LatencyTracker` + SQLite + console table. The measurement harness *before* the feature. | `sway demo` runs a stub pipeline end-to-end and prints a timing table |
| **1** | Raw dictation: hotkey → ring buffer → VAD trim → FLAC → `whisper-large-v3-turbo` → paste. Always-on-top status pill. | Median under target, proven by the Phase 0 tracker |
| **2** | Cleanup layer: filler removal, self-correction handling, grammar and punctuation, streamed, with the skip heuristic and logged skip rate. | 54% skip rate measured |
| **3** | Context awareness: focused-window detection, per-app profiles (casual / formal / verbatim), personal dictionary biasing Whisper's `prompt`. | `profiles.py`, `dictionary.py` |
| **4** | Chunked streaming with boundary dedup and graceful fallback. | Shipped, **defaults off** on measurement — see [ADR-006](decision-log.md) |
| **5** | Voice commands over a selection, searchable history, tray menu, PyInstaller packaging. | `commands.py`, `history_window.py`, `tray.py`, `packaging/` |
| **6** | The dashboard: real-data-only metrics across Home, History, Dictionary, Snippets, Transforms, Insights, Settings, About. | `dashboard/`, 8 pages |

---

## Desktop phases (PRODUCTION_BRIEF D0–D8)

| Phase | Scope | Status |
|---|---|---|
| **D0 — Audit and baseline** | Architecture map, additive stage extensions with a migration note, a 20-clip baseline captured *against the new stage set*, a no-audio/no-transcript-persisted audit, and a measured source-vs-exe divergence list | **Complete** — [`d0-audit.md`](d0-audit.md) |
| **D1 — Lifecycle and shell** | No console, no browser; tray menu with state-tracking icon; second-launch focus over the existing socket; clean shutdown; crash handler; launch-at-login | **Complete** — commit `3aaccc3` |
| **D2 — Native-feeling UI** | `pywebview` window over WebView2 with a browser fallback; the Sway Bar replacing the pill; non-activating and click-through; app icon and taskbar identity | **Complete** — `window.py`, `bar.py`, `orb.py` |
| **D3 — Latency** | Per-item before/after against the D0 baseline, reverting what did not pay | **Partial** — streaming benchmarked and defaulted off; **the three-model STT benchmark and the cleanup-model benchmark are not done**, and they are the only lever on the largest stage |
| **D4 — Transcription quality** | Dictionary in Settings, correction map, app label to the cleanup pass, `temperature=0`, silence-hallucination drop, repeated-phrase detection, versioned prompt file, self-correction, eval harness | **Partial** — everything shipped except the prompt living in a **versioned file** rather than a module constant, and a general cleanup eval set (corrections have one) |
| **D5 — Controls and settings** | Keyring credentials with explicit resolution order, "Test key", onboarding, agent on/off, full settings pane, atomic `settings.json` | **Complete** — `credentials.py`, `settings.py`, onboarding in `app.js` |
| **D6 — Dashboard** | Real-data-only panels, latency breakdown, grouped errors, searchable history, delete-all, live updates | **Mostly complete** — **Groq free-tier usage from response headers is not surfaced**, and live updates are polling rather than push |
| **D7 — Reliability and packaging** | Per-failure-mode recovery, never lose a recording, whole-pipeline timeout budget, onedir zip/installer, SmartScreen documentation, documented uninstall, README refresh | **Partial** — packaging shipped; **the retry queue, the timeout budget, and the documented uninstall do not exist**, and the README still describes the pre-rename `flow` package |
| **D8 — Voice command triggers** | Declarative action model, local pre-LLM detection, expand-only-what-fired resolution, instruction hierarchy, trigger manager UI, test bench, measurement | **Partial** — transforms and snippets ship with local matching, the one-call rule, and a UI; **the test bench, the literal escape hatch, per-request token measurement, and the false-positive counter do not exist**; `mode` and `deterministic` types beyond date/macro are not implemented |

---

## What to do next, in order

Ranked by evidence, not by appetite. The first two come straight from the measured
baseline; the third and fourth are the standing risks.

### 1. Close the latency tail (D3 + D7)

**The number:** p50 623 ms is inside budget. **p99 1479 ms is not**, and cleanup owns
it.

Two changes, in this order:

- **A whole-pipeline timeout budget with raw-transcript fallback** (FR-8.7). On
  exceed, paste the raw transcript rather than nothing. This converts the worst case
  from "1.5 seconds of waiting" into "fast, slightly untidy" — which is the right
  trade for a tool whose entire premise is speed.
- **The three-model STT benchmark** (FR-3.8). `transcribe` is a floor, not a tail:
  p50 405 ms with a max of 585 ms. It cannot be made less variable, only lower, and
  only by changing model. This moves the median for every dictation.

Explicitly **not** incremental injection — see [ADR-012](decision-log.md); the
evidence is against it.

### 2. Tighten the skip heuristic (D3)

Of 26 labelled runs, 14 skipped — but 8 of those were the verbatim app profile, not
the text heuristic, which fired only 5 times in 26. **Whether the other 12 model calls
were earning their keep is the open question**, and it is the cheaper lever: a skip
removes the whole 238 ms span rather than concealing it. Needs the eval set from item
4 to answer without guessing.

### 3. Never lose a recording (D7 / FR-8.6)

The one remaining way the app can lose a user's words outright. The lifecycle is
already specified in full (bounded FLAC blobs in `<data_dir>/queue/`, never in a
database, never in `%TEMP%`, deleted on successful retry, wipeable from "delete all")
because it is the single exception to the no-audio-persisted rule and has to be
exactly right.

### 4. Make quality measurable (D4)

Move the cleanup prompt into a **versioned file** and build the fixed eval set with
expected outputs, reporting WER and latency per config. Without this, every prompt and
model change is a vibe — including the answer to item 2. Extend it with the trigger
negative cases: a phrase appearing as ordinary speech must be *measured* not to fire,
not merely structurally prevented.

### 5. Startup health check (ADR-014)

`llama-3.1-8b-instant` was retired, began 404ing, and took the whole cleanup stage
down. The user found out by dictating. One cheap call at startup would have caught it
first.

### 6. Free-tier usage from response headers (D6 / FR-9.10)

Read the limit and remaining values from `x-ratelimit-*` per request and store the
latest. The published 30/min and 2000/day figures are a **labelled display fallback
only**, for before the first request of a session — never computed against when
headers are available, because free-tier numbers change.

### 7. Documentation debt

`README.md` still describes the package as `flow`, the status as Phase 5, and the
cleanup model as `llama-3.1-8b-instant`. All three are wrong. Anyone building from
source is reading it first.

---

## Deferred, with reasons

| Item | Why not now |
|---|---|
| **macOS / Linux build** | Every OS-specific call is already behind `PlatformAdapter`, so this is work rather than a rewrite. The constraint to plan for: `pystray` needs the main thread on macOS, which currently belongs to the webview. |
| **VAD endpointing** (FR-2.10) | Collides with trigger detection in a way that needs the grace window from [ADR-015](decision-log.md) built first. Hold-to-talk stays the default contract regardless. |
| **Incremental injection** | Measured against; see [ADR-012](decision-log.md). |
| **Aho–Corasick trigger matching** | The regex alternation is fast enough at tens of triggers. Needs a measurement proving it is the bottleneck, plus its bundle cost, before adding a C extension. |
| **Local models** | Out of scope for the current phases. A post-1.0 conversation, not a backlog item. |
| **Dropping numpy** | 21 MB of a 92 MB bundle for a single int16 view in the FLAC encoder. Real, but it buys disk, not latency, so it sits below everything above. |
| **Code signing** | No certificate is assumed. SmartScreen behaviour for an unsigned binary has to be *documented* rather than solved. |
