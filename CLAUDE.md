# Project: Flow — local voice dictation with LLM cleanup

## What this is
A desktop app that runs in the background. The user holds a hotkey, speaks,
releases. Within ~1 second, cleaned-up text appears at their cursor in
whatever application currently has focus. Wispr Flow is the benchmark for
feel and latency.

## Hard constraints — do not violate these
- Python 3.11+. No Docker. No containers. No server component.
- Groq API only (key in .env as GROQ_API_KEY). No OpenAI, no local models
  in Phases 0-4.
- Everything must stay inside Groq's free tier: 30 req/min org-wide,
  2000 audio requests/day, ~14,400 req/day on llama-3.1-8b-instant.
- Target: <1000ms p50 from hotkey release to text pasted.
- Minimal dependencies. Justify every new package in a comment before
  adding it. Prefer stdlib.
- Target OS: Windows. Isolate all OS-specific code behind a
  `platform/` interface so macOS/Linux can be added later.

## Architecture rules
- Audio input stream opens ONCE at app startup and stays open forever,
  writing into a ring buffer. Never open the mic on hotkey press.
- One persistent `httpx.Client` with http2=True, created at startup,
  pre-warmed with a HEAD request to api.groq.com. Never create a client
  per request.
- The audio callback thread must never block, allocate large buffers,
  or do I/O. It only writes to the ring buffer.
- Network work runs on a single asyncio loop in a dedicated thread.
- Text injection: save the existing clipboard, set ours, send Ctrl+V,
  restore the original clipboard after 150ms. Never simulate typing.
- Every stage boundary emits a timing event to a stage-latency logger.

## Build phases — implement ONE phase per session. Stop at the end of
## each phase and wait for me to test before starting the next.

### Phase 0 — skeleton and measurement
Project structure, config loading, and a `LatencyTracker` that records
per-stage timings and writes them to SQLite plus a formatted console
table. Build the measurement harness BEFORE the feature. A stub pipeline
with fake stages must run end-to-end and print a timing table.

### Phase 1 — raw dictation loop
Global hotkey (hold Right Ctrl) → ring buffer capture → WebRTC VAD trim →
FLAC encode 16kHz mono → Groq whisper-large-v3-turbo → paste raw text.
No LLM yet. A minimal always-on-top pill overlay showing idle /
recording / thinking. Success: median under 900ms on a 5-second clip,
proven by the Phase 0 tracker.

### Phase 2 — cleanup layer
Add llama-3.1-8b-instant. System prompt removes filler words and
self-corrections, fixes grammar and punctuation, and NEVER answers the
content or adds commentary — it only rewrites. Stream the response.
Implement the skip heuristic: bypass the LLM when the transcript is
under 8 words and contains no filler markers. Log skip rate.

### Phase 3 — context awareness
Detect the focused window and process name. Per-app profiles: casual for
chat apps, formal for mail clients, verbatim/no-cleanup for terminals and
code editors. Personal dictionary in SQLite, injected into Whisper's
`prompt` parameter to bias proper nouns and jargon.

### Phase 4 — chunked streaming
Ship 5-second overlapping audio chunks while the user is still speaking,
so key release only transcribes the tail. Handle chunk-boundary
deduplication carefully. Must degrade gracefully to Phase 1 behavior on
network failure.

### Phase 5 — commands and history
Voice commands over selected text ("make this bullet points"). Searchable
history window. Tray menu. PyInstaller packaging.

## Error handling
Every network path needs a timeout, one retry with jitter, and a
user-visible failure state on the overlay. On 429, surface which limit
was hit by reading the response headers. The app must never hang with
the mic hot.

## What NOT to do
- Do not add a web UI, FastAPI, or any localhost server.
- Do not add a database beyond SQLite.
- Do not add features from later phases early.
- Do not use `keyboard` (needs admin on Windows) — use pynput.
- Do not write tests for the audio callback; write them for the
  transcript-processing pure functions instead.