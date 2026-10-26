# Sway — Decision Log

Architecture decision records. Each entry states the decision, what was actually
weighed, and — where one exists — the measurement that settled it. An ADR is amended,
never quietly rewritten; if a decision is reversed, the reversal gets its own entry.

**Verified against:** `1e85b9f`, 2026-08-26.

---

## ADR-001 — Paste the clipboard; never simulate typing

**Status:** Accepted, from Phase 1. Unchanged.

**Decision.** Text reaches the cursor by saving the clipboard, setting ours, sending
Ctrl+V, and restoring the original after 150 ms.

**Why not simulate keystrokes.** Per-character injection is unreliable across apps
with input masking, autocomplete, formatting, and IME handling — and it is
proportionally slower with length, which puts a variable cost on the one number the
product is built around. Every fast dictation tool, including the Wispr Flow
benchmark, pastes.

**Consequences.** The clipboard is briefly clobbered, so the restore matters. It is
**skipped if the clipboard changed** in the meantime, which means raising the delay
for slow Electron apps (Slack, Notion, Teams) only ever costs a longer window of
holding the dictated text, never a lost copy. It also means the bar must never take
focus — see ADR-004.

---

## ADR-002 — The microphone opens once, at startup, forever

**Status:** Accepted, from Phase 1. A hard constraint in `CLAUDE.md`.

**Decision.** `sounddevice.RawInputStream` opens in `Engine.start()` and stays open
for the process lifetime, writing into a preallocated ring buffer. The hotkey never
opens a stream.

**Why.** Opening an audio stream costs real time from a latency budget of 1000 ms
total, and it costs it at exactly the wrong moment — while the user is already
speaking.

**Consequences.** The audio callback is on the hot path of the OS audio thread, so it
does a bounds-checked memcpy and nothing else: no I/O, no logging, no meaningful
allocation. It also makes **pre-roll free**: 300 ms of audio from *before* the
keypress is already in the buffer, so beginning to speak as you press does not clip
the first syllable, and the VAD trims it off again when it was silent.

---

## ADR-003 — Build the measurement harness before the pipeline

**Status:** Accepted, Phase 0.

**Decision.** `LatencyTracker` + SQLite + a formatted console table were built and
proven against a stub pipeline before any real stage existed.

**Why.** "Lowest end-to-end latency is the highest priority" is unfalsifiable without
a way to falsify it. Every optimisation since has had to show a before/after table,
and several have been reverted on the evidence.

**What it caught.** The `cleanup` span originally wrapped the skip branch as well as
the model call, so 13 of 46 spans were 0 ms skips. Blending them **inverted the
signal**: improving the skip rate made the cleanup percentile drop without the model
call getting any faster, so a change that only tightened the heuristic would have
shown up as a latency win in a stage it never touched. Fixed by opening the span only
when the model is actually called and recording skips as metadata.

**Standing rule.** A stage missing from an older run renders as **absent**, never as
0 ms. Folding a missing stage in as zero manufactures an improvement that never
happened.

---

## ADR-004 — The bar is PIL-composited Tk, not a WebView

**Status:** Accepted, D2.

**Decision.** The floating bar is a tkinter window with every pixel composited by
Pillow, shaped with `-transparentcolor`, and flagged
`WS_EX_TRANSPARENT | WS_EX_NOACTIVATE`.

**Why not a WebView.** A WebView window would have been dramatically easier to style,
and it is disqualified for a functional reason, not a stylistic one: **a webview
window activates when shown.** Paste targets the *foreground* window (ADR-001), so a
bar that steals focus mid-dictation sends the user's words into the bar instead of
their document — a silent, intermittent, miserable bug. Non-activation is not
optional and is covered by an explicit test that the foreground window is unchanged
across a full show → waveform → hide cycle.

