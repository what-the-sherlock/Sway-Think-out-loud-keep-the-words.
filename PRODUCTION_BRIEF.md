# Flow — prototype `.exe` → production desktop app

Companion to CLAUDE.md. CLAUDE.md's hard constraints and architecture rules
win any conflict with this document.

## Phase numbering — read this first

Two numbering schemes exist and they are **not** the same:

- **CLAUDE.md Phases 0–6** describe how the *engine* was built. Those are
  history; all of them are shipped. This document never renumbers them.
- **This document's phases are `D0`–`D8`** ("D" for desktop). They are the
  remaining work.

Every cross-reference in this file is qualified — `D3`, or
`CLAUDE.md Phase 3`, never a bare "Phase 3". Do the same in commits, code
comments, and questions back to me. If you find a bare phase number
anywhere in this file, that is a bug in this file; flag it.

## Role

Lead engineer on Flow, a Windows voice-dictation app (Wispr Flow clone). The
engine (CLAUDE.md Phases 0–5) works. The packaged build still behaves like a
dev build. Close that gap.

Work phase by phase. Do not start a phase until I confirm the previous one.
At the start of each phase restate the plan in 5 lines or fewer, then
implement.

## Repo as it actually is (verified 2026-08-20)

- Runtime: Python 3.11+, `.venv/`, hatchling, src layout (`src/flow/`).
- Packaging: PyInstaller **onedir** (`packaging/flow.spec`) into `dist/flow/`,
  with two exes sharing one COLLECT: `Flow.exe` (windowed, `console=False`,
  entry `packaging/entry_gui.py` -> `flow.gui:main_windowed`) and
  `flow-cli.exe` (console, `packaging/entry_cli.py` -> `flow.cli:main`).
  onedir is deliberate: onefile self-extracts to %TEMP% on every launch and
  costs startup latency.
- Dashboard: stdlib `http.server.ThreadingHTTPServer` on a daemon thread,
  binds `127.0.0.1` on an **ephemeral port**, per-launch token in the URL,
  origin checks on writes. Static files in `src/flow/dashboard/static/`.
  Opened in the default browser today — that is what D2 replaces.
- Audio: `sounddevice.RawInputStream`, opened once at startup, callback
  writes into `flow.audio.ring_buffer`. 16 kHz mono. `webrtcvad-wheels` trim,
  FLAC encode via `soundfile`.
- Hotkey: `pynput` global listener, hold Right Ctrl (`flow.hotkey`).
  `keyboard` is forbidden (needs admin).
- Injection: clipboard save -> set -> Ctrl+V -> restore after 150 ms
  (`flow.inject`). Never per-character typing.
- Network: one persistent `httpx.Client(http2=True)` pre-warmed at startup,
  driven from one asyncio loop on a dedicated thread (`flow.net_loop`).
- Models: STT `whisper-large-v3-turbo`; cleanup **`openai/gpt-oss-20b`**;
  commands `openai/gpt-oss-20b`.
- Instrumentation: `flow.latency` — `LatencyTracker` + SQLite + console
  table. Stages: capture, vad, encode, transcribe, merge, selection, command,
  cleanup, inject, plus a synthetic TOTAL row.
- State: `<data_dir>/` from `flow.platform.get_platform().user_data_dir()`
  holds `latency.db`, `history.db`, `dictionary.db`, `flow.log`.
  Config: `config.toml` + `FLOW_*` env; API key in `.env` as `GROQ_API_KEY`.
  `.env` is gitignored and must stay that way — this brief documents that the
  key lives there, so confirm the ignore rule rather than assuming it.
- Already present: tray icon (`flow.tray`; menu = Open Dashboard / Show
  History / Quit), single-instance named mutex `FlowDictation.SingleInstance`
  (`flow.gui`), always-on-top tkinter pill overlay (`flow.overlay`), per-app
  profiles (`flow.profiles`), personal dictionary (`flow.dictionary`),
  chunked streaming (`flow.streaming`), voice commands over a selection
  (`flow.commands`), analytics (`flow.analytics`).

