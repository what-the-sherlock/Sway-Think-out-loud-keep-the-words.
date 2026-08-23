# Derived screens — the four the reference is silent on

Everything here is derived from [`ui-reference-analysis.md`](ui-reference-analysis.md)
(§ references point at it), not from general knowledge of how dictation apps
look. Each derivation names the section it comes from. Where the reference
gives no basis at all, that is said explicitly rather than filled in.

Spacing values are quoted from §3's measured rhythm:

```
sidebar 245   panel left pad 56   panel inset 12
panel top → title 60     title → tabs 70      tabs → rule 30
rule → hero 35           hero → section 50    section → first row 30
list row 66              sidebar item 50      card gutter 28–32
radius: card 14, nav item 12, modal 16, chip pill, button ~10
```

Palette tokens from §5, unchanged:

```
canvas   #F5F3EF     panel   #FFFFFF    well    #F0EEE9
ink      #16150F     muted   #77736B    hairline #E6E3DD
dark     #1A1A1A
lilac    #EFE4FE fill / #8B5CF6 line
amber    #FFB84D
teal     #17706B
red      #E5484D
```

**Closed decision applied throughout:** light theme only, no switching, no
abstraction layer.

---

## 1. Runtime dictation states

The most-seen surface in the product, and §7 records that the reference shows
**none of it** — no recording indicator, no processing state, no paste
confirmation, no level feedback. So every colour and shape below is derived,
and the derivation is stated for each.

### 1.1 What already exists vs. what is new

`src/flow/overlay.py` is 450 lines and already does most of the mechanics.

| Element | Status |
|---|---|
| Capsule geometry: `_HEIGHT 44`, `_AVATAR 34`, `_PAD_LEFT 6`, `_GAP 10`, `_PAD_RIGHT 16`, `_MIN_ACTIVE_WIDTH 128`, `_FONT_SIZE 13` | **Exists.** Keep every value. |
| PIL supersampled compositing at `_SS = 4`, LANCZOS downsample | **Exists.** This is what makes 44px edges clean; do not touch it. |
| `-transparentcolor` keying, `_KEY_RGB = (8,8,9)` | **Exists.** |
| Queue-based `set_state`, render-key memoisation in `_apply_frame` | **Exists.** The render key `(state, detail, badge_frame, elapsed_s)` is the contract everything below must respect. |
| Four states: `IDLE / RECORDING / THINKING / ERROR` | **Exists**, but incomplete — no `injecting`, no `done`. |
| Badge shape varies per state (pulse dot / spinner arc / warning triangle) | **Exists**, and is correct: §5 records that the reference never signals state by colour alone either. |
| Elapsed counter `Listening · 0:03` (`_elapsed_label`) | **Exists.** Directly matches §4's progress-bar rule — "always paired with a caption stating what completes it". |
| **Accent palette** | **New — repalette required.** See 1.3. |
| **`INJECTING` and `DONE` states** | **New.** |
| **Mic level indicator** | **New.** See 1.5. |
| Fade-out after inject | **New** (PRODUCTION_BRIEF D2). |
| Click-through / non-activating (`WS_EX_TRANSPARENT｜WS_EX_NOACTIVATE`) | **New** (D2, R3). Not a visual change. |

So: **structurally a restyle, not a rewrite.** The new work is two states, one
indicator, and a palette correction.

### 1.2 The pill stays dark — and that is not a theme

The app is light. The pill is not part of the app's canvas; it floats over
whatever the user happens to have on screen, which we do not control. §5's
palette has no token for "surface over an unknown background", because the
reference never has one. A light pill over a light document disappears; a dark
pill is legible over both. This is the same reasoning that makes tooltips dark
in light UIs.

This is one fixed surface with fixed colours, not a second theme and not an
abstraction layer. `flow.branding`'s `SURFACE (26,27,32)` / `INK (235,236,240)`
stay exactly as they are.

### 1.3 Accents — the current four are wrong under §5's discipline

`flow/branding.py` today:

```
ACCENT_RECORDING = (255,  77,  94)   red
ACCENT_THINKING  = ( 74, 168, 255)   blue
ACCENT_ERROR     = (255, 171,  46)   amber
ACCENT_IDLE      = (108, 111, 125)   grey
```

§5 records four accents with exactly one job each: lilac = brand/onboarding,
**amber = "this is your hotkey"**, teal = data, red = a count that needs you.
Measured against that, three of the four above are misassigned:

- **Red on recording.** §5 gives red one job: a count that needs the user. A
  recording indicator needs nothing from the user — they are already doing the
  thing. Red here spends the one token that means "act on me" on the most
  frequent, most benign state in the product.
