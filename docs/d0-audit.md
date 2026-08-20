# D0 — Audit and baseline

Deliverables: architecture map, extended stage set, baseline harness,
source-vs-exe divergence list. No optimization; nothing on the hot path was
made faster in this phase, by design.

Companion files:
- `scripts/baseline.py` — guided 20-clip capture, writes `docs/d0-baseline.txt`
- `tests/test_latency_d0.py` — schema and privacy guarantees, 7 tests

---

## 1. Architecture map

### Processes

One. `Flow.exe` (windowed) or `flow run` from source. `flow-cli.exe` is the
same package with a console attached for the terminal subcommands and is not
running while the app is. There is no helper process, no service, no
subprocess.

### Threads

Eleven kinds, all daemon except the main thread. Named threads use the
`flow-` prefix, which makes them identifiable in a stack dump.

| Thread | Owner | Lifetime | Notes |
|---|---|---|---|
| main | `Engine.run_overlay` | app | Tk mainloop; owns every widget |
| PortAudio callback | `AudioCapture` | app | writes ring buffer only, no I/O |
| pynput listener ×2 | `HotkeyListener` | app | dictation key + command key |
| `flow-net-loop` | `NetworkLoop` | app | the single asyncio loop; all Groq I/O |
| `flow-latency-writer` | `LatencyTracker` | app | owns the `latency.db` connection |
| `flow-tray` | `TrayIcon` | app | pystray's own run loop |
| `flow-dashboard` | `DashboardServer` | app | `serve_forever`, plus a thread per request |
| dictation worker | `Engine._on_release` | one dictation | spawned per utterance, unnamed |
| selection worker | `Engine._on_command_press` | one command | Ctrl+C round trip |
| `flow-chunk-streamer` | `ChunkStreamer` | press→release | only when `[streaming] enabled` |
| clipboard restore | `TextInjector` | 150 ms | `threading.Timer`, one per injection |

The dictation worker and selection worker are spawned per event and are the
only unnamed threads. Worth naming in D1 — a hung dictation currently shows
up in a dump as an anonymous `Thread-N`.

### Sockets

One, and only while the app runs: `DashboardServer` binds `127.0.0.1` on an
**ephemeral port** (port `0`, kernel-assigned), guarded by a per-launch token
embedded in the URL and an origin check on state-changing routes. Nothing
else listens. This already satisfies what D1 asked for.

### State on disk

All under `%LOCALAPPDATA%\Flow` (`get_platform().user_data_dir`):

| Path | Written by | Contains |
|---|---|---|
| `latency.db` | `flow-latency-writer` | run + stage timings, numeric metadata |
| `history.db` | `HistoryStore` | **transcripts** — the user's actual words |
| `dictionary.db` | `DictionaryStore` | personal vocabulary terms |
| `flow.log` | rotating file handler | diagnostics, 1 MB × 3 |

Config is read once at startup into a frozen `Config`; nothing re-reads it.
Resolution order is `$FLOW_CONFIG` → `./config.toml` → bundle dir →
`<user config dir>/config.toml`, with `.env` searched in cwd and then the
bundle dir.

---

## 2. Stage set, extended

Four members added to `flow.latency.Stage`, all additive:

| Stage | Span | Why it was missing |
|---|---|---|
| `hotkey_hold` | press → release | existed only as an annotation, so it got no percentiles |
| `dispatch` | release → worker thread scheduled | invisible: the tracker's `TOTAL` starts *after* it |
| `stt_first_byte` | request handed to httpx → response headers | `transcribe` was one opaque number |
| `llm_first_token` | cleanup request → first content delta | ditto for `cleanup` |

### The schema rule, now enforced

Stages persist as their **string value**, never their enum position. So
members can be inserted in pipeline order without touching stored data —
only the report's column order changes. Renaming a value is the thing that
breaks: it orphans every historical row carrying the old spelling, silently,
by splitting one stage into two half-populated ones.
`tests/test_latency_d0.py::test_pre_d0_stage_values_are_never_renamed` pins
the nine pre-D0 values so this cannot regress unnoticed.