If anything above is wrong, read the code and correct me before writing.

## Hard constraints

1. Free tier only. Groq for both STT and cleanup. No paid services, no paid
   signing cert assumed.
2. No Docker, no Electron, no Node toolchain. Disk is tight — report the
   bundle-size cost of every new dependency before adding it.
3. **Lowest end-to-end latency is the highest priority**, measured from key
   release to text at the cursor.
4. Windows target; OS-specific code stays behind `flow.platform`.
5. `python -m flow` / `flow run` from VS Code must never break.
6. Everything local except the two Groq calls. No telemetry. No audio or
   transcript leaves the machine.
7. No public network listener. The dashboard is loopback-only and remains the
   one agreed exception to CLAUDE.md's no-web-UI rule.

## Benchmark: Wispr Flow

Launch -> tray icon, no console, no browser tab, no terminal command. Hold
hotkey -> overlay with mic level and state. Release -> text at the cursor.
The dashboard is a window of the app. Quit leaves zero processes, no
listening socket, hotkey hook released.

## Cross-phase interaction rules

These four collisions are between features specified in different phases.
They are stated here, once, because whichever phase lands second would
otherwise silently break the first.

**R1 — VAD endpointing (D3) vs trigger detection (D8).** D8's strongest
false-positive filter is "a trigger preceded by a pause"; D3's endpointing
ends the recording *on* a pause. "...and that's the plan. [pause] frame it
better" is precisely the utterance endpointing would truncate before the
trigger arrives. Rule: when endpointing is enabled **and** any trigger is
enabled, endpointing does not finalize on the first qualifying silence. It
opens a grace window (default 600 ms, configurable) and keeps capturing; if
speech resumes within it and the new segment's leading words match a trigger
prefix in the automaton, capture continues to the real end of speech.
Otherwise the utterance finalizes at the original silence boundary and the
grace-window audio is discarded. Log every grace-window extension as a
latency stage so its cost is visible rather than assumed.

**R2 — Incremental injection (D3) vs `transform` triggers (D8) and
self-correction (D4).** You cannot re-frame text already pasted, and "no,
scratch that" needs a whole-utterance view. Rule: incremental injection is
**suppressed** — the path waits for the complete response — whenever a
`transform`-scope trigger fired or a correction marker was detected in the
transcript. Both are known before the model call, so this is a branch, not a
rollback.

Further: **benchmark incremental injection before building it.** Because
injection is clipboard-paste, incremental means repeated Ctrl+V into the
target app — visually noisy, and it shreds undo granularity in most editors.
Measure the perceived-latency win against a single paste of the complete
response. If the win is small, say so and do not build it. This is a
measurement task in D3, not a commitment.

**R3 — Overlay focus (D2) vs injection (everywhere).** The overlay must
never take focus, even momentarily, or the clipboard paste lands in the
overlay instead of the app being dictated into — a silent, intermittent,
miserable bug. `WS_EX_NOACTIVATE` alongside `WS_EX_TRANSPARENT`, and an
explicit test that the foreground window is unchanged across a full
show -> waveform -> hide cycle.

**R4 — Queued audio (D7) vs the no-audio-persisted rule (D0).** D7's "never
lose a recording" writes audio to disk, which D0 otherwise verifies never
happens. Not a contradiction, but the lifecycle must be specified: FLAC
blobs in `<data_dir>/queue/`, never in any DB; a hard cap (count and total
bytes, oldest evicted); deleted on successful retry; deleted on quit unless
still pending; enumerable and wipeable from the D6 "delete all". Never
`%TEMP%`, which no uninstall path covers.

---

# Phases

## D0 — Audit and baseline

Instrumentation only. No behavior changes, no optimization.

- Architecture map: processes, threads, the asyncio loop, the tray thread,
  the Tk thread, the dashboard thread, the ephemeral port, where state lives.