**Consequences.** Everything is drawn at 4× and downsampled, because PIL's primitives
are hard-edged and this renders at 14–44 px where that looks cheap. Per-monitor DPI
awareness must be enabled at import time, before any window exists, or Windows
bitmap-scales the window and blurs exactly the anti-aliasing this exists to produce.
The frame rate is adaptive — ~60 fps only while something moves, 150 ms when resting —
because the repaint sits inside the latency budget and the resting cost matters more
than the peak.

**Related.** One renderer spans all three tiers: `render_bar(expand)` where the
resting dot is simply the capsule at `expand=0`. That makes the transition a morph
rather than a swap, and leaves one layout to reason about instead of two that must be
kept in visual agreement.

---

## ADR-005 — Keep the loopback socket; do not use the pywebview JS bridge

**Status:** Accepted, D2. Explicitly decided rather than defaulted.

**Decision.** The window wraps the existing loopback HTTP server. It does not talk to
Python over pywebview's JavaScript bridge.

**Why.** The socket is already built, tokened, and origin-checked, so the bridge would
be a rewrite of the data layer for no user-visible gain. Three further reasons:

1. It preserves opening the dashboard in a real browser with devtools during
   development, which a webview cannot offer.
2. Live updates want SSE or polling, which a socket does naturally and a bridge does
   awkwardly.
3. The second-launch focus path already depends on the socket.

**Constraint that survives.** The user must still never see a URL, browser chrome, or
a port number.

---

## ADR-006 — Chunked streaming ships, but defaults **off**

**Status:** Accepted on measurement. Reverses the Phase 4 assumption.

**Decision.** `[streaming].enabled = false`.

**The measurement.** Groq's `whisper-large-v3-turbo` returns in roughly the same time
whether the clip is 2 seconds or 23 — p50 405 ms, p99 585 ms, max 585 ms across the
historical set, an unusually tight distribution. Chunking therefore removes almost no
tail latency while costing **2–4× the audio requests** against a 2000/day free-tier
budget.

**Why keep the code.** It is correct, it is tested, and the measurement is
environment-dependent: a user on a slower link may genuinely benefit. The flag is
documented with the reasoning so anyone flipping it knows what they are buying.

**The general lesson.** `transcribe` is a **floor, not a tail**. STT cannot be made
less variable, only lower — and only by changing model. That makes the STT model
benchmark the only lever on the largest single stage, and it moves the median rather
than the tail.

---

## ADR-007 — One model call, always

**Status:** Accepted, D8.3. Non-negotiable.

**Decision.** Cleanup and every fired transform directive merge into a **single**
chat-completion request. Never one call per instruction.

**Why.** Chaining multiplies both latency and the free-tier quota, and the free tier
is 30 requests per minute org-wide. Two instructions on one utterance would double a
238 ms p50 stage and halve the number of dictations possible in a minute.

**What makes it possible.** The type system exists so most triggers never reach the
model at all: `deterministic` and `macro` actions are pure local string operations
(zero tokens, zero latency); `mode` adds one short directive line; `transform` is the
only type that genuinely needs the model, and it is meant to be rare.

**The corollary that keeps it cheap as the library grows.** *The model never sees the
trigger library.* It sees only the directives that actually fired — normally none,
occasionally one or two — resolved from precompiled strings held in memory. Directives
are written once when the user saves the transform, not generated at request time.

**Prompt ordering.** Stable part first (system prompt + cleanup rules, byte-identical
across requests), variable part last (directives, app label, transcript). If Groq
prompt caching applies to the cleanup model, this ordering is what makes it pay.

---

## ADR-008 — Guardrail the model's output against the raw transcript

**Status:** Accepted, Phase 2, extended in D4 and with transforms.

**Decision.** Everything the system prompt asks for — rewrite only, never answer,
never translate, never add markdown — is **re-checked in code** against the raw
transcript. Any failure discards the rewrite and pastes the raw transcript.

**Why.** An instruction-following model is not a contract. An 8B-class model asked to
tidy "should we ship Tuesday?" will sometimes answer the question instead.