### Reading back pre-D0 rows

A run recorded before D0 has **no row** for the new stages. It is not a zero.
The distinction is now explicit rather than implied:

- `StageStats.sampled` records how many runs the stage *could* have appeared in.
- `StageStats.partial` is true when `count < sampled`.
- `format_table` stars such stages and prints a footnote.

Two different things produce `partial`, and the table flags rather than
guesses between them: the stage post-dates those runs, or it genuinely did
not run (cleanup skipped by the heuristic, `merge` on a non-streamed run,
`selection` on a plain dictation). Either way the percentiles describe only
the runs that have the stage. Folding a missing stage in as 0 ms would drag
every percentile toward zero and flatter the numbers — the exact failure
mode this phase existed to prevent.

### What is deliberately not measured

**Upload time on its own.** httpx exposes no hook between "request handed
over" and "response headers received", so wire time, Groq's queue wait, and
model compute are one indivisible number (`stt_first_byte`). Separating them
needs a custom transport, which D0 does not justify. The FLAC byte count is
annotated on the run instead, so payload size can still be correlated
against that span in D3.

**`inject_start` / `inject_complete` as separate stages.** The existing
`inject` stage already brackets exactly that: clipboard save, set, Ctrl+V.
The 150 ms clipboard *restore* runs on a `threading.Timer` after the span
closes, so it has never counted against user-visible latency. No new stage
was needed; the misconception was mine in the brief.

---

## 3. Privacy verification

**Result: `latency.db` holds no transcript text and no audio.** Every one of
the 15 `annotate()` call sites passes counts, durations, booleans, or fixed
labels — `words`, `characters`, `audio_ms`, `speech_ms`, `cleanup_skip_reason`,
`skipped="no_speech"`. Nothing passes transcript content.
`test_persisted_runs_carry_no_transcript_or_audio` asserts the store
round-trips only numeric metadata.

Three things to be aware of rather than alarmed by:

1. **`focused_process` is stored** (e.g. `Code.exe`). Not transcript content,
   but it is a record of app usage over time. D4 and D8 both need it; worth
   surfacing in D7's uninstall/reset documentation.

2. **`history.db` holds full transcripts, by design.** That is the feature.
   It is the file D6's "delete all" must actually wipe, and D7's uninstall
   must remove.

3. **One unbounded path from a remote server into the metrics DB.** On a
   non-JSON 4xx, `_client_error_message` puts `response.text[:300]` into a
   `GroqError`, which reaches `runs.error` via `mark_failed` and the log via
   the handler. `_describe` truncates at 500 chars, which bounds the size but
   not the content. Groq's error bodies do not normally echo the request, so
   this is a latent vector, not an observed leak. **Recommend fixing in D7**
   (error handling): map remote errors to a fixed set of local labels and log
   the raw body at DEBUG only. Not fixed here — D0 changes no behavior.

---

## 4. Baseline

`scripts/baseline.py` walks 20 dictations (7 short / 7 medium / 6 long),
buckets runs by their **measured** `hotkey_hold` rather than by which prompt
was on screen, and writes `docs/d0-baseline.txt`. Runs with no `hotkey_hold`
— everything recorded before D0 — are reported as `unclassified` rather than
guessed into a bucket.

**The 20-clip baseline is not captured yet — it needs your microphone.** Run:

```
flow run                      # in one window
python scripts/baseline.py    # in another
```

### Guard against a fake baseline

`baseline.py` refuses to write a report unless at least 10 runs in the window
carry a `hotkey_hold` stage, and confirms each clip against the DB as you go
(`captured 4/20 | hold 2.9s -> short | ok`).