- **Extend `flow.latency.Stage`, additively.** This is a schema change to a
  populated `latency.db`, so: new members only, no renames, no reordering
  that changes existing values, and a note in the migration on how rows
  written before this phase read back (missing stages, not zeroed stages —
  the report must distinguish "not measured" from "took 0 ms"). Add what is
  missing to cover the full wall clock: `hotkey_press`, `upload`,
  `stt_first_byte`, `llm_first_token`, and explicit `inject_start` /
  `inject_complete` boundaries. Every run already has a run id; make sure it
  reaches the rotating log too.
- **Baseline after the stage additions land, not before.** A baseline taken
  against the old stage set is missing half the spans and cannot be compared
  with D3's after-table. Order: extend stages -> verify -> then capture the
  20 dictations (3 s / 10 s / 30 s). Report p50/p95 per stage and end-to-end.
- Confirm no audio and no transcript text lands in `latency.db` or
  `flow.log`. Record this as the baseline the R4 queue is an explicit,
  bounded exception to.
- Enumerate every concrete divergence between `python -m flow run` and
  `dist/flow/Flow.exe`: cwd, `sys._MEIPASS` resource paths, `sys.stdout is
  None`, missing DLLs, config and `.env` discovery, cold start.

**Done when:** the extended stage set is committed with its migration note,
the 20-clip baseline table was captured against that stage set, and the
divergence list exists. Nothing is optimized.

## D1 — Lifecycle and shell

Much of this exists; the work is auditing it against the checklist and
filling the gaps.

- Verify `Flow.exe` double-click starts engine + hotkey + tray + overlay with
  no console and no browser.
- stdout/stderr are `None` in the windowed build — confirm every logging path
  and third-party print survives that, and route everything to `flow.log`.
- **Tray menu.** Final shape is Open Dashboard - Agent On/Off - Settings -
  View Logs - Quit. D1 ships Open Dashboard, View Logs, Quit, and **Agent
  On/Off** (it needs only an engine flag and the icon-state work below).
  **Settings is deferred to D5** — do not add a placeholder menu entry that
  opens an empty pane; a dead menu item reads as a bug. Add it in D5 with the
  pane behind it.
- Icon reflects state: idle / listening / processing / error / disabled.
- **Second-launch focus needs a mechanism, and it already exists.** The
  running instance owns the mutex and a loopback server on an ephemeral port;
  it writes port and token to `<data_dir>/instance.json`. A second launch that
  fails to take the mutex reads that file and POSTs a `focus` request, then
  exits 0. Do not invent a named pipe for this.

  `instance.json` holds a **live auth token on disk**, so it is not an
  ordinary state file:
  - Create it with an owner-only ACL, not default permissions.
  - Delete it on clean shutdown, and rewrite it (never append) on start.
  - **Treat a stale file as absent.** Check the recorded PID is alive and
    owns the mutex before connecting; a stale port could belong to an
    unrelated process by then.
  - **A hung first instance must not hang the second one.** Use a short
    connect timeout (~1 s). On timeout, tell the user Flow is running but
    not responding and offer to end it — do not silently kill it, and do not
    fall through to starting a rival hotkey hook.
  - On any other failure, fall back to today's message dialog.
- Clean shutdown from tray and from the window: stop the audio stream, unhook
  pynput, cancel in-flight httpx calls, close the dashboard socket, remove
  `instance.json`, flush the latency queue and SQLite, remove the tray icon,
  exit 0. Verify in Task Manager that nothing survives.
- Closing the main window hides to tray. Quit is explicit.
- Top-level crash handler: log the traceback, native notification, clean
  shutdown — never a zombie hotkey hook with the mic hot.
- "Start on login" toggle via
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, implemented in
  `flow.platform.windows`.

**Done when:** double-click starts everything with no console and no browser;
the tray menu works and its icon tracks state; Quit leaves zero processes and
no listening socket, confirmed in Task Manager; a second launch focuses the
first rather than starting a second hotkey hook; and a forced crash leaves no
zombie process.