**Checks.** Length (grew or shrank far beyond a tidy-up), entities (names, numbers,
dates present in the transcript must survive), emptiness, and repeated-phrase loops.

**Amendment (multilingual).** The language check now has exactly one way to be
varied, and the principle is unchanged rather than weakened. A transform's own
settings (`OutputExpectation`) still cannot touch it — that is asserted by a
dedicated regression test. Only a *stored user setting*, an explicit output
language, can, and it arrives by a separate argument (`LanguageIntent`) that
nothing model-facing can construct. When it is set the check **inverts and
tightens**: instead of "no script may disappear" it becomes "the output must be
in the target's script, and every script present must be accounted for". That
catches two failures the original could not see at all — the model answering in
the source language, and the model writing the right language in romanized
Latin. Known gap: Hindi and Marathi share a script, so a translation into the
wrong one of the two is invisible to a script-level check.

**The interesting interaction.** The entity check treats every number as protected —
which means the *correct* rewrite of "meeting at 7, no, 8" looks exactly like the
model having lost the 7. That is why the correction scanner exists as a **router, not
a rewriter**: it never deletes a word, it exists to tell the guardrail which entities
the speaker themselves withdrew. Transform output expectations (`may_shorten`,
`may_lengthen`) play the same role for Summarize and the email transform.

**Asymmetry that sets the defaults.** A missed correction leaves the user with the
literal transcript — annoying, visible, fixable by hand. A false positive silently
deletes something they meant to say. The two are not equally bad, and the thresholds
reflect that.

---

## ADR-009 — Two configuration layers, not one

**Status:** Accepted, D5.

**Decision.** `sway.config` (TOML + env, frozen, read once, unknown keys are a hard
error) and `sway.settings` (`settings.json`, user-editable from the UI) are separate
layers. `apply_to_config()` is the single place the overlay happens, and it overrides
only the specific fields listed in `Settings`.

**Why not extend config.** They serve different people under different rules. A
developer editing TOML expects it to win and expects a typo to be an error. A normal
user editing from the UI needs the opposite: a corrupt file must **never brick
startup**, an out-of-range value is replaced individually rather than discarding the
whole file (losing one bad field is recoverable; losing an entire transform library
because a boolean was misspelled is not), writes must be atomic because they happen
while the app is running, and unknown keys from a newer build must round-trip rather
than being deleted on the next save.

---

## ADR-010 — onedir, not onefile

**Status:** Accepted, D7.

**Decision.** PyInstaller onedir into `dist/`, two executables sharing one COLLECT:
windowed `Sway.exe` and console `sway-cli.exe`.

**The measurement.** Source `sway config`: 376/293/279 ms. Exe `sway-cli config`:
1249/298/379 ms. The 1249 ms is first-touch disk on a 92 MB bundle; warm runs match
source.

**Why it settles it.** onefile self-extracts to `%TEMP%` on **every** launch and pays
that cost each time. onedir pays it once. The under-2-second startup target is
currently met.

