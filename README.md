# Flow

Local voice dictation with LLM cleanup. Hold a hotkey, speak, release — cleaned-up
text appears at your cursor in whatever application has focus, in under a second.

**Status: Phase 5 (commands and history).** Hold Right Ctrl, speak, release: while
you're still talking, overlapping 5-second windows of audio are transcribed
in the background, so release only has to transcribe the short tail. The
pieces are stitched into one deduplicated transcript, transcribed by Groq
Whisper (biased toward your personal dictionary), cleaned up by
`llama-3.1-8b-instant` in a style chosen from the focused app — casual for
chat apps, formal for mail clients, untouched for terminals and code editors
— and pasted at your cursor. Short, clean transcripts skip the cleanup call
entirely; any failure in the streaming path falls back to a single whole-clip
request (Phase 1 behavior), so a network hiccup never loses a word.

Select text anywhere and hold Right Shift instead to run a voice command —
say "make this bullet points" or "fix the grammar" and the selection is
replaced by the instruction applied to it. Every successful dictation and
command is recorded to a searchable history, browsable from the tray icon's
"Show History" window or `flow history list` in a terminal.

## Requirements

- Python 3.11 or newer
- Windows (the only implemented `platform/` adapter; see Architecture)
- A `GROQ_API_KEY` — free at [console.groq.com/keys](https://console.groq.com/keys)
- A microphone. Run `flow devices` to list what's available.

## Setup

```powershell
uv venv --python 3.11
uv pip install -e ".[dev]"
```

Or without `uv`:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Then:

```powershell
copy .env.example .env
notepad .env              # fill in GROQ_API_KEY
copy config.example.toml config.toml   # optional; defaults work as-is
```

`.env` and `config.toml` are both git-ignored.

## Usage

```powershell
python -m flow run              # the app: hold Right Ctrl, speak, release
python -m flow devices          # list microphones, for [audio].device
python -m flow dictionary add "GraphQL" "Aashutosh"   # bias Whisper toward these terms
python -m flow dictionary list
python -m flow dictionary remove "GraphQL"
python -m flow history list                  # recent dictations/commands
python -m flow history list --search "milk"  # substring search, across text/instruction
python -m flow history list --kind command
python -m flow demo             # Phase 0: run 20 fake dictations, print a latency table
python -m flow report           # summarise recorded dictation history
python -m flow config           # show resolved configuration (secrets redacted)
```

`flow run` opens the microphone once at startup and keeps it open for the whole
session — never on hotkey press. A small always-on-top status pill (see
`flow.branding`/`flow.overlay`) floats near the top of the screen: a circular
badge carrying the app mark, ringed and badged in a state-specific color and
glyph (a pulsing dot while listening, a spinner while thinking, a warning
triangle on error) rather than a plain colored shape, so the state reads at a
glance. A tray icon (same mark) offers "Show History" and "Quit". Ctrl+C at
the console (or Quit from the tray) shuts everything down cleanly (mic, both
hotkey listeners, network client, overlay, tray icon).

## The dictation pipeline

```
hold Right Ctrl
  → while still speaking: overlapping 5s chunks → VAD trim → FLAC → Groq Whisper
    (background, once every 4s of new audio; doesn't block continued recording)
  → release Right Ctrl → tail capture (since the last chunk) → VAD trim → FLAC
  → Groq whisper-large-v3-turbo on just the tail (biased by the personal dictionary)
  → stitch chunk transcripts + tail into one deduplicated transcript
  → Groq llama-3.1-8b-instant cleanup (styled per focused app)
  → paste at cursor
```

If the hold was shorter than one chunk, or any chunk (or the tail) fails for any
reason, this transparently falls back to a single whole-clip request — exactly
Phase 1's pipeline — so a network hiccup during streaming never loses a word.

Every stage boundary is measured by the Phase 0 `LatencyTracker` — `flow report`
after a session of real dictation shows exactly where the time goes:

```
Stage       Count    p50    p90    p99   mean    max
----------------------------------------------------
capture        12    0.6    0.9    1.1    0.7    1.1
vad            12    3.2    4.8    5.1    3.4    5.1
encode         12    6.5    9.1    9.6    6.9    9.6
transcribe     12  180.4  240.3  260.1  190.7  260.1
merge          12    0.1    0.2    0.2    0.1    0.2
cleanup        12  180.2  240.5  260.0  190.3  260.0
inject         12   14.2   18.9   19.3   14.9   19.3
----------------------------------------------------
TOTAL          12  385.2  510.1  550.4  406.0  550.4
```
(Illustrative — actual numbers depend on clip length, network, and hardware. With
streaming, `capture`/`vad`/`encode`/`transcribe` cover only the short tail, so
these are typically much faster than Phase 1's whole-clip numbers for anything
longer than one chunk. The target is a p50 under 1000ms end to end.)

Each stage in one sentence:

- **capture** — drain the frames written since the relevant start point (the
  hotkey press for a single-shot request, or the last chunk boundary for a
  streamed tail) out of the ring buffer.
- **vad** — WebRTC VAD trims leading/trailing silence; a clip with no detected
  speech is dropped here for free (no encode, no upload, no Groq quota spent).
- **encode** — FLAC (lossless, several times smaller than raw PCM for the upload).
- **transcribe** — Groq `whisper-large-v3-turbo`, one retry with jittered backoff
  on network errors and 5xx; 429s are surfaced immediately with whichever specific
  limit was hit (read from `x-ratelimit-remaining-*` headers), not retried — the
  free tier's 30 req/min is too small to burn on blind retries. The `prompt`
  parameter is seeded from the personal dictionary (`flow dictionary`) to bias
  transcription toward proper nouns and jargon Whisper would otherwise mishear.
  On the release path this is just the tail; the background chunk requests during
  the hold use the same client and prompt bias but aren't part of this run's
  release-to-paste measurement (see `[streaming]` below).
- **merge** (Phase 4) — `flow.streaming.stitch_transcripts` dedups the chunk
  transcripts collected while you were still speaking against the tail: for each
  boundary it finds the longest matching run of words (case/punctuation-insensitive)
  between what's already merged and the next piece, and drops the duplicate.
  Skipped entirely (and absent from the table) when streaming didn't run — a short
  hold, `[streaming].enabled = false`, or a fallback to the single-shot path.
- **cleanup** — Groq `llama-3.1-8b-instant` rewrites the transcript: filler words
  and self-corrections removed, grammar and punctuation fixed, meaning preserved
  exactly, never answered or commented on. Which system prompt it uses depends on
  the focused window at the moment the hotkey was pressed (`flow.profiles`):
  casual for chat apps, a more formal/professional rewrite for mail clients, and
  no cleanup call at all for terminals and code editors, where the transcript
  must reach the cursor exactly as spoken. Transcripts under 8 words with no
  filler markers also skip this call (see `[cleanup]` below); the filler check
  is language-aware, using Whisper's own detected language (`flow.groq_client`
  requests `verbose_json` specifically to get this) to pick an appropriate
  word list rather than always checking for English fillers. The LLM's output
  is also checked by `flow.cleanup.is_cleanup_output_safe` -- a rewrite that
  grew, shrank, or emptied itself far more than a tidy-up ever should is
  discarded in favor of the raw transcript, a safety net for the cases where
  an 8B model doesn't follow the "rewrite, don't respond" instruction. Same
  retry/rate-limit handling as transcription, over the same persistent client.