## D2 — Native-feeling UI

- Wrap the existing dashboard in **`pywebview`** (OS WebView2 runtime; no
  Node, no Electron). Report its bundle-size delta before adding it. If you
  prefer something else, argue it in 3 lines first; the winner must keep the
  bundle small and add no build toolchain.
- **Transport: DECIDED — keep the loopback socket.** Not the pywebview JS
  bridge. It is already built, tokened, and origin-checked, so the bridge
  would be a rewrite of the data layer for no user-visible gain. Keeping it
  also preserves opening the dashboard in a real browser with devtools during
  development, which a webview cannot offer; D6's live updates want SSE or
  polling, which a socket does naturally and a bridge does awkwardly; and
  D1's second-launch focus already depends on it. The user must still never
  see a URL, browser chrome, or a port.
- App icon (`packaging/flow.ico`), correct taskbar title, remembered window
  size/position, no browser context menu, no text-selection cursor on chrome
  elements, no visible loading flash on open.
- **Overlay upgrade.** `flow.overlay` already renders a pill via Pillow into
  tkinter with `-transparentcolor`. Add a live mic-level waveform, make it
  click-through and **non-activating** (`WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`
  via `flow.platform.windows` — see R3, this is not optional), make its
  position configurable (cursor-adjacent or bottom-center), and fade it out
  after inject. Keep the existing queue-based `set_state` threading contract —
  Tk objects are touched only on the Tk thread.
- Design: light-only dashboard, dark overlay capsule (DESIGN.md §2 — dark
  mode is ruled out, not deferred), system font stack, generous spacing,
  no framework default look, no emoji-as-icons.
- Native Windows notifications for bad API key, offline, and rate limited.

**Done when:** nothing on screen suggests a browser; the overlay makes each
dictation legible without opening the dashboard; and the R3 focus test passes —
the foreground window is provably unchanged across a full overlay cycle.

## D3 — Latency

Measure each item independently against the D0 baseline and report
before/after per stage. Say honestly which ones did not pay, and revert those.

Already done — verify and measure, do not rebuild: 16 kHz mono capture, FLAC
encode, VAD trim, persistent HTTP/2 client, skip-LLM heuristic, clipboard
paste injection, chunked streaming (`[streaming] enabled` defaults false —
benchmark whether it should default true).

Actually new work:

- **VAD endpointing:** end the recording on detected silence rather than
  waiting for key release. Opt-in; hold-to-talk stays the default contract.
  Must implement R1's grace window if triggers are enabled.
- **Connection pre-warm on hotkey press**, not only at startup, so TLS and
  DNS are already paid by the time audio is ready.
- **STT model benchmark on the 20-clip set:** `whisper-large-v3-turbo` vs
  `whisper-large-v3` (accuracy mode) vs `distil-whisper-large-v3-en`
  (fastest, English-only). Report WER-ish accuracy vs latency; I pick the
  default from the data. Expose all three in settings.
- **Cleanup-model benchmark** against the current `openai/gpt-oss-20b`.
- **Incremental injection: benchmark first, build only if it wins.** See R2.
  Measure perceived latency against a single paste of the complete response,
  and weigh it against the repeated-Ctrl+V cost to undo granularity. Report
  the number before writing the implementation.
- Audit the hot path for any import, config parse, or model load that should
  have happened at startup.

**Done when:** there is a before/after table per stage against the D0
baseline, p95 for a 10 s dictation is materially better, and every
optimization that did not pay is named and reverted.

## D4 — Transcription quality

Custom vocabulary and per-app context already exist (`flow.dictionary`,
`flow.profiles`); extend rather than replace.

- Surface the dictionary in Settings; add a post-transcription correction map
  for terms Whisper reliably mangles.
- Pass the focused window title / app name to the cleanup pass as a short
  label so tone adapts (code editor vs email vs chat).
- `temperature=0`. Drop the result when VAD saw no speech (silence
  hallucination). Detect repeated-phrase loops.