- **Blue on thinking.** A fifth accent with no job in §5 at all.
- **Amber on error.** Amber's attested job (§1 7.png, §5) is *the hotkey* —
  the greeting renders `Ctrl` + `Win` in amber precisely so the hotkey is
  unmissable. Spending it on failure is spending it on the wrong thing.

Derived reassignment:

| State | Accent | Derived from |
|---|---|---|
| `IDLE` | none — no accent ring, compact capsule | §5 "Active nav: fill, not colour". An idle nav item has no colour; neither should an idle pill. |
| `LISTENING` | **amber `#FFB84D`** | §5: amber's one job is "this is your hotkey". The hotkey being held *is* the recording state. This is the single strongest derivation available. |
| `PROCESSING` | **no accent — neutral ink + motion** | §1 1.png. The reference's own answer to "working, please wait" is a neutral indeterminate bar on a light track, not a coloured spinner. Reuse that answer rather than inventing a colour. The existing 8-step arc becomes an ink-coloured arc. |
| `INJECTING` | same as processing | Sub-100ms in practice; a distinct colour would flicker. |
| `DONE` | **no accent — a brief ink check glyph, then fade** | §6: the reference confirms results with facts, not decoration. |
| `ERROR` | see §2 below | |

Note the deliberate consequence: **only one runtime state carries colour.**
That is the §5 discipline applied honestly — the pill is ambient chrome that
sits on screen all day, and four coloured states would be four colours
competing for attention every minute of use.

### 1.4 The five states, drawn

Widths follow the existing formula
`max(128, 6 + 34 + 10 + text_width + 16)`; height is always 44.

```
IDLE                                    compact: width == height == 44
        ┌────────┐
        │  ( ◍ ) │                      avatar 34, mark at 0.56×avatar ≈ 19
        └────────┘                      no badge, no label, no accent ring
        └─ 44 ──┘                       fill: SURFACE (26,27,32)

LISTENING                               width 128 min, grows with the label
        ┌──────────────────────────────────┐
        │  ( ◍ )◐   Listening · 0:03  ▮▮▯  │   avatar ring: AMBER, 2px
        └──────────────────────────────────┘   badge: pulsing dot, 500ms on/off
         6   34  10        13px        16      level glyph: 3 bars, see 1.5
                                                elapsed from _elapsed_label()

PROCESSING
        ┌──────────────────────────────┐
        │  ( ◍ )◜   Transcribing…      │      avatar ring: none
        └──────────────────────────────┘      badge: 8-step arc in INK, 90ms
                                               label states the STAGE, not "thinking"

INJECTING
        ┌──────────────────────────────┐
        │  ( ◍ )◜   Pasting…           │      same arc; label changes only
        └──────────────────────────────┘

DONE                                    holds ~600ms, then fades to IDLE
        ┌──────────────────────────────┐
        │  ( ◍ )✓   47 words           │      badge: ink check glyph
        └──────────────────────────────┘      §6: confirm with a fact, not a tick
```

**`PROCESSING` labels the actual stage.** We have the stage boundaries already
— CLAUDE.md requires a timing event at each one, and `queries.stage_breakdown`
reads them back. So `Transcribing…` → `Cleaning up…` → `Pasting…` is free and
true. §6 records that the reference's most instructive states are the ones
that state a fact ("Unlocks in 1,995 words") rather than a mood; "Thinking" is
a mood.

**`DONE` shows the word count.** Derived from §6's "the loop is closed with a
personalised number" (6.png) and §1 7.png's three-number rail. `analytics.count_words`
already computes it. It also silently answers "did anything actually paste?",
which is our most common real failure.

### 1.5 Level meter — a three-state glyph earns its cost; a waveform does not

PRODUCTION_BRIEF D2 asks for "a live mic-level waveform". Recommendation:
**ship the indicator, not the waveform.**

*What it must answer.* Not "how loud am I" — the user knows that. It answers
**"is Flow hearing anything at all?"**, which is the visible symptom of three
of our six real errors (mic muted in Windows, mic held by another app, wrong
device selected). Without it, all three present identically as an empty
transcript after a full round trip and a wasted API call.

*What a waveform costs.* Two things, and the second is the disqualifier:

1. CLAUDE.md: "the audio callback thread must never block, allocate large
   buffers, or do I/O." A peak-amplitude scan of a block already in cache is a
   few hundred ops and does not violate this. A rolling waveform history does
   allocate.
2. `overlay.py:_apply_frame` re-renders **only when the render key changes**.
   A waveform changes every frame by construction, so it converts an
   idle-most-of-the-time widget into a 20–30 fps PIL supersample-and-downsample
   pipeline (`_SS = 4`, LANCZOS) running during the exact seconds where our
   p50 budget is being spent. That is CPU spent making latency worse in order
   to display how latency is going.