- **inject** — save the clipboard, set the cleaned-up text, send Ctrl+V, restore
  the original clipboard 150ms later. Never simulates typing. If the focused
  window is confidently running elevated (higher privilege than Flow itself),
  Windows' UIPI silently drops synthetic input into it, so `flow.inject`
  detects this beforehand and leaves the cleaned-up text on the clipboard
  instead of pasting -- the overlay shows "elevated window — copied instead"
  rather than silently doing nothing.

A failed run is recorded (so `flow report` still shows it) but excluded from the
timing statistics, and the overlay shows a short-lived error state instead of
hanging with the mic hot.

## Voice commands, history, and the tray icon (Phase 5)

**Voice commands** (`flow.commands`, `flow.selection`) apply a spoken instruction
to whatever text is selected in the focused app, in place:

```
select text → hold Right Shift → speak an instruction → release
  → Ctrl+C round trip reads the selection (captured at press, in the background,
    same "hand off immediately" principle as the dictation hotkey)
  → the instruction is transcribed (Phase 1's single-shot pipeline -- commands
    are short, so chunked streaming doesn't apply)
  → Groq llama-3.1-8b-instant applies the instruction to the selection
    (flow.commands.COMMAND_SYSTEM_PROMPT: output only the transformed text)
  → paste at cursor, replacing the selection
```

Right Shift, not Ctrl/Alt, by default (`[hotkey].command_key`): those are common
menu/accelerator modifiers in many apps, and holding one while speaking risks
losing the selection before Ctrl+C ever runs. Set `command_key = ""` to disable
commands entirely. If nothing was selected, or the Ctrl+C round trip times out
(`[commands].selection_timeout_s`), the overlay flashes an error and nothing is
sent to Groq.