- Move the cleanup prompt into a **versioned file in the repo** so it can be
  iterated and diffed. Explicit negative instructions (never answer, never
  summarize, never add content) plus a few-shot example set.
- Self-correction: "no, scratch that" applies the correction rather than
  being transcribed literally. Detection of the correction marker happens
  pre-model, alongside D8's trigger detection, because R2 needs to know
  before the call whether incremental injection is suppressed.
- **Eval harness:** a fixed set of recordings with expected outputs plus a
  script reporting WER and latency per config, so prompt and model changes
  are measurable rather than vibes.

**Done when:** the eval script exists, current numbers are committed to the
repo, and the cleanup prompt lives in a versioned file rather than a string
literal.

## D5 — Controls and settings

In-app; no TOML editing and no env vars for anything a normal user touches.

- **API key -> Windows Credential Manager via `keyring`** (report bundle
  cost). `.env` and `FLOW_*` keep working for dev, and resolution order is
  explicit: keyring first, then `FLOW_GROQ_API_KEY`, then `.env`. On first
  run, if a key is found in `.env`, copy it into keyring and **leave `.env`
  untouched** — the dev flow depends on it and silently editing a user's file
  is worse than a duplicated secret. Say so in the migration message.
- A "Test key" button making one cheap call and reporting OK/failure clearly.
- A clean onboarding screen when no key exists, replacing `flow.gui`'s
  current message box.
- Agent on/off from both tray and dashboard, tray icon updating instantly.
- **Add the Settings entry to the tray menu here**, wired to the real pane
  (deferred from D1).
- Settings: hotkey (rebindable, with conflict detection), input device, STT
  model, cleanup on/off, streaming on/off, endpointing on/off, custom
  vocabulary, launch-on-startup, overlay position, history retention.
- Persist to `<data_dir>/settings.json`, validated on load with sane
  fallbacks layered over the existing `flow.config` precedence. A corrupt
  file must never brick startup.

**Done when:** a fresh machine with no `.env` can be taken from first launch
to a working dictation entirely through the UI; the key is in Credential
Manager and not in any plaintext file the app wrote; every setting listed
above is changeable without a restart or states plainly that it needs one;
and a deliberately corrupted `settings.json` still starts the app.

## D6 — Dashboard

Fed from `latency.db`, `history.db`, and `flow.analytics`. **Real data only** —
CLAUDE.md's Phase 6 rule stands: never fabricate a metric; show an explicit
"not available yet" and add the instrumentation. Anything assumption-derived
(time saved, which compares against `[analytics].typing_wpm`) is labelled an
estimate in the UI.

- Agent status, big and unambiguous: running / paused / error, with last
  error and time.
- Counters: transcriptions today / this week / total, words dictated,
  estimated time saved.
- Latency: p50 and p95 end-to-end plus a per-stage breakdown, so it is
  visible whether STT, LLM, or injection is the bottleneck. Sparkline over
  the last 50 dictations. Must render correctly for pre-D0 rows whose newer
  stages are absent rather than zero.
- Errors: recent failures with type, timestamp, and a one-line cause, grouped
  by type rather than an undifferentiated log dump.
- History: recent transcripts, searchable, copy and delete, and a "delete
  all" that actually wipes the rows — including the R4 queued-audio blobs.
- **Groq free-tier usage. Response headers are the source of truth**; read
  the limit and remaining values from them per request and store the latest.
  30 req/min and 2000 audio req/day are a *display fallback only*, for before
  the first request of a session — label them as such and never compute
  against them when headers are available. Free-tier numbers change.
- Live updates without a manual refresh.

**Done when:** every panel above renders from real stored data or shows an
explicit "not available yet"; no number on the page is fabricated or
hardcoded except the labelled rate-limit fallback; "delete all" leaves no
transcript rows and no queued audio on disk; and the page updates without a
manual refresh.

## D7 — Reliability and packaging