*The derived alternative.* Quantise peak amplitude into three buckets and add
the bucket to the render key:

```
silent    ▯▯▯     below the VAD's own speech threshold
speech    ▮▮▯     VAD says this frame is speech
loud      ▮▮▮     approaching clip
```

The key becomes `(state, detail, badge_frame, elapsed_s, level_bucket)`, so
the pill re-renders on a *bucket change* — a handful of times per utterance,
not 30 times a second. The memoisation contract survives intact.

Derivation for the glyph itself: §4's progress bar is "3–4px, with a caption
stating what completes it" and §5 signals selection by fill. Three discrete
filled bars is the same vocabulary at pill scale. It is also the app mark's own
form — `flow.branding.mark_image` is a four-bar equaliser glyph, and so is the
reference's (§1 1.png).

*Empty-state discipline (§6):* if the bucket is `silent` for the whole hold,
that is **cause 2 — genuinely zero, print the zero**. On release, the pill goes
to `DONE` with `No speech detected` rather than making the API call. Truthful,
and it saves a request against the 2000/day audio budget.

### 1.6 Position and dismissal

- Position: keep today's top-centre `+24`, make it configurable (D2).
- `DONE` and `ERROR` are transient. `ERROR_HOLD_S = 4.0` already exists and
  already expires errors back to idle — §6's discipline (the pill must never
  lie about the current state) is already implemented; keep it.
- `DONE` holds ~600ms then fades. Nothing else fades.

---

## 2. Error states

### 2.1 The colour question, answered

**Decision: reuse red. Do not add a fifth token.**

§5 records four accents with one job each and states plainly that this
discipline "is what makes a restrained layout read as a product rather than a
control panel". A fifth accent breaks the rule that makes the system work.

Red's attested job is *a count that needs you* — the `2` on the bell, the `1`
on Settings (§4, §5). The load-bearing half of that phrase is **"needs you"**,
not "is a number". An error the user must act on is the same semantic: a thing
demanding action. So red extends to errors **only where the user must actually
do something**, and stays absent everywhere else.

That splits our six errors by *actionability*, not by severity:

| Error | Colour | Because |
|---|---|---|
| **API key invalid / missing** | **red** | Only the user can fix it. |
| **Mic unavailable** (no device) | **red** | Only the user can fix it. |
| **Mic in use by another app** | **red** | Only the user can fix it. |
| **Groq unreachable** | **none** — muted ink | Transient. Nothing to do but retry, which we already do. |
| **Rate limited** | **none** — muted ink + countdown | Resolves itself at a known time. See 2.3. |
| **Clipboard injection failed** | **none** — muted ink + instruction | The text is *not lost*; it is on the clipboard. This is a hand-off, not a failure. |

Colour is never the only signal (§5, and `overlay.py` already varies badge
shape per state). Red marks urgency; the words carry the identity.

### 2.2 Where errors appear — three surfaces, one message each

**(a) The pill** — transient, 4s (`ERROR_HOLD_S`), then back to idle.

```
        ┌────────────────────────────────────────┐
        │  ( ◍ )⚠   Microphone is in use         │
        └────────────────────────────────────────┘
          badge: warning triangle (already implemented)
          ring:  RED for actionable, none for transient
          label: ≤ 34 chars, plain language, no error codes
```

**(b) Home, agent status** — persistent while the condition holds. Derived from
§4's stat card and §1 7.png's right rail, and it replaces both today's
`views.performance` health banner and the sidebar mic toggle:

```
  ┌─ panel, radius 14 ───────────────────────────────────────────┐
  │ 24 pad                                                       │
  │  ● Paused — no API key                        [ Fix this ]   │  ← dot RED (actionable)
  │     16px semibold ink                          black button  │
  │                                                              │
  │  Flow can't transcribe until a Groq key is set.              │  ← 13px muted
  │  16px gap                                                    │
  └──────────────────────────────────────────────────────────────┘
```

One button, top-right of the card — §4: "one page action per page max", black
fill. The button goes straight to the field that fixes it, never to a doc.

**(c) Settings › Data → error log** — the history. Derived from §4's list row
(fixed left meta column, hairline separated, 66px, hover-revealed actions).
`queries.performance()` already returns errors grouped by type.

```
  RECENT ERRORS                                         [search]   ← §4 section header,
  ──────────────────────────────────────────────────────────────      12px uppercase muted
   09:41 pm   Rate limited — audio requests per day        ⧉  ⋮    ← row 66, hover-revealed
  ──────────────────────────────────────────────────────────────
   09:12 pm   Groq unreachable — connection timed out      ⧉  ⋮
  ──────────────────────────────────────────────────────────────
```

