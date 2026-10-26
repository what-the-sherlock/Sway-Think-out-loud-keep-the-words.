# Sway — Product Requirements Document

**Version:** 1.0 (consolidating shipped behaviour + remaining scope)
**Verified against:** `1e85b9f`, 2026-08-26. 1391 tests passing.
**Authority:** [`CLAUDE.md`](../CLAUDE.md) > [`PRODUCTION_BRIEF.md`](../PRODUCTION_BRIEF.md) > this document.
**Companion docs:** [Product Brief](product-brief.md) · [Technical Specification](technical-spec.md) · [API Reference](api-reference.md) · [Data & Privacy](data-and-privacy.md) · [Decision Log](decision-log.md) · [Test Plan](test-plan.md) · [Roadmap](roadmap.md)

---

## 1. Purpose and scope

This document specifies **what Sway must do**, in terms verifiable by a test or by a
person following a script. It covers the shipped product surface and the
requirements still open. It deliberately does not specify *how* — that is the
[Technical Specification](technical-spec.md).

Every requirement carries a status:

| Status | Meaning |
|---|---|
| **Shipped** | Implemented and covered by tests or a manual check |
| **Partial** | Implemented for the common path; a named gap remains |
| **Open** | Specified, not implemented |

---

## 2. Personas

### P1 — Maya, product lead (primary)

Writes 40+ Slack messages, a dozen emails, and two specs a week. Thinks out loud;
her first draft spoken is better than her first draft typed. Tried Windows Voice
Access, abandoned it inside a day because every "um" and every restarted sentence
survived into the message.

- **Needs:** text in the app she is already in; fillers gone; no window switching.
- **Fails if:** the tool is slower than typing the sentence, or she has to proofread
  every output.
- **Tell that it works:** she stops noticing she is using it.

### P2 — Dan, staff engineer (primary)

Dictates PR descriptions and design notes, types code. Lives in VS Code, Windows
Terminal, and GitHub in a browser. Deeply suspicious of a tool that rewrites his
words.

- **Needs:** *verbatim* in the terminal and the editor — a shell command that gets
  "helpfully" punctuated is worse than useless. Cleanup only where he wants prose.
- **Fails if:** the tool ever silently changes a technical term, a flag, or a number.
- **Tell that it works:** he stops checking the output before sending it.

### P3 — Priya, consultant, RSI (secondary)

Wrist injury; typing is limited by hours per day, not by speed. Uses her machine for
everything, not a curated subset of apps.

- **Needs:** it must work in *every* app, including ones nobody planned for. Needs
  hands-free (tap to start, tap to stop) because holding a key is itself a strain.
- **Fails if:** the tool works in three blessed apps and silently does nothing
  elsewhere.

### P4 — Sam, AI-heavy operator (secondary)

Writes prompts all day. The Prompt Engineer transform is why they installed it; the
dictation is a bonus.

- **Needs:** saved instructions with their own shortcuts, editable, exportable.
- **Fails if:** the instruction library gets expensive or slow as it grows.

---

## 3. Core user journeys

### J1 — First run *(Shipped)*

1. User double-clicks `Sway.exe`. No console window, no browser tab, no terminal.
2. If no API key is found, an onboarding screen appears — not a message box.
3. User pastes a Groq key; **Test key** makes one cheap call and reports OK or a
   specific failure.
4. The key is written to Windows Credential Manager. If a key was found in `.env`,
   it is *copied up* into the credential store and `.env` is left untouched.
5. Microphone permission and device selection are confirmed with a live level meter.
6. The user is shown the hotkey and prompted to try one dictation.

**Acceptance:** a machine with no `.env` and no prior install reaches a working
dictation entirely through the UI, and the key exists in Credential Manager and in no
plaintext file Sway wrote.

### J2 — Dictate into the focused app *(Shipped)*

1. User is in any application, cursor in a text field.
2. Holds Right Ctrl. Within one frame, the resting dot expands into a capsule showing
   **recording**, with a live level indication.
