# Sway — Test and Verification Plan

**Verified against:** `1e85b9f`, 2026-08-26. **1391 tests across 43 test modules, all passing.**

```powershell
.venv\Scripts\python.exe -m pytest        # ~50 seconds
```

---

## 1. Testing philosophy

Three rules from `CLAUDE.md`, restated because they explain the shape of everything
below.

**1. Do not test the audio callback.** It runs on the OS audio thread, it must not do
I/O, and a test that drove it would be testing PortAudio. Test the
transcript-processing pure functions instead — that is where the product's actual
judgement lives.

**2. Pure functions carry the logic, so they can carry the tests.** Every module that
decides something — should the model be skipped, did the speaker correct themselves,
which trigger fired, is this rewrite safe, what does this diff look like — is
deliberately free of network, threading, SQLite, and Tk. That is a testability
decision, not an aesthetic one.

**3. Measurement is a test.** A latency claim without a stored before/after table is
an opinion. The `latency.db` harness is part of the test surface, not an add-on.

### What is *not* automatically tested, stated honestly

The hotkey listener, the audio callback, the clipboard paste against real
applications, the tray icon's registration with the shell, and the bar's real
rendering all need a live desktop session and a person. They are covered by §4's
manual matrix and by the diagnostic scripts in §5, not by pytest.

---

## 2. Automated coverage by layer

### Pure decision logic — the core

| Module | Test file | What must hold |
|---|---|---|
| `cleanup` | `test_cleanup.py`, `test_cleanup_guardrails.py` (648 lines) | Skip heuristic fires on short clean text and not otherwise; language-aware filler detection; **every guardrail** — length, entity survival, emptiness, hallucination loops — rejects a bad rewrite and accepts a good one |
| `corrections` | `test_corrections.py` | Detection fires on real corrections and stays quiet on ordinary speech. Negative cases carry the same weight as positive ones, because a false positive silently deletes words |
| `triggers` | `test_triggers.py` | Whole-phrase word-boundary matching only; longest-first ordering; trailing punctuation stripped; raw transcript preserved |
| `transforms` | `test_transforms.py`, `test_transform_end_to_end.py` | Directive block construction; output expectations; shortcut conflict detection; the fired-only rule |
| `profiles` | `test_profiles.py` | Process name → style, including verbatim for terminals and editors |
| `analytics` | `test_analytics.py` | Word counting; WPM against *speech* duration not hold duration; refusal to report WPM below the noise floor or above the plausibility ceiling |
| `diff` | `test_diff.py` | Word-level diff rejoins to the original text exactly, including spacing |
| `streaming` | `test_streaming.py` | Chunk clock; boundary dedup via longest matching run |
| `motion`, `orb` | `test_motion.py`, `test_orb.py` | Animation curves are sampled not stepped; the orb is checked against upstream's **frozen golden vectors**, so the port is verified as numbers rather than as an impression |

### Audio primitives

| Module | Test file | What must hold |
|---|---|---|
| `audio/ring_buffer` | `test_ring_buffer.py` | Allocation-free wraparound; correct frames returned across the wrap; bounds |
| `audio/vad` | `test_vad.py` | Silence trimmed with padding retained; a no-speech clip is dropped |
| `audio/flac` | `test_flac.py` | Real FLAC output, correct sample rate and channel count |

### Stores and instrumentation

| Module | Test file | What must hold |
|---|---|---|
| `latency/store`, `tracker`, `report` | `test_store.py`, `test_tracker.py`, `test_report.py`, `test_latency_d0.py` | Real SQLite. Bounded queue drops rather than blocks; **a missing stage reports as absent, never as 0 ms**; percentiles are correct; the D0 stage additions read back correctly against pre-D0 rows |
| `history` | `test_history.py` | Round-trip, search, delete, delete-all, retention pruning |
| `dictionary` | `test_dictionary.py` | Terms, pinning, hit counting, prompt-bias truncation to the character budget |
| `migrate` | `test_migrate.py` | Every migration is additive and idempotent |

### Configuration and settings

| Module | Test file | What must hold |
|---|---|---|
| `config` | `test_config.py` (577 lines) | Precedence env > TOML > defaults; unknown keys are a hard error; `Secret` never renders |
| `settings` | `test_settings.py` (507 lines) | **A corrupt file never bricks startup**; per-field fallback rather than whole-file discard; atomic write; unknown keys round-trip |
| `credentials` | `test_credentials.py` | keyring → `SWAY_GROQ_API_KEY` → `GROQ_API_KEY` order; `.env` copy-up leaves `.env` untouched; a missing backend degrades instead of failing |

