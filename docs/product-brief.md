# Sway — Product Brief

**Status:** internal, living document. Companion to [`CLAUDE.md`](../CLAUDE.md) and
[`PRODUCTION_BRIEF.md`](../PRODUCTION_BRIEF.md), which win on any conflict.
**Last verified against the tree:** 2026-08-26 (`1e85b9f`), 1391 tests passing.

---

## 1. In one sentence

Sway is a Windows desktop app that turns speech into finished, correctly punctuated
text at your cursor in whatever application already has focus — in a fraction of a
second, without you switching windows, copying anything, or thinking about the tool.

## 2. The problem

Dictation has been technically solved for years and is still barely used, because
every available shape of it breaks the thing it was supposed to help with: flow.

| Existing shape | Why it fails in practice |
|---|---|
| **OS dictation** (Windows Voice Access, macOS) | Transcribes literally. "Um, so I think we should — no, actually, let's ship Tuesday" arrives verbatim. The user now edits, which costs more than typing did. |
| **Browser/web transcription tools** | Wrong window. You speak in the tool, then copy, then switch, then paste. Four context switches per thought. |
| **Whisper in a terminal / script** | Correct output, but a workflow: record a file, run a command, find the text. Nobody uses this mid-sentence. |
| **Meeting-notes AI** | Optimised for after the fact, not for the moment you are trying to write something. |

The common failure is not accuracy. It is **latency plus literalness**. A tool that
returns exactly what you said, one second too late, is not a writing tool — it is a
transcript.

## 3. What Sway does differently

Three commitments, each of which shows up as an architectural constraint rather than
a feature bullet:

**1. It is invisible until you hold a key.** No window to focus, no field to click
into. The microphone is already open — opened once at startup into a ring buffer,
never on keypress, because opening a stream costs a chunk of the budget the product
does not have. The text lands where your cursor already is, via clipboard paste,
never simulated typing.

**2. It returns what you meant, not what you said.** A second model pass strips
fillers, resolves self-corrections ("meeting at 7 — no, 8" → "meeting at 8"), and
fixes punctuation, while being explicitly forbidden from answering, summarising, or
adding anything. That pass is guarded: a rewrite that grew, shrank, or lost a name,
number, or date relative to the raw transcript is discarded and the raw transcript is
pasted instead. The user always gets their own words; the worst case is that they get
them untidied.

**3. It adapts to where the text is going.** The focused process is read at the
moment the hotkey is pressed. Chat apps get a casual rewrite, mail clients get a
formal one, and a terminal or code editor gets **no model call at all** — a shell
command must arrive exactly as spoken.

## 4. Beyond dictation: the instruction layer

Dictation is the floor, not the product. Sway also runs **saved instructions** over
text, by routes that cost very different things:

- **Transforms over a selection** (keyboard shortcut). Select text anywhere, press
  the transform's chord, and the selection is replaced. Ships with Prompt Engineer,
  Turn into an email, Summarize, Professional, Polish, Concise, and Clarity. One
  network call — the instruction comes from storage, not from your voice.
- **Transforms spoken mid-dictation** (phrase). "…make that an email" is matched
  locally, excised from the transcript, and its directive is folded into the cleanup
  request that was going to happen anyway. **One model call, always** — never one
  call per instruction.
- **Snippets** — a spoken phrase becomes a stored block of text, or today's date.
  Matched locally, before any model call: zero tokens, zero network, zero latency.
- **Voice commands over a selection** — select text, hold the command key, speak an
  instruction. Two calls (transcribe the instruction, then apply it), which is why a
  saved transform is the cheaper form of the same idea.

The design rule underneath all of these: *the model never sees the instruction
library.* It sees only the directives that actually fired, which is normally none.
That is what keeps the app inside a free tier while the library grows.

## 5. Who it is for

**Primary — the person who thinks faster than they type.** Product managers,
founders, consultants, engineers writing Slack messages, PR descriptions, and emails.
They already know what they want to say. Typing is the bottleneck, and their previous
dictation experiment failed on filler words.

**Secondary — the person with a physical reason to prefer voice.** RSI, a wrist
injury, a temporary constraint. They need the tool to work in *every* app, not a
blessed few — which is exactly what "paste into the focused window" buys.

**Secondary — the heavy AI-tool user.** They spend the day writing prompts. The
Prompt Engineer transform turns a rambling spoken idea into a structured prompt in one
keystroke. That is a different job from dictation and arguably the sharpest wedge in
the product.

**Explicitly not the target (for now):** teams, enterprises, meeting transcription,
non-Windows users. See §9.

## 6. Positioning

> **Wispr Flow is the benchmark for feel and latency.** Sway's answer is not "the
> same but cheaper" — it is "the same feel, with your data on your machine, and an
> instruction layer you own."

| | Sway | Wispr Flow | OS dictation |
|---|---|---|---|
| Text lands in the focused app | Yes | Yes | Yes |
| Removes fillers / self-corrections | Yes, guarded | Yes | No |
| Per-app tone, incl. verbatim for terminals | Yes | Partial | No |
| User-defined instructions with shortcuts | Yes | Limited | No |
| Data at rest | Local SQLite only | Vendor cloud | Vendor cloud |
| Telemetry | None | Yes | Yes |
| Cost | Free tier (Groq) | Subscription | Free |
| Platform | Windows | Windows/macOS | Per-OS |