**Zero errors is printed as zero** (§6 strategy 2 — `FIXES MADE BY FLOW` shows
`0`):

```
   0
   ERRORS IN THE LAST 30 DAYS
   across 412 dictations
```

Not "All good ✓". A fact.

### 2.3 Rate limiting — the one error with a threshold

§4: a progress bar is "always paired with a caption stating what completes it".
§6 strategy 3: the locked state states **the exact remaining cost** ("Unlocks
in 1,995 words"), not "come back later".

We have the exact numbers. `GroqRateLimitError` carries `limit_type` and
`retry_after_s`, parsed from the 429 headers with a body fallback
(`groq_client.py:167`), and `engine.py:497` already builds a detail string from
both. So the threshold pattern applies literally:

```
  ┌──────────────────────────────────────────────────────────────┐
  │  ● Rate limited — requests per minute                        │  ← dot MUTED, not red
  │                                                              │
  │  ████████████████████████░░░░░░░░░░░░░  ← 4px, teal          │
  │                                                              │
  │  Ready in 47s                              13px muted        │  ← counts down live
  └──────────────────────────────────────────────────────────────┘
```

And when it is the daily audio budget rather than the per-minute one — a
completely different situation for the user, which `engine.py:504` already
distinguishes:

```
  ●  Daily audio limit reached
     Groq's free tier allows 2,000 audio requests a day.
     Resets in 6h 12m.
```

Teal on the bar, not red: §5 gives teal to data, and a quota consumed is data.

**Constraint from PRODUCTION_BRIEF D6, restated because it binds this screen:**
the headers are the source of truth for these numbers. `30/min` and
`2000/day` may be shown as a **labelled fallback** before the first request of
a session, and must never be computed against once real header values exist.

### 2.4 The six, in full

| Error | Pill label | Home card | Action |
|---|---|---|---|
| API key invalid | `Key rejected` | ● red · "Paused — key rejected" · *Groq rejected this key. It may have been revoked.* | `Fix this` → Settings › General, key field focused |
| API key missing | *(never reached — onboarding gates it)* | ● red · "Paused — no API key" | `Fix this` → Settings › General |
| Groq unreachable | `No connection` | ● muted · "Can't reach Groq" · *Retrying. Your recording is safe.* | none |
| Rate limited | `Rate limited (47s)` | ● muted · countdown card, §2.3 | none |
| Mic unavailable | `No microphone` | ● red · "No microphone found" · *Flow can't hear anything.* | `Choose device` → Settings › Voice |
| Mic in use | `Mic is in use` | ● red · "Microphone is in use" · *Another app has exclusive control.* | `Choose device` → Settings › Voice |
| Injection failed | `Copied instead` | ● muted · "Couldn't paste into that window" · *Your text is on the clipboard — press Ctrl+V.* | none |

`PasteBlockedError` already exists (`errors.py`) for the privilege-mismatch
case, and `paste_likely_blocked()` already detects it (`platform/windows.py:301`),
so the last row is a message we can already produce correctly.

**"Your recording is safe" is a promise, not decoration.** D7 requires the
audio queue (R4) behind it. Until that ships, the string must not.

---

## 3. Settings

§2 gives us where Settings lives — bottom sidebar group, after the divider,
badged when it needs attention — and §7 records that its contents are never
shown. So the shell is attested and everything inside it is derived.

### 3.1 Shape

§2: the reference's IA is flat, and sub-navigation is *always* tabs directly
under the page title. Settings gets tabs, not a nested tree. Five tabs, which
is within the attested vocabulary — the Style page has five (§1 12.png).

```
  General · Voice · Apps · Triggers · Data
```

> **Noted, not acted on.** §2 records that the reference puts the equivalents
> of *Apps* (Style) and *Triggers* (Snippets) at the **top level** and demotes
> Settings — it treats personalisation as a feature and configuration as
> plumbing. The target scope puts both inside Settings, so that is what is
> drawn here. Flagged as Q2 in the gap analysis.

The badge: §5 gives red to a count that needs the user. Settings' badge counts
**unresolved configuration problems** — no key, key rejected, no device,
trigger-phrase conflicts. Zero problems, no badge. It is a count, and it needs
them.

### 3.2 General

```
 ┌ panel ────────────────────────────────────────────────────────────────┐
 │← 56 pad                                                               │
 │                                                                       │
 │  Settings                                              ← 30px semibold│  60 from top
 │                                                                       │
 │  General   Voice   Apps   Triggers   Data              ← 15px, active │  70 below title
 │  ────────                                                 underlined  │  30, then hairline
 │  ═════════════════════════════════════════════════════════════════════│
 │                                                                       │  35
 │  GROQ API KEY                                          ← 12px upper,  │
 │                                                           muted, +8%  │
 │  ┌─ well #F0EEE9, radius 10, h 48 ──────────────┐                     │
 │  │  gsk_••••••••••••••••••••••••••7d4f          │  [Change] [Test]    │
 │  └──────────────────────────────────────────────┘   ghost    ghost    │
 │                                                                       │  12
 │  Stored in Windows Credential Manager.                 ← 13px muted   │
 │                                                                       │  40
 │  ─────────────────────────────────────────────────────────────────────│
 │                                                                       │  32
 │  Start Flow when I sign in                        ┌────┐              │  row 66
 │  Flow runs in the notification area.              │  ●─│  toggle      │
 │                                                   └────┘  §4          │
 │  ─────────────────────────────────────────────────────────────────────│
 │  Overlay position                          Top centre  ⌄              │  row 66
 │  ─────────────────────────────────────────────────────────────────────│
 └───────────────────────────────────────────────────────────────────────┘
```

**Masked key.** No reference basis — derived from §4's inset well (used for
chip groups and key-cap groups) plus §5's muted text. Last four characters
shown so the user can tell two keys apart; that is the only thing masking
must not take away.