- Recovery per failure mode, each with a specific user-facing message: no
  network, invalid key (no retry on 401), 429 (exponential backoff with
  jitter, surface which limit was hit from the headers), Groq 5xx, timeout,
  no mic, mic held by another app.
- **Never lose a recording** — queue the audio per R4's lifecycle, retry, and
  allow a re-run from history.
- Whole-pipeline timeout budget; on exceed, inject the raw transcript rather
  than nothing.
- Packaging: keep **onedir**. Ship a zip or an installer, not onefile.
  Version metadata and icon are already in the spec. Target startup under
  2 seconds; report final bundle size and cut unused collected modules.
- Document what SmartScreen shows for an unsigned binary and the options.
- Documented uninstall/reset: settings, credentials (keyring entry), all
  three DBs, the queue directory, `instance.json`, and the Run registry key.
- README: build steps, run-from-source steps, architecture diagram,
  troubleshooting.

**Done when:** every failure mode above has been deliberately induced and
produces its specific message and retry behavior; a recording made while
offline survives and can be re-run from history; startup is under 2 seconds
with the bundle size reported; and the documented uninstall leaves nothing
behind on a machine that ran the app.

## D8 — Voice command trigger system

Extends `flow.commands`, which today handles spoken instructions over a
*selection*. This phase is about triggers inside ordinary dictation: a
configured phrase stops being content and becomes an instruction. Saying
"frame it better" must not appear in the output; instead the whole output is
rewritten with better framing. Users define their own triggers from the UI.

The two failure modes that matter: (a) token blowup from stuffing every
configured rule into every prompt, and (b) silently eating words when a
trigger fires by accident. The architecture below is not optional.

**One pipeline, not two.** D8's `scope: selection` is what `flow.commands`
already does. Refactor `flow.commands` to consume this phase's resolver and
its single-model-call path, rather than leaving a second, divergent
instruction pipeline alongside it. If that refactor turns out to be larger
than the rest of D8, stop and tell me before doing it.

### D8.1 Action model

Declarative records, versioned, stored in settings, import/export from the
UI. Adding a behavior must never require touching the pipeline.

    id           stable slug
    phrases[]    trigger phrases + auto-generated variants
    enabled      bool
    type         deterministic | macro | mode | transform
    scope        utterance | selection | previous_output
    instruction  compact directive text (mode/transform only)
    payload      literal text (deterministic/macro only)
    priority     int, for conflict resolution
    apps[]       optional; active only in these focused apps

Types in ascending cost order — the type system exists so that most triggers
never reach the model at all:

- `deterministic` — pure local string ops: insert today's date, wrap in a
  code fence, insert a saved signature, uppercase. Zero tokens, zero latency.
  Anything expressible without a model must be this type.
- `macro` — expand the phrase into a stored block of text. Zero tokens.
- `mode` — sets a formatting/tone flag for this utterance ("bullet points",
  "formal", "terse"). Adds one short directive line, not a paragraph.