**Consequence worth knowing.** `build\` contains a `Sway.exe` that is only a
bootloader stub — double-clicking it fails with *"Failed to load Python DLL
python311.dll"*, because the `_internal\` runtime it needs went to `dist\`. It sits
one folder away and looks identical in Explorer, so the build script deletes it and
leaves exactly one working executable on disk.

---

## ADR-011 — stdlib `http.server`, not a framework

**Status:** Accepted, Phase 6. `CLAUDE.md` rules out FastAPI outright.

**Decision.** `ThreadingHTTPServer` on a daemon thread, binding `127.0.0.1` on an
ephemeral port.

**Why.** The job is to hand a browser three static files and answer a dozen read-only
JSON routes for one user on loopback. A framework would add a dependency and a startup
cost for nothing. Safety comes from three cheap layers instead: loopback binding, a
per-launch token compared in constant time, and origin checks on writes.

**Related decision.** Search in `history.db` is a plain `LIKE` scan, not FTS5 — a
personal history is at most a few thousand rows, far below where FTS would matter, and
this avoids depending on an SQLite build option that is not guaranteed present
everywhere.

---

## ADR-012 — Incremental injection: measure first, and the measurement says no

**Status:** Open, with the evidence currently against building it.

**The proposal.** Paste the cleanup response as it streams, so the user sees text
sooner.

**Why the evidence is against it.** On real model calls, input length versus duration
gives r = +0.385, **r² = 0.149** — length explains about 15% of cleanup's variance.
The slowest call (1118 ms) had 260 input characters; an 888-character input finished
in 625 ms. The delay is therefore largely *before the first token*, which is precisely
where incremental injection has nothing to hide. It would conceal a wait that is not
happening while the tokens stream.

**The cost side.** Injection is clipboard-paste (ADR-001), so incremental means
repeated Ctrl+V into the target app: visually noisy, and it shreds undo granularity in
most editors.

**What the evidence points at instead.** A whole-pipeline timeout budget with
raw-transcript fallback, and a tighter skip heuristic. A skip removes the whole 238 ms
span rather than concealing it.

**Independent complication.** Incremental injection must be suppressed whenever a
transform fired or a correction was detected — you cannot re-frame text already
pasted, and "no, scratch that" needs a whole-utterance view. That is a branch rather
than a rollback (both are known before the model call), but it narrows the cases where
the win would apply even if the win existed.

---

## ADR-013 — Zero-cost triggers are matched whole-phrase, longest-first

**Status:** Accepted, D8.2.

**Decision.** Trigger matching is word-boundary anchored and whole-phrase only —
never a substring. Longer phrases are matched before shorter ones. Trailing
punctuation is stripped before comparison, because Whisper punctuates aggressively
and "my email." arrives with a full stop the user never typed into the trigger. The
raw pre-substitution transcript is always returned alongside the result.

**Why each part.** Substring matching would let "date" inside "candidate" fire a
trigger. Longest-first stops "my email address" being pre-empted by "my email".
Preserving the raw transcript is what makes a false positive *recoverable* rather than
a silent deletion.

**Deliberately not built yet.** An Aho–Corasick automaton. A precompiled regex
alternation, rebuilt only when the trigger list changes, is fast enough at tens of
triggers and is far less code. Reaching for the automaton (or `pyahocorasick`, a
C-extension dependency with its own bundle cost) requires a measurement showing the
regex is actually the bottleneck.

---

## ADR-014 — The model is configuration, not a constant

**Status:** Accepted. Forced by events.

**What happened.** Groq retired `llama-3.1-8b-instant`. It began returning 404, which
took the entire cleanup stage down. `openai/gpt-oss-20b` replaced it on the same free
tier.

**Decision.** `[cleanup].model` and `[commands].model` are configuration values.
`reasoning_effort = "low"` is likewise configurable, and `""` sends nothing at all for
models that do not support the parameter — measured at roughly 2× faster than the
model default for identical rewrites.

**Still missing.** A startup health check would have caught this before the user's
first dictation of the day did. It does not exist yet.

---

## ADR-015 — VAD endpointing and trigger detection collide, and endpointing yields

**Status:** Specified, not yet implemented (PRD FR-2.10).

**The collision.** The strongest false-positive filter for a spoken trigger is *"a
trigger preceded by a pause"*. But endpointing ends the recording **on** a pause. So
"…and that's the plan. [pause] frame it better" is precisely the utterance endpointing
would truncate before the trigger ever arrives.

**Decision.** When endpointing is enabled **and** any trigger is enabled, endpointing
does not finalize on the first qualifying silence. It opens a grace window (default
600 ms, configurable) and keeps capturing. If speech resumes within it and the new
segment's leading words match a trigger prefix, capture continues to the real end of
speech; otherwise the utterance finalizes at the original silence boundary and the
grace-window audio is discarded.

**Non-negotiable rider.** Every grace-window extension is logged as a latency stage,
so its cost is visible rather than assumed.