**Test connection.** §6's discipline is that the app reports facts. So the
result is a fact, in place, replacing the caption line:

```
  ✓  Connected · whisper-large-v3-turbo · 340 ms      ← ink, not green
  ✕  Rejected · Groq returned 401 invalid_api_key     ← red dot: the user must act
```

Not a green tick and not a toast. The latency number is free — we are already
making the call — and it is the first real evidence the user gets that the
thing works.

### 3.3 Voice — hotkey, device, language

**Hotkey.** This is the one Settings control with a real reference: §1 4.png.
Reuse it exactly — the 76px key caps in a `#F0EEE9` well, the pencil-icon
`Edit shortcut` tertiary button on the left.

```
  DICTATION HOTKEY
  ┌─ well, radius 12, h 112 ──────────────────────────────────┐
  │            ┌────────┐                                     │
  │            │Ctrl    │   ← 76×76, radius 12, white,        │
  │            │        │     hairline border, label inset    │
  │            │        │     top-left like a real keycap     │
  │            └────────┘     (§1 4.png, §4)                  │
  └───────────────────────────────────────────────────────────┘
   ✎ Edit shortcut                                             ← tertiary, left
```

**Two key-cap colours, both attested, and they mean different things:**

- **amber fill** — "this is your hotkey", at rest. §1 7.png, §5.
- **lilac fill + purple border** — "this key is down right now". §1 4.png:
  *"Do the buttons turn purple while pressing them?"*

So in the capture modal, caps light lilac as the user holds keys, and the
saved hotkey renders amber on Home. This also means the settings screen
performs the same live capture test the onboarding screen does — the user can
re-verify the hook at any time, not only during onboarding.