3. Speaks. Releases.
4. The bar shows **thinking**, then **injecting**, then a brief **done** state that
   reports a fact — words and milliseconds — and fades.
5. Cleaned text is at the cursor. The clipboard is back to what it was.

**Acceptance:** p50 from release to text under 1000 ms on a 5-second clip; the
foreground window is unchanged throughout; the clipboard is restored.

### J3 — Verbatim into a terminal *(Shipped)*

Same as J2, in Windows Terminal or VS Code. The transcript is pasted with **no
cleanup call** — the app profile is resolved from the focused process name at the
moment of the press.

**Acceptance:** no chat-completion request is made; the pasted text matches the raw
transcript exactly.

### J4 — Transform over a selection *(Shipped)*

1. User selects text in any app.
2. Presses a transform's shortcut (e.g. Ctrl+Alt+2 for *Turn into an email*).
3. The selection is read via a Ctrl+C round trip, one model call applies the stored
   instruction, and the result replaces the selection.

**Acceptance:** exactly one network call; the original clipboard is restored; if
nothing was selected, the bar reports it and no request is sent.

### J5 — Spoken transform mid-dictation *(Shipped)*

1. User dictates normally and ends with a trigger phrase — "…make that an email".
2. The phrase is matched locally, excised from the transcript, and its directive is
   appended to the cleanup request that was already going to happen.

**Acceptance:** one model call total, not two; the phrase does not appear in the
output; the raw pre-excision transcript is preserved in history.

### J6 — Snippet *(Shipped)*

User says a configured phrase ("my email address"). It is replaced locally with the
stored text, or with today's date, before any model call.

**Acceptance:** zero network calls attributable to the substitution; whole-phrase
word-boundary match only; longer phrases win over shorter ones that prefix them.

### J7 — Self-correction *(Shipped)*

User says "meeting at 7 — no, 8". The correction is detected **before** the model
call, which does two things: it overrides the skip heuristic (five words would
otherwise skip the model entirely), and it tells the output guardrail that the
retracted "7" is allowed to disappear.

**Acceptance:** the pasted text contains 8 and not 7; with `context_mode = "off"` the
literal transcript is pasted instead.

### J8 — Failure *(Partial)*

Any failure — no network, bad key, 429, Groq 5xx, timeout, no mic, mic held by
another app, elevated target window — produces a specific message on the bar and a
recorded run. The mic is never left hot.

**Acceptance:** each failure mode named in FR-8 has been deliberately induced and
produces its own message. **Gap:** no audio queue exists, so a dictation made while
offline is lost rather than retried (FR-8.6).

### J9 — Review and manage *(Shipped)*

User opens the app window from the tray or the bar menu. Home shows live status and
recent activity; History is searchable with copy/delete/delete-all; Dictionary,
Snippets, Transforms, Insights, and Settings each have a page.

---

## 4. Functional requirements

### FR-1 — Capture

| # | Requirement | Status |
|---|---|---|
| 1.1 | The microphone stream opens **once** at app start and stays open for the process lifetime. It is never opened on hotkey press. | Shipped |
| 1.2 | The audio callback only writes into a preallocated ring buffer: no I/O, no logging, no large allocation. | Shipped |
| 1.3 | Audio is captured at 16 kHz mono; the ring buffer holds `[audio].ring_seconds` (default 60 s) — the maximum length of a single recording. | Shipped |
| 1.4 | **Pre-roll:** audio from before the keypress (`[audio].preroll_ms`, default 300 ms) is included, so beginning to speak as you press does not clip the first syllable. Free, because the mic is always recording. | Shipped |
| 1.5 | WebRTC VAD trims leading and trailing silence, keeping `vad_padding_ms` on each side. A clip with no detected speech is dropped before encode — no upload, no quota spent. | Shipped |
| 1.6 | Audio is FLAC-encoded (lossless, several times smaller than raw PCM) for upload. | Shipped |
| 1.7 | Audio is **never written to disk** at any point in the pipeline. | Shipped |
| 1.8 | The input device is selectable by substring match; unset uses the system default. | Shipped |