This exists because the first version did not have it: run with `--since 1`
as a smoke test, it happily re-reported the entire historical database as a
baseline — epoch timestamp, every row in `unclassified`, zero new dictations —
and that output was briefly written up as a "pre-D0 reference". A file that
reads as a baseline but contains no new dictations is the worst possible
artifact for D3 to diff against. It is now a hard error.

### Pre-D0 analysis (75 historical runs)

Not a baseline and not clip-classified — these predate `hotkey_hold`. But
they answer two questions D3 would otherwise guess at.

**First: the `cleanup` stage was measuring the wrong thing.** The span used
to wrap the skip branch as well as the model call, so 13 of 46 recorded
cleanup spans are 0.00 ms skips. Splitting them:

| | n | p50 | p90 | max |
|---|---|---|---|---|
| skipped (0 ms spans) | 14 | 0.0 | 0.0 | 2.2 |
| real model calls | 32 | **238.0** | **728.5** | **1118.1** |
| blended (what was reported) | 46 | 178.4 | 625.0 | 1118.1 |

The blend does not just add noise, it inverts the signal: **improving the
skip rate makes the cleanup percentile drop without the model call getting
any faster.** A D3 "optimization" that only tightened the heuristic would
have shown up as a latency win in a stage it never touched. Fixed in this
phase — the CLEANUP span now opens only when the model is actually called,
and skips are recorded as `cleanup_skipped` metadata, which is where
skip-rate analysis belongs.

**Second: cleanup's spread is jitter, not length.** On real model calls
only, input length vs duration gives **r = +0.385, r² = 0.149** — length
explains about 15% of the variance. The slowest call (1118 ms) had 260
input chars; a 888-char input finished in 625 ms.

Per the D3 decision rule, that points at **timeout budget with
raw-transcript fallback**, not at streaming: the delay is largely *before*
the first token, so incremental injection would hide it rather than remove
it. It also argues against R2's incremental injection on cost/benefit
grounds independently of its D8/D4 collisions.

**Third: the skip heuristic's real rate.** Of the 26 runs carrying a
`cleanup_skipped` label, 14 skipped — **54%**, not the 33% first reported
(that figure came from the polluted blend). Reasons: `verbatim_profile` 8,
`short_and_clean` 5, `low_confidence` 1. Note that 8 of 14 skips are the
per-app VERBATIM profile, i.e. dictating into a terminal or editor, not the
heuristic judging the text. The text-based heuristic fires only 5 times in
26. **Whether the other 12 model calls were earning their keep is the open
question for D3**, and it is the cheaper lever than streaming: a skip
removes the whole 238 ms span rather than concealing it.

**Other stages** (unaffected by the above):

```
Stage        Count    p50    p90     p99   mean     max
capture         69    2.8    9.3    36.3    4.0    36.3
vad             69   10.1   19.2    51.3   10.3    51.3
encode*         68   31.8   44.7    71.5   27.9    71.5
transcribe*     68  404.9  557.4   585.7  392.2   585.7
inject*         68   17.9   26.5    40.4   17.9    40.4
TOTAL           69  622.6  885.2  1479.0  599.8  1479.0
```

**`transcribe` is a floor, not a tail.** p50 405 ms, p99 585 ms, max 585 ms —
an unusually tight distribution. Groq is being consistent. STT cannot be made
less variable; it can only be made *lower*, and only by changing model. That
makes D3's three-model benchmark the only lever on the largest single stage,
and it moves the median rather than the tail.