**History** (`flow.history.HistoryStore`) records every successful dictation and
command — what was pasted, the raw transcript/selection it came from, and (for
commands) the spoken instruction — to `<data_dir>/history.db`, pruned at startup
past `[history].retention_days`. Search is a plain `LIKE` scan rather than
SQLite's FTS5 extension: a personal history is at most a few thousand rows, far
below where that would matter, and it sidesteps depending on an SQLite build
option that isn't guaranteed present everywhere. `[history].enabled = false`
turns off recording (and the history window has nothing to show) if you'd rather
nothing was kept.

**The searchable history window** (`flow.history_window.HistoryWindow`) is a Tk
window — live search box, a list of matches, the full text of whichever one is
selected, Copy/Delete buttons — opened from the tray icon while `flow run` is
active. It's built lazily on the same Tk thread the status pill runs on
(`Overlay.run_on_ui_thread`, since Tk cannot be touched from another thread
safely), so it needs `[overlay].enabled = true`.

**The tray icon** (`flow.tray.TrayIcon`, `pystray` + `Pillow`) offers "Show
History" and "Quit". Best-effort by design: if the tray backend can't start (no
notification area, the optional dependency missing), Flow logs a warning and
keeps running without one rather than failing to start — `[tray].enabled = false`
skips it outright.

## Architecture

Threading model, straight from CLAUDE.md's constraints:

- **Audio**: `sounddevice.RawInputStream` opens once in `Engine.start()` and stays
  open for the process lifetime. Its callback only does a bounds-checked memcpy
  into `RingBuffer` — no I/O, no meaningful allocation, no logging on the steady
  path.
- **Hotkey**: `pynput.Listener` (not `keyboard` — that needs admin on Windows) runs
  its own thread — one for dictation, and (Phase 5) a second independent one for
  the command hotkey, so holding one never interferes with the other. Each
  `on_release` immediately hands its work off to a fresh worker thread so the
  listener stays free for the next key event instead of blocking through
  VAD/encode/network/paste (or, for a command, the Ctrl+C selection read).
- **Network**: one `asyncio` event loop in one dedicated thread
  (`NetworkLoop`). `GroqClient` wraps a single persistent `httpx.AsyncClient`
  with `http2=True`, created once and pre-warmed with a HEAD request at startup —
  never one client per request.
- **Streaming** (Phase 4, `flow.streaming.ChunkStreamer`): a background thread
  started on hotkey press and stopped on release. Every `chunk_seconds -
  overlap_seconds` of new audio it cuts an overlapping window out of the ring
  buffer, VAD-trims and FLAC-encodes it inline, and fires off a transcription
  request without blocking — so it never falls behind while you keep talking.
  A failed chunk request sets a flag that stops scheduling further chunks and
  tells `Engine` to fall back to a single whole-clip request; nothing about
  streaming ever risks losing audio, since the full clip is still sitting in
  the ring buffer regardless of what streaming did with it.
- **Paste**: `TextInjector` saves/sets/restores the clipboard via ctypes calls into
  `user32`/`kernel32` (no clipboard dependency) and sends Ctrl+V through pynput.
  The 150ms restore runs on a `threading.Timer`, never a blocking sleep.