- `transform` — genuinely needs the model to rewrite the output ("frame it
  better"). The expensive one; keep it rare. Suppresses incremental
  injection per R2.

### D8.2 Detection — local, pre-LLM

Runs on the raw transcript before any model call, so matching costs zero
tokens.

- Normalize: lowercase, strip punctuation, collapse whitespace, expand common
  ASR contractions.
- **Single-pass matching whose cost does not scale with the number of
  configured triggers.** A precompiled regex alternation, rebuilt only when
  the trigger list changes, is acceptable and is the expected default — at
  tens of triggers it is fast enough and is far less code than the
  alternatives. Anything more complex (a hand-rolled Aho–Corasick, or
  `pyahocorasick` as a C-extension dependency) must be justified with a
  measurement showing the regex is actually the bottleneck, plus its bundle
  cost. Do not reach for the automaton by default.
- Handle ASR variance without a fuzzy scan over the whole transcript: index
  phrases by a normalized/phonetic key and run bounded edit-distance checks
  **only on candidate windows** the matcher flags.
- **Use the VAD segment boundaries already available in `flow.audio.vad`.** A
  trigger preceded by a pause and sitting at a clause boundary is almost
  certainly a command; the same words mid-sentence are probably content.
  Require a word boundary plus either a pause or a clause boundary before
  firing. This single rule kills most false positives — and it is why R1
  exists, since endpointing would otherwise cut the utterance at exactly that
  pause.
- Escape hatch: a configurable literal prefix ("literally ...", "quote ...")
  forces the phrase through as text.
- Excise matched spans before the transcript goes downstream, recording their
  positions: a trailing trigger applies to the whole utterance, a
  mid-utterance one may scope to the preceding clause.

### D8.3 Instruction resolution — expand only what fired

The core token-efficiency rule: **the model never sees the trigger library.**
It sees only the directives for triggers that actually fired — normally zero,
occasionally one or two.

- Resolve fired ids to precompiled directive strings held in memory. No
  per-request re-deriving, re-templating, or re-serializing.
- Directives are compact and imperative, one line each, written once when the
  user saves the trigger, not generated at runtime.
- If only `deterministic` / `macro` actions fired, apply them locally and
  **skip the model entirely**, honoring the existing skip heuristic from
  CLAUDE.md Phase 2.
- **One model call, always.** Cleanup and all fired transforms merge into a
  single request with a small directive block. Never chain one call per
  trigger — that multiplies both tokens and latency.
- Prompt ordering: the stable part first (system prompt + cleanup rules,
  byte-identical across requests), the variable part last (directives,
  context, transcript). Check whether Groq prompt caching applies to the
  cleanup model and report what it saves; if it does, this ordering is what
  makes it work.
- Context by reference, not by volume: the focused app is a short label,
  never window contents. `previous_output` scope pulls from a small ring
  buffer of the last few injections and is sent **only** when a trigger with
  that scope fired.

### D8.4 Instruction hierarchy

State the precedence explicitly in the prompt so output stays coherent when
several things apply at once:

1. Deterministic local ops (applied outside the model)
2. Fired trigger directives, by `priority` then by recency
3. Default cleanup rules (fillers, punctuation, don't-answer-don't-summarize)
4. Model defaults

Conflicting triggers ("make it terse" + "add more detail") resolve by
priority, ties by last-spoken, and the conflict is logged and surfaced in the
dashboard rather than silently resolved.

### D8.5 UI

- Trigger manager: list, add, edit, delete, enable/disable, reorder priority.
  Type picker with sensible per-type fields.
- Live conflict warning when a new phrase overlaps or is a substring of an
  existing one.
- **Test bench:** paste or record a sample utterance and see which triggers
  fire, the excised transcript, the resolved directives, the estimated token
  cost, and the final output — without dictating into a real app.
- Ship a small set of sensible defaults, all disabled by default.
- **Undo:** a false positive silently deletes words, so every injection is
  recoverable from history with the raw pre-trigger transcript preserved.

### D8.6 Measurement

Extend `latency.db` and the dashboard: triggers fired per action,
prompt/completion tokens per request, share of utterances resolved with no
model call, detection time, R1 grace-window extensions and their cost, and a
manually flaggable false-positive counter. Add trigger cases to the D4 eval
set — including negative cases where the phrase appears as ordinary speech
and must **not** fire.

**Done when:** a trigger is definable from the UI in under 30 seconds, works
on the next utterance with no restart, deterministic triggers make no network
call, `flow.commands` runs through the shared resolver rather than its own
path, and the D4 eval shows no regression on the negative cases.

---

## Working rules

- Small, reviewable commits with clear messages. One phase per branch,
  named `d0-...` through `d8-...`.
- Propose architecture changes in a few lines and wait for go-ahead.
- No dependency without its bundle-size cost stated first.
- Show the measurement when you optimize. If it did not help, say so and
  revert it.
- Disagree with anything in this brief before implementing it, not after.
- Never leave `python -m flow run` broken.