### FR-2 — Activation

| # | Requirement | Status |
|---|---|---|
| 2.1 | A global hold-to-talk binding starts and ends dictation. Default Right Ctrl. | Shipped |
| 2.2 | Bindings may be a bare key, a modifier-only combination (`ctrl+cmd`), a chord (`ctrl+alt+p`), a mouse button (`mouse:middle`), or a chord ending in a mouse button. An action may hold **any number** of bindings. | Shipped |
| 2.3 | A hold shorter than `[hotkey].min_hold_ms` (default 200 ms) is treated as an accidental tap and discarded. | Shipped |
| 2.4 | **Hands-free:** tap to start, tap again to stop, optionally requiring a double tap. Off by default. | Shipped |
| 2.5 | A separate binding runs a voice command over the current selection. Default Right Shift — chosen because Ctrl/Alt are common accelerators and holding one risks losing the selection. | Shipped |
| 2.6 | Each transform may carry its own bindings; conflicts between transforms are detected and surfaced. | Shipped |
| 2.7 | Bindings are rebindable from the UI with conflict detection, including keys reserved by Windows. | Shipped |
| 2.8 | The hotkey library must not require administrator privileges (`pynput`, never `keyboard`). | Shipped |
| 2.9 | Releasing the key hands work to a worker thread immediately, so the listener is free for the next event before the pipeline finishes. | Shipped |
| 2.10 | **VAD endpointing** — end the recording on detected silence rather than on key release, opt-in, with a grace window when triggers are enabled. | Open |

### FR-3 — Transcription

| # | Requirement | Status |
|---|---|---|
| 3.1 | Transcription uses Groq `whisper-large-v3-turbo` over a single persistent HTTP/2 client, pre-warmed at startup. | Shipped |
| 3.2 | The personal dictionary biases Whisper's `prompt` parameter, ordered by pinning and by actual use, truncated to `max_prompt_chars` (default 200) — Whisper attends to roughly its last 224 tokens, so this is a real ceiling. | Shipped |
| 3.3 | Detected language is returned on every transcription and drives the language-aware filler check. Whisper's label is **discarded below ~2s of speech**, where it is close to random, in favour of the session's recent language. | Shipped |
| 3.4 | `temperature = 0`. A result the VAD says contains no speech is dropped rather than pasted (silence hallucination). | Shipped |
| 3.5 | Repeated-phrase loops in the transcript are detected and rejected. | Shipped |
| 3.6 | Terms dictated repeatedly can be learned into the dictionary automatically; learned terms are always visible and removable. | Shipped |
| 3.7 | **Chunked streaming** — overlapping 5-second windows shipped while the user still speaks, so release only transcribes the tail, with boundary dedup and graceful fallback. | Shipped, **default off** — see [Decision Log ADR-006](decision-log.md) |
| 3.8 | **STT model choice** is per language, not a user setting: `whisper-large-v3-turbo` by default, `whisper-large-v3` for languages the distilled decoder handles badly (Hindi, Marathi). Both are overridable in `[groq]`. Measured at +140ms median, paid only by those languages. | Shipped |
| 3.9 | **Marathi and Hindi are selectable.** Marathi previously existed in no list in the product, so a Marathi speaker was forced onto auto-detect — where turbo decodes Marathi through a Hindi prior and corrupts the transcript. | Shipped |

### FR-4 — Cleanup