Capture modal (§4's modal: 16px radius, dimmed backdrop, primary bottom-right):

```
 ┌──────────────────────── modal 560×320, radius 16 ─────────────────────┐
 │  Press the keys you want to hold                       ← 20px         │
 │  Release them when you're done.                        ← 13px muted   │
 │                                                                       │
 │  ┌─ well, h 112 ─────────────────────────────────────────────────┐    │
 │  │        ┌────────┐  ┌────────┐                                 │    │
 │  │        │Ctrl    │  │Shift   │   ← LILAC while held            │    │
 │  │        └────────┘  └────────┘                                 │    │
 │  └───────────────────────────────────────────────────────────────┘    │
 │                                                                       │
 │  ⚠ Ctrl+Shift is used by your voice-command hotkey.   ← red dot,      │
 │                                                          user must act│
 │                                        [Cancel]  [ Save ]             │
 └───────────────────────────────────────────────────────────────────────┘
```

Conflict detection is a D5 requirement; the message states *which* conflict,
never "invalid".

**Input device.** §7 notes the language picker (§1 3.png) is the only long-list
pattern in the reference — but device lists are 3–15 entries, so this is a
list, not a searchable modal grid. Selected state = lilac fill + purple 1px
border (§5). `flow devices` already enumerates them.

```
  INPUT DEVICE
  ┌──────────────────────────────────────────────────────────────┐
  │ ◉  System default                                 ▮▮▯        │  ← selected: lilac
  ├──────────────────────────────────────────────────────────────┤     fill + purple border
  │ ○  Microphone (Realtek Audio)                     ▯▯▯        │  ← rows 66
  ├──────────────────────────────────────────────────────────────┤
  │ ○  Headset (Jabra Evolve)                         ▯▯▯        │
  └──────────────────────────────────────────────────────────────┘
  Speak — the bars move for the device Flow is listening to.
```

The level glyph is the same three-bar component as the pill (§1.5), live on the
selected row. It makes "did I pick the right one" answerable without leaving
the screen.

*Empty state, cause = genuinely zero (§6 strategy 2):*

```
   0
   MICROPHONES FOUND
   Windows isn't reporting any input device.   [Open Sound settings]
```

### 3.4 Apps — per-app profiles

`flow.profiles` has three styles today (casual / formal / verbatim) resolved
from `focused_process_name()`. §1 12.png frames the same idea as writing style
per context, which is the better user-facing frame.

```
  Chat apps          Casual  ⌄     ← §4 list row, 66
  ──────────────────────────────────────────────────────────────
   Slack · Discord · Teams · Telegram · WhatsApp · Signal   + Add
   ↑ §4 chips, translucent on the well
  ──────────────────────────────────────────────────────────────
  Mail clients       Formal  ⌄
  ──────────────────────────────────────────────────────────────
   Outlook · Thunderbird · Mailbird                        + Add
  ──────────────────────────────────────────────────────────────
  Terminals & editors   No cleanup  ⌄
  ──────────────────────────────────────────────────────────────
   cmd · powershell · WindowsTerminal · Code                + Add
  ──────────────────────────────────────────────────────────────
  Everything else    Casual  ⌄                     ← DEFAULT_STYLE
```

Chips are removable on hover (§4: hover-revealed actions). `+ Add` opens a
picker of currently-running processes — derived from §1 3.png's picker, at the
scale the data actually needs.

### 3.5 Triggers — the CRUD list with no reference at all

§7 lists this as having no reference. Derived entirely from §4's list row, §4's
chip pair, and §6's empty-state discipline.

**Empty state — cause 1, never-configured, so it teaches (§6).** §1 11.png's
`"trigger"` → `expansion` chip pair explains the whole concept with no prose,
and §8 names it as one of the five things worth copying outright. Copy it:

```
 ┌─ hero card, dark #1A1A1A, radius 14, h ~250 ──────────────────────────┐
 │  40 pad                                                               │
 │   The stuff you shouldn't have to say twice.        ← serif 22, white │
 │   Say a phrase, get the text. Nothing reaches the network.  ← 15 muted│
 │                                                                       │
 │   ┌───────────────┐    ┌──────────────────────────────────┐           │
 │   │ "my email"    │ →  │ you@example.com             │           │  ← chip pairs,
 │   └───────────────┘    └──────────────────────────────────┘              §4, h 34
 │   ┌───────────────┐    ┌──────────────────────────────────┐           │
 │   │ "today's date"│ →  │ 21 August 2026                   │           │
 │   └───────────────┘    └──────────────────────────────────┘           │
 │                                                                       │
 │   [ Add a trigger ]                                    ← light chip   │
 └───────────────────────────────────────────────────────────────────────┘
```

Worked examples, not placeholders — and the two shown are deliberately the two
D8.1 types that make **zero model calls** (`macro`, `deterministic`), so the
teaching card teaches the cheap path.

**Populated — §4's list row, with the chip pair inside it:**

```
  My triggers                                        [ Add new ]  ← black, top-right
  ═══════════════════════════════════════════════════════════════
   ┌────┐                                                          ← row 66
   │ ●─ │  "my email"      →  you@example.com        ✎  🗑    ← hover-revealed
   └────┘                                                             (§4)
   toggle §4
  ───────────────────────────────────────────────────────────────
   ┌────┐
   │ ●─ │  "sign off"      →  Best regards, Aashutosh     ✎  🗑
   └────┘
  ───────────────────────────────────────────────────────────────
   ┌────┐
   │ ─● │  "bullet points" →  Rewrite as a bulleted list  ✎  🗑    ← off: grey track
   └────┘                     ⚡ uses the model                     ← 12px muted
  ───────────────────────────────────────────────────────────────
```

- Per-row toggle, black track when on (§4, §5) — D8.1 requires `enabled`.
- The `⚡ uses the model` marker on `mode`/`transform` rows. No reference basis;
  derived from §6's rule that opt-in cost is stated where the control is, not
  buried (Transforms' "Opt in", Scratchpad's "Add to Flow Bar"). Users should
  be able to see at a glance which of their triggers cost latency and quota.
- Deletion is destructive and §7 records the reference shows **no** confirm
  dialog anywhere. So: delete immediately, with an undo in the toast. That is
  consistent with §6's "recoverable, not blocked" posture (every history row
  keeps its audio and its raw text) and with D8.5's hard undo requirement.