The honest weakness: Sway is **not offline**. Two Groq calls per dictation cross the
network. The positioning is "local-first, not local-only" — audio is never persisted,
no telemetry is emitted, and every artefact of your use lives on your own disk — but
the audio does reach an API. Claiming otherwise would be a lie the architecture does
not support.

## 7. Product principles

1. **Latency is a feature, and it is the first one.** Target p50 under 1000 ms from
   key release to text at the cursor. Anything that costs milliseconds must earn them
   with a measurement, not an argument.
2. **Real data only.** No fabricated metric ever reaches the UI. If a number cannot
   be measured, the app either adds the instrumentation or shows an explicit "not
   available yet". Anything derived from an assumption — time saved, which compares
   against a configured typing speed — is labelled an estimate in place.
3. **Never silently eat words.** Every path that removes text (the skip heuristic, a
   fired trigger, a guardrail rejection) preserves the raw transcript and is
   recoverable from history.
4. **The app disappears when unused.** At rest the entire on-screen presence is a
   ~14px dot. Confidence, not a persistent reminder that you installed something.
5. **Degrade, never block.** A missing tray backend, an absent WebView2 runtime, a
   corrupt settings file, a dead credential store: each costs a capability, never
   startup. The dictation loop is the product; everything else is optional around it.
6. **Minimal dependencies, each justified in a comment before it is added.** Prefer
   stdlib. Every package carries a bundle-size cost against a 92 MB build.
7. **The measurement harness comes before the feature.** Latency instrumentation was
   built in Phase 0, before there was a pipeline to measure.

## 8. Success metrics

Measured today, from real stored data. Every figure below comes from
[`d0-audit.md`](d0-audit.md); none is estimated.

| Metric | Definition | Source | Current |
|---|---|---|---|
| **End-to-end p50** | key release → text at cursor | `latency.db`, synthetic `TOTAL` row | **623 ms** (75 historical runs) |
| **End-to-end p99** | as above | same | **1479 ms** — outside budget; cleanup owns the tail |
| **STT p50** | `transcribe` stage | same | **405 ms**, p99 585 ms — a floor, not a tail |
| **Cleanup p50** | `cleanup` stage, real model calls only | same | **238 ms**, p90 728 ms, max 1118 ms |
| **Model-call avoidance** | dictations resolved with no cleanup call | `cleanup_skipped` metadata | **54%** (8/14 from verbatim app profiles, 5/14 from the text heuristic, 1 low-confidence) |
| **Guardrail rejection rate** | rewrites discarded as unsafe | `cleanup` metadata | instrumented; no committed baseline yet |
| **Words dictated / time saved** | words ÷ configured typing WPM | `history.db` + `[analytics].typing_wpm` | shipped, labelled an estimate in the UI |

Not yet instrumented, named here so the gap is explicit: retention (does a user still
dictate in week three), transform adoption rate, and trigger false-positive rate. The
last has a specified home — a manually flaggable counter — and no implementation.

## 9. Non-goals

- **No cloud account, no sync, no team features.** There is no server component and
  there is not going to be one.
- **No public network listener.** The dashboard binds loopback on an ephemeral port
  behind a per-launch token. It is the single agreed exception to the no-web-UI rule
  and it stays local.
- **No meeting transcription, no diarisation, no long-form recording.**
- **No local model in the current phases.** Groq only. Revisiting that is a post-1.0
  conversation, not a backlog item.
- **No telemetry, ever.** Not anonymised, not opt-in, not "just crash reports".
- **No macOS or Linux build yet** — but every OS-specific call already sits behind a
  `PlatformAdapter`, so this is deferred work rather than a rewrite.

## 10. Risks

| Risk | Severity | Standing mitigation |
|---|---|---|
| **Free-tier limits** (30 req/min org-wide, 2000 audio req/day) | High | The skip heuristic avoids roughly half of cleanup calls; streaming defaults off because it costs 2–4× the audio requests for almost no latency win; 429s read the actual limit from response headers and are never blind-retried. |
| **Model deprecation** | High — it has already happened once. `llama-3.1-8b-instant` began 404ing and took the whole cleanup stage down. | The model is configurable, not hardcoded; `openai/gpt-oss-20b` replaced it. A startup health check would have caught it and does not exist yet. |
| **Latency tail** | Medium | p99 is 1479 ms against a 1000 ms budget and cleanup owns it. The evidence points at a whole-pipeline timeout budget with raw-transcript fallback rather than at streaming: input length explains only ~15% of cleanup's variance (r² = 0.149), so the delay is largely *before* the first token, where incremental injection would hide it rather than remove it. |
| **Unsigned binary / SmartScreen** | Medium | No signing certificate is assumed. This has to be documented rather than solved. |
| **Trigger false positives** | Medium | Whole-phrase, word-boundary matching only — never substring; longer phrases matched first; the raw pre-substitution transcript is always preserved. |
| **Bundle size (92 MB)** | Low | Known composition: `numpy.libs` 21 MB for a single int16 view in the FLAC encoder, `PIL` 13 MB, Tcl/Tk ~7.8 MB. numpy is the obvious candidate for removal. |