| # | Requirement | Status |
|---|---|---|
| 4.1 | A chat-completion pass removes filler words and self-corrections and fixes grammar and punctuation. It **never** answers the content, summarises it, or adds commentary, and never translates **unless the user has set an output language** (FR-4.12). | Shipped |
| 4.2 | The style is chosen from the focused process at press time: casual for chat apps, formal for mail clients, **verbatim (no call at all)** for terminals and editors. | Shipped |
| 4.3 | Users can override the style per application. | Shipped |
| 4.4 | **Skip heuristic:** transcripts under `skip_min_words` (default 8) with no filler markers bypass the model. The threshold is **scaled per language** — Hindi and Marathi carry the same meaning in ~⅔ the whitespace tokens, so an unscaled threshold skipped cleanup for essentially every Indic dictation. Skip rate is logged. | Shipped |
| 4.5 | A transcript Whisper itself was unsure of skips the model (`skip_when_unconfident`). | Shipped |
| 4.6 | A detected self-correction **overrides** 4.4 and 4.5 — those are exactly the cases they get wrong — but never overrides `enabled = false` or a verbatim profile. | Shipped |
| 4.7 | Filler removal is user-switchable, for users who want a closer record of what they actually said. | Shipped |
| 4.8 | **Guardrails:** a rewrite that grew or shrank far beyond a tidy-up, emptied itself, or lost a protected entity (name, number, date) is discarded and the raw transcript is pasted instead. Entities the speaker themselves retracted are exempt. | Shipped |
| 4.9 | The response is streamed, and time-to-first-token is measured separately from total. | Shipped |
| 4.10 | `max_output_tokens` caps a runaway generation so it cannot blow the latency budget. | Shipped |
| 4.11 | The cleanup prompt lives in a **versioned file in the repo** so it can be diffed. | Open — it is currently a module-level constant in `sway/cleanup.py` |
| 4.12 | **Output language.** The user may set the language the pasted text ends up in (default: whatever they spoke, which is not a translation). It forces the model call through the skip heuristics, never applies under a verbatim profile, and is the only thing permitted to vary the language guardrail — which then becomes stricter, not weaker. | Shipped |
| 4.13 | **Native register.** Per-language prompt guidance keeps everyday English loanwords (`meeting`, `reschedule`, `client`) in Latin script rather than substituting formal coinages, and prefers colloquial verb forms. Measured by `scripts/eval_languages.py --live`. | Shipped |
| 4.14 | **Script preservation.** When nothing is being translated, the reply stays in the script the speaker used — romanized Hinglish is not converted to Devanagari. | Shipped |
| 4.15 | When an output language is set but the transcript is **not confident**, nothing is translated and the raw words are pasted (`language_uncertain`). This outranks the 4.6 self-correction override. | Shipped |

### FR-5 — Injection

| # | Requirement | Status |
|---|---|---|
| 5.1 | Save the existing clipboard, set ours, send Ctrl+V, restore the original after `restore_delay_ms` (default 150 ms). **Never simulate typing.** | Shipped |
| 5.2 | The restore is skipped if the clipboard changed in the meantime, so raising the delay only ever costs a longer hold. | Shipped |
| 5.3 | If the focused window is confidently running elevated, Windows silently drops synthetic input into it — so Sway detects this beforehand, leaves the text on the clipboard, and says "elevated window — copied instead" rather than appearing to do nothing. | Shipped |
| 5.4 | The bar must **never** take focus, even momentarily, or the paste lands in the bar instead of the target application. | Shipped |
| 5.5 | The last transcript can be re-pasted or copied from the bar menu. | Shipped |
| 5.6 | **Incremental injection** — benchmark before building; build only if it wins against a single paste of the complete response. | Open — evidence to date argues against it |

### FR-6 — Instructions (snippets, transforms, commands)