**Conflict warning** (D8.5 requires it). §5 gives red to counts that need the
user, so a conflict count is red, on the tab:

```
  General   Voice   Apps   Triggers ②   Data
                                     ↑ red, §5
```

and inline on the offending row:

```
   ┌────┐
   │ ●─ │  "sign off"  →  Best regards…                   ✎  🗑
   └────┘  ● "sign" already fires first — this will never match
           ↑ red dot, 13px, states the cause and the consequence
```

Never "invalid input". §6: the reference always names the exact thing.

### 3.6 Data

```
  History retention                        90 days  ⌄
  ──────────────────────────────────────────────────────────────
  Latency retention                        30 days  ⌄
  ──────────────────────────────────────────────────────────────
  Data folder      %LOCALAPPDATA%\Flow            [Open]
  ──────────────────────────────────────────────────────────────

   0
   ERRORS IN THE LAST 30 DAYS                        ← zero as zero (§6)
   across 412 dictations

  RECENT ERRORS                                     ← §2.2(c) rows
  ...

  ──────────────────────────────────────────────────────────────
  Delete all history                          [ Delete all… ]
  Removes every transcript, every timing, and any queued audio.
```

`Delete all` is the one place a confirm dialog is warranted despite §7's
silence — it is unbounded and irreversible, unlike a single row. D6 requires it
to actually wipe the R4 queued audio, so the copy says what it wipes.

---

## 4. Permissions

§2 names `PERMISSIONS` as a rail phase; §7 records it is never shown. So the
frame is fully attested and the contents are derived.

### 4.1 Frame — the attested rail layout (§1 2.png, §2, §3)

```
 ┌ white rail, h 68 ─────────────────────────────────────────────────────┐
 │   WELCOME  ›  PERMISSIONS  ›  API KEY  ›  SET UP  ›  TRY IT   English ⌄│
 │   ══════════════════                                                  │  ← lilac underline
 └───────────────────────────────────────────────────────────────────────┘     ~40% of width
 ┌ canvas #EAE7E0, tonal watermarks ─────────────────────────────────────┐
 │                                                                       │
 │  ← Back                                          ← body, top-left     │  ~68 below rail
 │                                                                       │
 │              Let Flow hear you                   ← serif ~44, centred │
 │                                                                       │
 │        Flow listens only while you hold the key. ← 16 muted, centred  │
 │                                                                       │
 │        ┌─ white card, w 700, radius 14 ──────────────────┐            │
 │        │                                                 │            │
 │        └─────────────────────────────────────────────────┘            │
 └───────────────────────────────────────────────────────────────────────┘
```

Five phases, `SIGN UP` dropped (no accounts). The language dropdown persists in
the rail (§2) and sets `[groq].language`.

### 4.2 The card — derived from the hotkey test, not invented

Windows has no permission prompt to trigger, so there is nothing to "grant".
What there *is*: a mic that may be blocked in Windows privacy settings, held
exclusively by another app, or simply absent — and the only way to know is to
open the stream and look.

§1 4.png is the reference's own answer to exactly this shape of problem. It
does not assert "hotkey registered"; it asks **"Do the buttons turn purple
while pressing them?"** and lets the user observe. §8 names this as one of the
five patterns worth stealing outright. Applied here verbatim in structure:

```
 ┌─ card 700 × ~300, radius 14, 40 pad ──────────────────────────────────┐
 │                                                                       │
 │  Say something. Do the bars move?               ← 20px semibold, left │
 │                                                                       │
 │  ┌─ well #F0EEE9, radius 12, h 120 ──────────────────────────────┐    │
 │  │                                                               │    │
 │  │                   ▮  ▮  ▯                ← the same 3-bar     │    │
 │  │                                            glyph as the pill  │    │
 │  │              Microphone (Realtek Audio)  ← 13px muted         │    │
 │  └───────────────────────────────────────────────────────────────┘    │
 │                                                                       │
 │  ⌄ Use a different microphone                   [ No ]   [ Yes ]      │
 │    ↑ tertiary, left                              grey    black        │
 └───────────────────────────────────────────────────────────────────────┘
```

The tertiary-left / No-Yes-right footer is lifted straight from §1 4.png's
`✎ Edit shortcut` / `No` / `Yes`.

`Yes` advances. **`No` is the whole point of the screen** — it is the branch
that turns three invisible failures into three specific ones.

### 4.3 The `No` branch — three causes, three different answers (§6)

The empty-state strategy is chosen by cause, exactly as §6 records. All three
are *cause 2 — genuinely zero* — so the zero is printed, and each names the one
thing that fixes it.

**No device at all:**
```
   0
   MICROPHONES FOUND
   Windows isn't reporting any input device. Plug one in, then
   check again.
                                   [ Check again ]  [ Skip for now ]
```