- **Overlay**: `tkinter` (stdlib) window, but every frame it shows is a single
  image composited with Pillow (`flow.overlay._render_pill`) from the shared
  mark/palette in `flow.branding` -- rounded capsule, avatar ring, state
  badge, and label text all rendered at 4x and downsampled for anti-aliased
  edges, since Tk's own canvas primitives have none. On Windows the window is
  shaped via `-transparentcolor` (see `_KEY_RGB`) so only the pill itself is
  visible, not a rectangle around it; `flow.platform.windows` also enables
  per-monitor DPI awareness at import time (before this window is ever
  created), without which Windows silently bitmap-scales the whole window on
  any non-100%-scaled display and blurs exactly the anti-aliasing this exists
  to produce. `Engine` and the hotkey/worker threads push state through a
  queue; only the thread that calls `Overlay.run()` touches Tk widgets.
  `Overlay.run_on_ui_thread` (Phase 5) generalises this into a way for any
  other thread (the tray icon's) to run arbitrary code -- opening
  `HistoryWindow` -- on that same Tk thread.
- **Tray** (Phase 5, `flow.tray.TrayIcon`): `pystray`'s icon loop runs on its
  own background thread (supported on Windows; a future macOS adapter will
  need this coordinated with Tk's main-thread requirement, since pystray
  needs the main thread there too). Its icon is the same `flow.branding`
  badge the overlay and every window's title bar use. Menu clicks call back
  into `Engine`, which either asks the overlay to show the history window or
  calls `Overlay.stop()` to shut the whole app down, same as Ctrl+C at the
  console.

`flow.platform` isolates every OS-specific call (clipboard, paste, data
directories, focused-window detection) behind one `PlatformAdapter` interface.
Only Windows is implemented for real; the POSIX adapter is best-effort/untested
(`focused_process_name()` returns `None` there, so profile selection just falls
back to the default), ready for macOS/Linux to be added as a second
implementation later without touching the rest of the app.

Per-app profiles (`flow.profiles`) are chosen from the foreground window's
process name at the moment the hotkey is pressed (`GetForegroundWindow` +
`GetWindowThreadProcessId` + `QueryFullProcessImageNameW`, all via `ctypes`,
same no-new-dependency approach as the clipboard code) — a pure lookup from a
curated table of executable names to a `CleanupStyle`, easy to extend.

## Configuration

Highest precedence first: environment variables (`FLOW_<SECTION>_<OPTION>`) →
`config.toml` → defaults. Unknown keys are a hard error. See `config.example.toml`
for every option with its default and a one-line explanation.

Notable options:

- `[audio].device` — substring match against `flow devices` output; unset uses
  the system default input.
- `[hotkey].key` — defaults to `ctrl_r`; see `flow.hotkey.KNOWN_KEYS` for the full
  list of recognised names.
- `[groq].language` — an ISO-639-1 hint (e.g. `"en"`); unset lets Whisper
  auto-detect. Either way, the detected language comes back on every
  transcription and feeds the cleanup skip heuristic's language-aware filler
  check (see `flow.cleanup._FILLER_WORDS_BY_LANGUAGE` for the covered list;
  an uncovered language just falls back to the language-agnostic checks
  rather than guessing).
- `[cleanup].enabled = false` — pastes the raw transcript, like Phase 1, in every app.
- `[cleanup].skip_min_words` — transcripts shorter than this with no filler
  markers skip the LLM call; `flow.cleanup.CleanupStats` logs the running skip
  rate at INFO every 10 dictations.
- `[dictionary].enabled = false` — skips opening the personal dictionary and
  sends no `prompt` bias to Whisper.
- `[dictionary].max_prompt_chars` — caps how much of the dictionary is sent
  per request; Whisper's own prompt window is roughly 224 tokens.
- `[streaming].enabled = false` — always uses a single whole-clip request, like
  Phase 1; no background chunk requests at all.
- `[streaming].chunk_seconds` / `overlap_seconds` — length of each background
  chunk and how much it shares with its neighbor (and the final tail) for
  boundary dedup. A longer dictation with streaming on costs roughly one extra
  Groq request per `chunk_seconds - overlap_seconds` of speech — worth knowing
  against the free tier's 30 req/min.
- `[hotkey].command_key` — hold with text selected to run a voice command;
  defaults to `shift_r`. `""` (or any falsy value) disables commands.
- `[commands].selection_timeout_s` — how long to wait for the Ctrl+C round trip
  before concluding nothing was selected.
- `[history].enabled = false` — stop recording dictations/commands entirely.
- `[history].retention_days` — entries older than this are pruned at startup;
  `0` disables pruning.
- `[tray].enabled = false` — don't start the tray icon at all.
- `[overlay].enabled = false` — runs headless (no Tk window, and no history
  window either, since it's built on the overlay's Tk thread), useful without
  a display attached.

`GROQ_API_KEY` comes from `.env` (or the real environment, which wins) and is
wrapped in a `Secret` that refuses to render itself in logs, reprs, or tracebacks.

## Layout

```
src/flow/
  cli.py              argparse entry point (run / devices / dictionary / history / demo / report / config)
  engine.py            wires capture -> VAD -> encode -> transcribe -> cleanup -> inject,
                        plus the Phase 5 command pipeline and history/tray wiring
  config.py            TOML + env + .env layering, Secret, strict validation
  hotkey.py             pynput listener + pure key-repeat debounce
  groq_client.py        persistent async HTTP/2 client, retry/backoff, rate limits
  cleanup.py             cleanup system prompts, skip heuristic, skip-rate stats (pure, tested)
  profiles.py             process name -> CleanupStyle lookup (pure, tested)
  dictionary.py            personal dictionary store + prompt-bias builder (pure builder, tested)
  streaming.py              chunk clock + transcript stitching (pure, tested) + ChunkStreamer worker
  commands.py               voice-command system prompt + message builder (pure, tested)
  selection.py               reads the current text selection via Ctrl+C (tested, fake platform)
  history.py                  dictation/command history store (tested, real SQLite)
  history_window.py            searchable Tk history window (untested UI wiring, like overlay.py)
  tray.py                      pystray tray icon + menu (untested UI wiring, like overlay.py)
  net_loop.py                   one asyncio loop, one dedicated thread
  inject.py                      clipboard save/set/paste/restore
  overlay.py                      status pill: idle / recording / thinking / error (untested UI wiring)
  branding.py                     shared mark + palette for the pill, tray icon, and window icons
  assets/logo_mask.png             pre-processed transparent mark (see scripts/build_logo_asset.py)
  audio/
    ring_buffer.py         allocation-free circular PCM buffer (pure, tested)
    capture.py              sounddevice wrapper (thin, untested per CLAUDE.md)
    vad.py                   WebRTC VAD silence trimming (pure, tested)
    flac.py                   PCM -> FLAC via soundfile (pure, tested)
  platform/                OS-specific code behind one interface, incl. focused-window
                            detection, DPI awareness, and (Phase 5) the copy shortcut
  latency/                  Phase 0's measurement harness
  pipeline/stub.py           Phase 0's fake pipeline
scripts/
  build_logo_asset.py       regenerates assets/logo_mask.png from the source artwork
  build_app_icon.py         regenerates packaging/flow.ico from flow.branding
packaging/
  flow.spec                 PyInstaller build spec -- see Packaging below
  flow.ico                  the built exe's icon (see scripts/build_app_icon.py)
  hooks/hook-webrtcvad.py    local override for a broken upstream PyInstaller hook
tests/                       pure functions, store/tracker, CLI, config, injection
```

## Packaging (Phase 5)

Freezes Flow into a self-contained Windows build that needs no Python install:

```powershell
uv pip install -e ".[build]"
python scripts/build_logo_asset.py   # only if the source artwork changed
python scripts/build_app_icon.py     # only if the mark/palette changed
pyinstaller packaging/flow.spec
```

That produces `dist\flow\`, the whole shippable unit — zip it and hand it to
someone as-is. It contains two executables sharing one `_internal\` (kept
tidy on purpose: this is the folder a non-technical recipient opens):

- **`Flow.exe`** — windowed, no console, the one to double-click. Bare
  invocation defaults to `run`; a missing `GROQ_API_KEY` or any other startup
  failure is reported through a native message dialog (`flow.gui`,
  `PlatformAdapter.show_message`) rather than a console line nobody sees. A
  Win32 mutex (`PlatformAdapter.acquire_single_instance_lock`) stops a second
  copy from starting — two live instances would mean two open microphones
  and every dictation typed twice — and shows its own dialog instead.
- **`flow-cli.exe`** — the console build, for `config`/`report`/`history`/
  `dictionary`/`devices`/`demo`, which need a real stdout.

`--onedir` (not `--onefile`) for both: a background dictation app should
start in well under a second, and onefile's self-extraction to a temp
directory on every launch works against exactly that.
`pyinstaller-hooks-contrib` (pulled in by the `build` extra) already knows
how to collect sounddevice's/soundfile's native libraries and pynput's/
pystray's per-OS backend modules; `packaging/flow.spec` only adds Flow's own
package and assets, the example config/env files, a Windows version
resource (so a shared build's file properties identify it), and a local
override for upstream's `webrtcvad` hook (it assumes a distribution name
that doesn't match `webrtcvad-wheels`, the fork this project actually
depends on — see `packaging/hooks/hook-webrtcvad.py`).

**Before sharing a build with someone else:** never bundle your own
`GROQ_API_KEY` into it — anyone with the files could read it back out, and
everyone using it would share your free-tier rate limit. Point recipients at
[console.groq.com/keys](https://console.groq.com/keys) for their own key
instead; `Flow.exe`'s first-run dialog walks them through where to put it.
An unsigned build will likely trip Windows SmartScreen on first launch —
expected for a small unsigned exe, not a sign anything is wrong.

## Tests

```powershell
pytest
```

Per CLAUDE.md, tests target the pure/transcript-processing functions — the ring
buffer, VAD trimming, FLAC round-trips, retry/backoff math, rate-limit header
parsing, key-repeat debouncing, clipboard injection against a fake platform,
chunk-boundary dedup, voice-command message building, selection reading against
a fake platform, the history store — not the audio callback or the pynput/Tk/
pystray listener wiring themselves.
#   S w a y - - - T h i n k - o u t - l o u d - k e e p - t h e - w o r d s .  
 