| # | Requirement | Status |
|---|---|---|
| 6.1 | A **snippet** replaces a spoken phrase with stored text or today's date, resolved entirely locally, before any model call. | Shipped |
| 6.2 | Matching is whole-phrase and word-boundary anchored — never a substring. Longer phrases are matched first. Trailing punctuation Whisper adds is stripped before comparison. | Shipped |
| 6.3 | The raw pre-substitution transcript is always preserved alongside the result. | Shipped |
| 6.4 | A **transform** is a saved instruction, runnable by keyboard shortcut over a selection *or* by spoken phrase mid-dictation. | Shipped |
| 6.5 | A spoken transform costs **one model call total**, merged into the cleanup request. Never one call per fired transform. | Shipped |
| 6.6 | The model never sees the transform library — only the directives that actually fired. | Shipped |
| 6.7 | Transforms carry output expectations (`may_shorten`, `may_lengthen`) so the guardrails do not reject a correct summary or a correctly lengthened email. | Shipped |
| 6.8 | Sway ships a default set (Prompt Engineer, Turn into an email, Summarize, Professional, Polish, Concise, Clarity), each editable and deletable, with a reset-to-defaults. | Shipped |
| 6.9 | Transforms are editable from the UI: name, instruction, description, phrase, shortcuts, example before/after. Limits: 100 transforms, 8000-character instruction. | Shipped |
| 6.10 | A transform preview shows a word-level before/after diff while nothing has been pasted yet. | Shipped |
| 6.11 | A **voice command** applies a spoken instruction to the current selection (two calls: transcribe, then apply). | Shipped |
| 6.12 | **Test bench** — paste a sample utterance and see which triggers fire, the excised transcript, the resolved directives, estimated token cost, and the final output, without dictating into a real app. | Open |
| 6.13 | **Escape hatch** — a literal prefix ("literally…", "quote…") forces a phrase through as text. | Open |
| 6.14 | **Import/export** of the transform library. | Open (the dictionary has import/export; transforms do not) |

### FR-7 — Surfaces

| # | Requirement | Status |
|---|---|---|
| 7.1 | **The bar** rests as a ~14px dot and expands into a capsule when engaged, as a single interpolated render rather than two widgets swapped. States: idle, recording, thinking, injecting, done, error, paused, plus transient coaching. | Shipped |
| 7.2 | The bar carries the two controls people change mid-dictation — language and microphone — each answered in place by a popover, not by navigating away. | Shipped |
| 7.3 | The bar menu offers pause/resume, hide, settings, history, re-paste, and copy. | Shipped |
| 7.4 | Bar position is configurable (top/bottom, centre/right). | Shipped |
| 7.5 | Motion respects the OS "reduce animation" accessibility setting, with an explicit override. | Shipped |
| 7.6 | **The window** wraps the dashboard in a real OS window — taskbar entry, app icon, no address bar, no tabs — falling back to the default browser when `pywebview`/WebView2 is unavailable. | Shipped |
| 7.7 | Window pages: Home, History, Dictionary, Snippets, Transforms, Insights, Settings, About. | Shipped |
| 7.8 | **The tray icon** reflects state and offers the menu; if the tray backend cannot start, the app logs it and keeps running. | Shipped |
| 7.9 | **The CLI** (`sway run / devices / dictionary / history / demo / report / config`) keeps working; `python -m sway run` from an editor must never break. | Shipped |
| 7.10 | Closing the main window hides to tray. Quit is explicit. | Shipped |
| 7.11 | A second launch focuses the running instance rather than starting a rival hotkey hook; a hung first instance must not hang the second one. | Shipped |

### FR-8 — Errors and reliability

| # | Requirement | Status |
|---|---|---|
| 8.1 | Every network path has a timeout and one retry with jitter. | Shipped |
| 8.2 | 429s are **not** retried; the specific limit hit is read from the response headers and surfaced. The free tier's 30 req/min is too small to burn on blind retries. | Shipped |
| 8.3 | Each failure mode produces its own user-visible message: no network, invalid key (no retry on 401), 429, Groq 5xx, timeout, no mic, mic held by another app, elevated target. | Partial |
| 8.4 | The app never hangs with the microphone hot. A failed run is recorded but excluded from timing statistics. | Shipped |
| 8.5 | A top-level crash handler logs the traceback, notifies, and shuts down cleanly — never a zombie hotkey hook. | Shipped |
| 8.6 | **Never lose a recording** — queue the FLAC to `<data_dir>/queue/` on failure, retry, and allow a re-run from history, with a hard cap and eviction. | Open |
| 8.7 | **Whole-pipeline timeout budget** — on exceed, inject the raw transcript rather than nothing. | Open |
| 8.8 | A corrupt `settings.json` must never brick startup; an out-of-range value is replaced individually rather than discarding the whole file. | Shipped |
| 8.9 | Quit leaves zero processes, no listening socket, and no hotkey hook. | Shipped |