### Boundaries, with fakes

| Module | Test file | Fake used |
|---|---|---|
| `inject` | `test_inject.py` | Fake platform adapter — save/set/paste/restore order, restore skipped when the clipboard changed, elevated-window path |
| `selection` | `test_selection.py` | Fake platform adapter — Ctrl+C round trip, timeout, empty selection |
| `hotkey` | `test_hotkey.py` (1396 lines) | Synthetic events — binding parsing for every shape, engage/disengage semantics, min-hold rejection, injected-event filtering |
| `groq_client` | `test_groq_client.py`, `test_groq_client_signals.py` | Transport stub — one retry with jitter on 5xx, **429 never retried**, rate-limit headers parsed, first-byte callbacks fire at most once |
| `dashboard` | `test_dashboard_server.py`, `test_dashboard_routes.py`, `test_dashboard_queries.py`, `test_dashboard_actions.py` | Real server on an ephemeral port — token required, cross-origin writes rejected, every action validates its payload |
| `instance` | `test_instance.py` | A stale file is treated as absent; a dead PID is not trusted |
| `bar`, `tray`, `gui` | `test_bar.py` (1082 lines), `test_tray.py`, `test_gui.py` | Render keys and state transitions without a display where possible |

---

## 3. The invariants that must never regress

These are the tests to look at first when something breaks. Each corresponds to a
failure mode that is either silent or catastrophic.

| # | Invariant | Why it is on this list |
|---|---|---|
| I-1 | A rejected rewrite falls back to the **raw transcript**, never to nothing | The user must always get their own words |
| I-2 | A trigger match preserves the raw pre-substitution transcript | A false positive must be recoverable, not a silent deletion |
| I-3 | A missing latency stage reports as absent, not 0 ms | Otherwise every percentile lies and improvements are manufactured |
| I-4 | 429 is never retried | The free tier is 30 req/min; blind retries consume it faster |
| I-5 | The clipboard restore is skipped if the clipboard changed | Otherwise the user loses a copy they made during the window |
| I-6 | A corrupt `settings.json` starts the app | Losing a transform library to a misspelled boolean is unrecoverable |
| I-7 | No transcript text reaches `latency.db` or `sway.log` | A stated privacy guarantee |
| I-8 | The dashboard rejects a request with no token | Otherwise any web page can read the user's history |
| I-9 | The bar never takes focus | Otherwise dictation lands in the bar |
| I-10 | A failed run is recorded but excluded from timing statistics | Otherwise failures flatter the percentiles |

---

## 4. Manual verification matrix

Run before any release. Each row is a real machine, a real desktop session, and a
person.

### Lifecycle

| # | Check | Pass condition |
|---|---|---|
| M-1 | Double-click `Sway.exe` | Engine, hotkey, tray, and bar start. **No console window, no browser tab.** |
| M-2 | Quit from the tray | Task Manager shows zero surviving processes; no listening socket; `instance.json` gone |
| M-3 | Launch a second copy | The first is focused. No second hotkey hook — verify by dictating once and getting **one** paste, not two |
| M-4 | Kill the first instance uncleanly, then launch | The stale `instance.json` is ignored; the app starts normally |
| M-5 | Force a crash | Traceback in `sway.log`, a notification, clean shutdown, **no zombie hotkey hook with the mic hot** |
| M-6 | Close the main window | Hides to tray; dictation still works |
| M-7 | Toggle launch-at-login, reboot | Sway starts; toggling off removes the `HKCU\...\Run` key |

### Dictation

| # | Check | Pass condition |
|---|---|---|
| M-8 | Dictate into Notepad, Slack, and Outlook | Text at the cursor; tone differs per app; clipboard restored |
| M-9 | Dictate into Windows Terminal and VS Code | **No cleanup call** (confirm in the log); the transcript is pasted exactly |
| M-10 | Begin speaking as you press the key | The first syllable survives (pre-roll) |
| M-11 | Tap the key for under 200 ms | Nothing happens; no request is sent |
| M-12 | Hold the key and say nothing | Dropped at VAD; no upload; no quota spent |
| M-13 | Dictate into an elevated window | "Elevated window — copied instead"; the text is on the clipboard |
| M-14 | Copy something during the 150 ms restore window | Your copy survives; the restore is skipped |
| M-15 | Say "meeting at 7 — no, 8" | The pasted text contains 8, not 7 |
| M-16 | Hands-free: tap to start, tap to stop | Works; the timeout ends a forgotten session |