**p50 total 623 ms** (inside CLAUDE.md's 1000 ms budget); **p99 1479 ms**
(outside it). The tail is the problem, and cleanup owns it.

---

## 5. Source vs `.exe` divergence

Measured, not assumed. `dist/flow` was built from the current tree.

| # | Divergence | Detail |
|---|---|---|
| 1 | **The bundle contains a plaintext copy of the API key** | `scripts/build_app.py` copies `.env` to `dist/flow/.env` deliberately, so the built app works standalone. `dist/` is gitignored, so it does not reach the repo — but D7's "ship a zip" would ship the key. D5's keyring migration is the real fix. Flagging now because it constrains D7. |
| 2 | **`.env` discovery differs by launch method** | From a foreign cwd, the exe reports `groq_api_key = set` (found via `bundle_dir()`); source reports `unset`. Correct and deliberate — `bundle_dir()` exists so Start-menu and taskbar launches work — but it means source and exe genuinely resolve config differently, and a source-run reproduction of an exe bug can silently differ. |
| 3 | **`sys.stdout` / `sys.stderr` are `None` in `Flow.exe`** | Already handled: `configure_logging` skips the console handler when `sys.stderr is None`, and `flow.gui` routes fatals to `show_message`. Verified by reading; D1 should verify it holds for third-party output too (pystray and PortAudio both write to stderr on some failures). |
| 4 | **Cold start: comparable, with a worse first run** | source `flow config` 376/293/279 ms; exe `flow-cli config` 1249/298/379 ms. The 1249 ms is first-touch disk on a 92 MB bundle; warm runs match source. Confirms the onedir choice — onefile would pay a self-extract on *every* launch. D7's "under 2 s startup" is currently met. |
| 5 | **Bundle is 92 MB** | `numpy.libs` 21 MB, `PIL` 13 MB, `libcrypto` 7.7 MB, `python311.dll` 5.6 MB, `_tcl_data` + tcl/tk ~7.8 MB. numpy is used in exactly one place (`flow.audio.flac`, viewing raw PCM as int16). D7 should test whether that can be done with `array`/`memoryview` and drop 21 MB. Noted, not acted on. |
| 6 | **No config.toml is present in either** | Both report `source_file = None` and run on defaults. The repo ships only `config.example.toml`. Not a divergence today, but D5's `settings.json` will be the first real config file and needs to work identically in both. |
| 7 | **Two dictation threads are unnamed** | `Thread-N` in a dump rather than `flow-dictation` / `flow-selection`. Trivial, but it is the thread most likely to be hung when something goes wrong. D1. |

Not found: no missing DLLs, no `_MEIPASS` path bugs (the code uses
`bundle_dir()` / `Path(__file__).parent` correctly, and the spec collects
`logo_mask.png` and the dashboard's static files explicitly), no cwd
assumptions beyond the deliberate config search.

---

## 5b. The 6 failed runs (8%)

All six are **the same bug, and it is already fixed**:

```
TypeError: _Transcript.__init__() got an unexpected keyword argument 'speech_ms'
```

Every one reached `capture,vad,encode,transcribe` and died constructing the
result. They span 08-19 21:16 → 08-20 00:32, a single development window
during CLAUDE.md Phase 6 when `speech_ms`/`audio_ms` were added to the call
site before the dataclass. The current `_Transcript` has both fields.

**This is not a reliability signal.** No rate limiting, no timeouts, no
network failures — the 8% is a development artifact sitting in the same
database as real runs. Two consequences:

- D7's failure-handling design gets no evidence from this data. The genuine
  failure modes still need to be induced deliberately, as D7 already specifies.
- **The baseline must be captured on the current build**, and reported
  separately from history. `baseline.py` does this by construction (it only
  reads runs after its own start), which is a second reason the empty-report
  guard matters.

Worth noting for D6: a crash-loop bug of this kind produced six identical
error strings, which is exactly the case for grouping errors by type rather
than dumping a log.

## 6. State of the tree

- 507 tests pass (500 pre-existing + 7 new).
- `python -m flow run` unaffected.
- Behavior changes: none on the hot path. `transcribe` now uses
  `client.stream()` instead of `client.post()` purely to observe header
  arrival; the body is read immediately after, so callers see identical
  results.

## 7. Recommended before D1

1. Capture the real 20-clip baseline (needs your mic).
2. Decide the D2 transport question — loopback socket vs pywebview JS bridge.
   D1's second-launch focus is specified in terms of the socket, so this
   wants deciding before D1 lands, not before D2.