### FR-9 — Dashboard and metrics

| # | Requirement | Status |
|---|---|---|
| 9.1 | **Real data only.** No fabricated metric. A metric that cannot be measured shows an explicit "not available yet". | Shipped |
| 9.2 | Anything assumption-derived (time saved) is labelled an estimate in the UI. | Shipped |
| 9.3 | Agent status is big and unambiguous: running / paused / error, with the last error and when. | Shipped |
| 9.4 | Counters: dictations today/this week/total, words dictated, estimated time saved, streaks. | Shipped |
| 9.5 | Latency: p50 and p95 end-to-end plus a per-stage breakdown, so it is visible whether STT, the LLM, or injection is the bottleneck. | Shipped |
| 9.6 | Rows written before a stage existed must render as **absent**, never as 0 ms. Folding a missing stage in as zero would drag every percentile down and invent an improvement. | Shipped |
| 9.7 | Errors are grouped by type with a one-line cause, not dumped as a log. | Shipped |
| 9.8 | History is searchable with copy, delete, and a delete-all that actually wipes the rows. | Shipped |
| 9.9 | Understanding metrics: corrections detected, transforms fired, snippets substituted, model calls avoided. | Shipped |
| 9.10 | **Groq free-tier usage from response headers**, which are the source of truth. The published 30/min and 2000/day numbers are a *labelled display fallback only*, used before the first request of a session and never computed against when headers are available. | Open |
| 9.11 | Live updates without a manual refresh. | Partial (polling) |
| 9.12 | **Trigger measurement:** prompt/completion tokens per request, share of utterances resolved with no model call, detection time, and a manually flaggable false-positive counter. | Partial — model-call avoidance is measured; token counts and the false-positive counter are not |

### FR-10 — Settings and credentials

| # | Requirement | Status |
|---|---|---|
| 10.1 | The API key lives in Windows Credential Manager. Resolution order: keyring → `SWAY_GROQ_API_KEY` → `GROQ_API_KEY` from the environment or `.env`. | Shipped |
| 10.2 | A key found only in `.env` is copied up into the keyring on first run and `.env` is **left untouched**. | Shipped |
| 10.3 | A missing or broken credential store degrades to the environment path. The app must never fail to start because a credential *store* is unavailable. | Shipped |
| 10.4 | **Test key** makes one cheap call and reports OK or a specific failure. | Shipped |
| 10.5 | Settings persist to `<data_dir>/settings.json`, written atomically (temp file + replace) so an interrupted write leaves the previous file intact. | Shipped |
| 10.6 | Unknown keys written by a newer build round-trip untouched rather than being deleted on the next save. | Shipped |
| 10.7 | Every setting either applies without a restart or says plainly that it needs one. | Shipped |
| 10.8 | The key is never rendered, logged, or placed in the address bar. `Secret` refuses to render itself in logs, reprs, or tracebacks. | Shipped |

### FR-11 — Privacy

| # | Requirement | Status |
|---|---|---|
| 11.1 | No telemetry of any kind leaves the machine. | Shipped |
| 11.2 | Nothing but the two Groq calls crosses the network. | Shipped |
| 11.3 | No audio is persisted anywhere. The only specified exception is the FR-8.6 retry queue, which is bounded, enumerable, and wipeable. | Shipped (queue not built) |
| 11.4 | No transcript text is written to `latency.db` or to the log file. Verified as an explicit audit item. | Shipped |
| 11.5 | The dashboard binds loopback only, on an ephemeral port, behind a per-launch token, with origin checks on every write. | Shipped |
| 11.6 | `instance.json` holds a live token: owner-only ACL, rewritten never appended, removed on clean shutdown, and a stale file is treated as absent. | Shipped |
| 11.7 | A documented uninstall/reset removes settings, the keyring entry, all three databases, the queue directory, `instance.json`, and the Run registry key. | Partial |

---

## 5. Settings inventory

Everything a normal user can change, with no TOML editing and no environment
variables. `None` consistently means "not overridden — use whatever `config.toml`
resolved", which is what lets a developer's config keep working untouched.