### Instructions

| # | Check | Pass condition |
|---|---|---|
| M-17 | Select text, press a transform shortcut | Selection replaced; **exactly one** network call |
| M-18 | Press a transform shortcut with nothing selected | Bar reports it; nothing is sent to Groq |
| M-19 | Dictate ending in a transform phrase | One model call total; the phrase does not appear in the output |
| M-20 | Say a snippet phrase | Substituted locally; **zero** network calls attributable to it |
| M-21 | Say a snippet phrase mid-sentence as ordinary speech | Does **not** fire (word-boundary rule) |
| M-22 | Define a new transform from the UI | Works on the next utterance with **no restart** |

### Failure modes — each deliberately induced

| # | Induce | Pass condition |
|---|---|---|
| M-23 | Disconnect the network | Specific "no network" message; mic released |
| M-24 | Set an invalid API key | Specific message; **no retry** on 401 |
| M-25 | Exceed the rate limit | The specific limit hit is named, read from the response headers; no blind retry |
| M-26 | Unplug the microphone while running | Specific message; the app does not hang |
| M-27 | Hold the mic in another app exclusively | Specific message |
| M-28 | Corrupt `settings.json` by hand | The app starts; the bad field falls back; the rest survives |
| M-29 | Delete `dictionary.db` while running | Degrades; dictation still works |
| M-30 | Run without WebView2 | Falls back to the default browser; every screen works |
| M-31 | Run with the tray backend unavailable | Logs a warning; keeps running |

### Dashboard

| # | Check | Pass condition |
|---|---|---|
| M-32 | Every panel with a fresh database | Explicit "not available yet", never a fabricated zero |
| M-33 | Every derived figure | Time saved is **labelled an estimate** |
| M-34 | Delete all history | No transcript rows survive; re-open confirms |
| M-35 | `curl` an API route with no token | `403` |
| M-36 | A cross-origin POST | `403` |
| M-37 | Any page after a dictation | Updates without a manual refresh |

---

## 5. Diagnostic and measurement scripts

| Script | What it does |
|---|---|
| `scripts/baseline.py` | Captures a **20-dictation baseline** bucketed by clip length (3 s / 10 s / 30 s), counting only runs recorded after it started so an existing `latency.db` cannot dilute the numbers. Bucketing is by *measured* `hotkey_hold`, not by which prompt was on screen. A capture that reads as a baseline but contains no new dictations is a hard error, because it is the worst possible artefact to diff against. |
| `scripts/e2e_check.py` | Runs recorded speech through **every stage of the real pipeline against the real Groq API** — VAD, FLAC, Whisper, skip heuristic, cleanup with the configured prompt, normalisation, and every guardrail — reporting per-stage latency and whether each clip's result was correct. Does not cover the hotkey listener, the audio callback, the bar, or the paste. |
| `scripts/eval_corrections.py` | The correction eval. **Detection** (offline, free): does the scanner fire on a real correction and stay quiet on ordinary speech? **Resolution** (`--live`, one Groq call per case): does the pasted text actually contain the value the speaker settled on? Reported separately because they fail differently, and the negative cases carry equal weight. |
| `scripts/check_recording.py` | Answers three questions the baseline harness cannot distinguish: is the app writing runs at all, is it writing them where the script looks, and do new runs carry the stages the baseline needs. |
| `scripts/tray_probe.py` | Verifies the shell actually registered the tray icon, rather than the app believing it did. |

**Standing rule:** commit the numbers these print whenever the prompt, the model, or a
pipeline stage changes, so the next change has something to diff against.

---

## 6. Known gaps in the plan

Named rather than left implicit:

1. **No eval set for the cleanup prompt itself.** `eval_corrections.py` covers
   corrections; there is no fixed recording set with expected outputs reporting WER
   and latency per config for the general cleanup path (PRD FR-4.11's companion).
2. **No negative-case corpus for transform triggers.** The specified requirement is
   that a trigger phrase appearing as ordinary speech must **not** fire, measured — not
   just structurally prevented.
3. **No automated foreground-window assertion.** I-9 (the bar never takes focus) is
   the single most expensive invariant to break and is currently verified by hand
   (M-1, M-8).
4. **No load or endurance test.** Nothing verifies behaviour after 500 dictations in a
   session, or with a 50 MB `history.db`.
5. **No packaged-build test run.** The suite runs against source. The measured
   source-vs-exe divergences (stdout is `None`, `.env` discovery, resource paths) are
   documented but not asserted.