**Blocked in Windows privacy settings** (detectable — the stream opens and
returns silence while a device exists):
```
   ●  Windows is blocking microphone access
      Desktop apps need microphone access turned on in Windows
      privacy settings.
                        [ Open privacy settings ]  [ Check again ]
```
Red dot: the user must act (§2.1). The button deep-links
`ms-settings:privacy-microphone` — `platform.open_url` already exists.

**Held exclusively by another app:**
```
   ●  Another app is using the microphone
      Close it, or pick a different device below.
                                    [ Choose device ]  [ Check again ]
```

**`Skip for now` is offered on every branch.** §6's discipline is that the app
never lies about state; blocking onboarding on a mic the user cannot fix right
now would trade one honest failure for a dead end. Skipping lands on Home with
the red agent-status card from §2.2(b) already showing — the problem stays
visible, it just stops being a wall.

### 4.4 What this screen does not do

- It does not claim to grant anything. There is no Windows API to call.
- It does not show a permission checklist with ticks. §6: facts, not decoration.
- It does not proceed on a successful stream open alone. A stream that opens
  and returns silence is our most common real failure, and it is
  indistinguishable from success without asking the user to look.

---

## 5. Empty-state and threshold discipline, applied across all four

§6 records four strategies chosen by cause. Assignment:

| Surface | Cause | Strategy | Rendered as |
|---|---|---|---|
| Pill, nothing happening | Not an empty state | — | Compact 44×44, no label, no accent |
| Held the key, said nothing | Genuinely zero | **2 — print the zero** | `No speech detected`; **no API call made** |
| Errors, none in 30 days | Genuinely zero | **2** | `0 / ERRORS IN THE LAST 30 DAYS / across 412 dictations` |
| Triggers, none defined | Never configured | **1 — teach** | Hero with worked chip pairs (§3.5) |
| Dictionary, none added | Never configured | **1** | Hero with worked word chips |
| History, none yet | Never configured | **1** | Hero: "Hold `Ctrl` and say something." |
| History search, no match | Filtered / none found | **4 — plain text** | `No matches for "foo"`, centred, muted |
| No microphones | Genuinely zero | **2** | `0 / MICROPHONES FOUND` |
| **p95 latency, too few runs** | Threshold not reached | **3 — state the exact cost** | See below |
| **Speaking rate, no measured sessions** | Threshold not reached | **3** | See below |

### 5.1 Thresholds — the `Unlocks in 1,995 words` pattern, made truthful

§6 strategy 3 shows the reward's shape and states the exact remaining cost.
§1 9.png achieves it by **blurring real-looking content**. We take the pattern
and drop the blur, because blurred invented content is still invented content
and CLAUDE.md Phase 6 forbids fabricating a metric even decoratively.

So: the card skeleton renders greyed and empty, with the count stated exactly.

```
 ┌─ card, radius 14, content at 30% opacity ─────────────────────┐
 │                                                               │
 │   ───                                                         │
 │   P95 LATENCY                                                 │
 │                                                               │
 │   ████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  ← 4px, teal          │
 │                                                               │
 │   Needs 20 runs · 13 to go                    ← 13px muted    │
 └───────────────────────────────────────────────────────────────┘
```

`———` where the number will be, never a `0` — because zero would be a claim,
and we do not have one yet. Contrast with the errors card above, where `0` **is**
the true value. That distinction is the whole of §6's discipline: *print the
zero when zero is a fact; print nothing when you do not know.*

Same treatment for speaking rate, whose threshold is real and already
implemented — `queries.summary()` returns `measured_sessions`, and `app.js:359`
already refuses to show WPM without it:

```
   ───
   WORDS PER MINUTE
   Needs 3 measured dictations · 2 to go
```

---

## 6. What is still unresolved after this derivation

Not designed here, because doing so would be guessing rather than deriving:

- **The processing stage labels' exact wording** depends on which stages D3
  keeps after the latency work. `Transcribing… / Cleaning up… / Pasting…`
  assumes today's three.
- **`DONE` hold and fade durations** (~600ms) are a feel judgement that needs
  to be tried against a real dictation, not chosen on paper.
- **The level buckets' thresholds** should come off the VAD's own speech
  decision (`webrtcvad`, mode 2) rather than a hand-picked amplitude, but the
  clip threshold has no such source and needs measuring.
- **Whether the pill or a Windows toast owns actionable errors.**
  PRODUCTION_BRIEF D2 asks for native notifications for bad key, offline, and
  rate limited. §2.2 gives all three a home in-app. Two surfaces for one event
  needs a decision, not a default. Carried to Step 3.