| Group | Setting | Values | Default |
|---|---|---|---|
| Activation | Dictation bindings | any number of key/chord/mouse bindings | `ctrl_r` (from config) |
| Activation | Command bindings | as above | `shift_r` (from config) |
| Activation | Hands-free on/off | bool | off |
| Activation | Hands-free bindings, double-tap | bindings, bool | from config |
| Audio | Input device | substring, or system default | system default |
| Voice | I'm speaking | ISO-639-1 hint, or auto-detect | auto |
| Voice | Write it in | ISO-639-1, or same as spoken | same as spoken |
| Voice | Understanding (correction resolution) | `off` / `conservative` / `balanced` | `conservative` |
| Voice | Remove fillers | bool | on |
| Voice | Auto-learn vocabulary | bool | on |
| Appearance | Bar position | top-center / bottom-center / top-right / bottom-right | top-center |
| Appearance | Reduced motion | auto / on / off | auto |
| System | Launch at login | bool | off |
| Data | History retention | days, 0 = keep forever | 90 |
| Data | Latency retention | days, 0 = keep forever | 30 |
| Content | Snippets | list | empty |
| Content | Transforms | list | 7 shipped |
| Content | Per-app rules | list | empty |
| Content | Dictionary | list, with pinning | empty |

Ceilings exist to stop a hand-edited file making the app unusable, not to second-guess
real input: 100 transforms, 8000-character instruction, 4000-character example,
200-character description.

---

## 6. Non-functional requirements

| # | Requirement | Target | Status |
|---|---|---|---|
| NFR-1 | End-to-end p50, key release → text at cursor | < 1000 ms | **623 ms** measured |
| NFR-2 | End-to-end p99 | < 1000 ms | **1479 ms** — not met; cleanup owns the tail |
| NFR-3 | Cold start to armed hotkey | < 2 s | met (warm runs match source; first exe run 1249 ms on a 92 MB bundle) |
| NFR-4 | Resting CPU with the bar idle | negligible | bar repaints at 150 ms when resting, ~60 fps only while something moves |
| NFR-5 | Bundle size | reported, and shrinking | 92 MB; numpy accounts for 21 MB and is used in one place |
| NFR-6 | Free-tier headroom | never exceed 30 req/min, 2000 audio req/day | streaming off by default; ~54% of cleanup calls skipped |
| NFR-7 | Dependencies | every one justified in a comment before it is added | enforced in `pyproject.toml` |
| NFR-8 | Platform isolation | every OS-specific call behind `PlatformAdapter` | Windows real, POSIX best-effort |
| NFR-9 | Test coverage | pure transcript/format/config functions, stores, and trackers — never the audio callback | 1391 tests |

---

## 7. Out of scope

Restating [Product Brief §9](product-brief.md) as hard boundaries for this release:
no server component, no FastAPI, no public listener, no database beyond SQLite, no
Docker, no Electron, no Node toolchain, no telemetry, no local models, no macOS or
Linux build, no meeting transcription, and no features pulled forward from a later
phase.

---

## 8. Open questions

1. **Does the cleanup call earn its keep on the ~46% of dictations that reach it?**
   A skip removes the whole 238 ms span rather than concealing it, which makes the
   heuristic a cheaper lever than any streaming work. Needs the eval set to answer.
2. **Which STT model should be the default?** The three-model benchmark (FR-3.8) is
   the only lever on the largest single stage, and it moves the median rather than
   the tail.
3. **Does Groq prompt caching apply to the cleanup model?** If it does, the
   stable-prefix prompt ordering already in place is what makes it pay, and the
   saving should be reported.
4. **What is the real trigger false-positive rate?** Unmeasurable until FR-9.12's
   counter exists. Until then the mitigation is structural (whole-phrase matching)
   rather than evidenced.
5. **Should the recording queue (FR-8.6) exist at all, given the no-audio-persisted
   rule?** It is a specified, bounded exception — but it is the only place audio ever
   touches disk, so the lifecycle has to be exactly right before it ships.
