# UI reference analysis — Wispr Flow desktop

Source: `Screenshots of whisper flow interface/1.png` … `14.png` (14 images, each
opened individually). This document records **what is observably in the images**.
Where I am inferring, it is marked *(inferred)*. Where the images are silent, it is
listed in §7 Gaps rather than filled in.

Two distinct surfaces appear:

| Surface | Screenshots | Character |
|---|---|---|
| Launch + onboarding | 1–6 | Full-bleed wizard, warm beige, one decision per screen |
| Main application | 7–14 | Sidebar + content panel, dense, persistent |

---

## 1. Screen inventory

### 1.png — Launch splash
- **Window**: small centred window, roughly 655×360 logical px, no title bar, no
  chrome, square corners, sitting over the desktop. Appears before anything else.
- **Content**, vertically centred, all centre-aligned:
  - App mark: dark rounded-square tile (~48px) containing a white four-bar
    equaliser glyph.
  - Headline in **serif**: "Getting Wispr Flow ready".
  - Sub-line in sans, muted: "This might take up to 30 seconds."
  - An indeterminate progress bar: 2px track, light grey, with a dark segment
    occupying the right ~60% — a determinate-looking but almost certainly
    indeterminate sweep.
- **What the user is doing**: nothing. This screen exists to own the 30 seconds
  where a cold app would otherwise look broken. It is the single most important
  screen in the set for our purposes: it converts "did it launch?" into
  "it's working".

### 2.png — Onboarding · Set up · languages (summary)
- **Frame**: the app window, near-maximised, rendering a wizard. Top bar is white,
  full width, and holds the step rail (see §2). A language dropdown ("English ⌄")
  sits at the far right of that bar.
- **Body**: warm beige (#EAE7E0-ish) canvas with very large, very low-contrast
  tonal watermark shapes (leaf/feather forms) — texture, not content.
- **Elements**: "← Back" text link top-left of the body; centred H1
  "Set all the languages you speak"; centred sub "Flow works in 100+ languages";
  then a white card, ~700px wide, centred, containing:
  - Card title "Your selected languages"
  - An inset well (light grey, rounded) holding chips: `English`, `Hindi (हिन्दी)`,
    `Marathi (मराठी)`, then a `+` chip. Selected chips are lilac-filled.
  - Footer row, right-aligned: secondary button "Change languages", primary
    black button "Continue".
- **User task**: confirm or amend detected languages.

### 3.png — Onboarding · Languages picker (modal)
- Same wizard behind it, **dimmed**. A large white modal (~1065×660) with heavier
  corner radius and a drop shadow.
- **Header row**: title "Languages", sub "Select the languages you want to use with
  Flow" on the left; "Auto-detect" label + pill **toggle** (off) on the right.
- **Left column (~700px)**: a search field with a magnifier icon and placeholder
  "Search for any languages", then a **3-column grid of language tiles**. Each tile:
  rounded rect, English name on line 1 (medium weight), endonym on line 2 (smaller,
  muted) — e.g. "Chinese - Simplified / 简体中文". Selected tiles: lilac fill +
  1px purple border. Unselected: flat light-grey fill, no border. The grid scrolls
  (Bengali/Bosnian/Breton are clipped at the bottom edge).
- **Right column (~280px)**: "Selected" heading and a list of chosen languages, each
  row with a `–` (remove) affordance right-aligned.
- **Footer**: black "Save and close" bottom-right of the modal.
- **User task**: search and multi-select from a long list. This is the only long-list
  interaction pattern in the set, and it is worth copying wholesale.

### 4.png — Onboarding · Test the keyboard shortcut
- H1 "Test the keyboard shortcut". Sub-line with **inline key caps**:
  "We recommend the `Ctrl` + `Win` keys at the bottom left of the keyboard" —
  the caps are rendered inline in running text, small, white with a hairline border.
- White card, ~700px:
  - Card question, left-aligned, semibold: "Do the buttons turn purple while
    pressing them?"
  - A grey inset well containing two **large key caps** (~76×76): `Ctrl` with its
    label in the top-left corner of the cap (mirroring a physical keycap legend),
    and the Windows key rendered as its glyph. These change colour (to purple)
    while physically held — the question text tells us so; the screenshot shows
    the idle state.
  - Footer: "✎ Edit shortcut" as a left-aligned tertiary button; right-aligned
    "No" (grey) and "Yes" (black) buttons.
- **User task**: physically press the hotkey and confirm the app is receiving it.
  Note the design decision: rather than asserting "hotkey registered", it asks the
  user to *observe* the feedback. That is a genuine permission/capture test dressed
  as a question.

### 5.png — Onboarding · Learn · try it in a fake Slack
- H1 "Use *Flow* to send a message" (the word Flow in serif italic, inline in a
  sans headline — a recurring typographic tic).
- White card, ~620px, styled as **a miniature Slack window**:
  - Header strip: Slack logo + "Slack" label, light grey.
  - Body: an avatar, name "Tobias", message "Hey Aashutosh, what's up?".
  - Bottom: a bordered input box whose placeholder is an instruction with inline
    key caps: "Hold down `Ctrl` `Win`, say something, then release." with a send
    arrow at the right.
- Full-width black "Continue" button at the bottom of the viewport (not in the card).
- **User task**: the first real dictation, in a safe sandbox that looks like the app
  they'll actually use. The card is the target — dictating fills that input.

### 6.png — Onboarding · Personalize · "Nice job!" payoff
- **This screen is dark.** Charcoal (#1E1E1E) canvas with the same watermark shapes
  in near-black; the step rail above stays white.
- Left half: giant serif "Nice job!"; below it, "Speaking can be" in sans, then two
  labelled bars:
  - `AVG TYPING SPEED` — value chip "50 words/min" on a short beige bar.
  - `AASHUTOSH'S VOICE` — value chip "200 words/min" on a long lilac bar (~3.5×).
  - A hand-drawn lilac arrow curves from the "Speaking can be" line to the right half.
- Right half: a line illustration (a winged shopping-cart), then "4x *faster!*"
  (numeral in serif, word in serif italic, lilac) and "than the average typing speed".
- Cream "Continue" button, centred, bottom.
- **User task**: none — this is the reward screen. It converts the one dictation
  they just did into a number about *them*. Labels are personalised ("AASHUTOSH'S
  VOICE"), and the comparison baseline is an assumption (avg typing speed 50 wpm).

### 7.png — Main app · Dictation (home)
- **Window**: ~1400×960, rounded corners, custom title bar (§3).
- **Greeting line**, top of content: "Hey Aashutosh, get back into the flow with
  `Ctrl` + `Win`" — the key caps here are **amber/orange filled**, the only warm
  accent in the app, used to make the hotkey unmissable on the home screen.
- **Promo banner**: full-width dark card with a photographic background (a laptop),
  serif headline "Try Flow in another app", sub "Flow works anywhere you type.",
  and a light "Get started" button.
- **Section**: label `TODAY` (uppercase, letter-spaced, small, muted) with a search
  icon right-aligned on the same baseline.
- **History list**: rows separated by hairlines, each: time ("10:16 pm", muted, fixed
  left column ~120px) + transcript text. The first row is hovered/active and reveals
  four right-aligned icon actions: ▶ play audio, ⧉ copy, ⚑ flag, ⋮ overflow. Other
  rows show no actions — **actions are hover-revealed**.
- Entries shown include a Devanagari transcript ("हम्म।") alongside English ones —
  multilingual history in one list, no grouping by language.
- **Right rail** (~310px, its own card): big serif numerals with sans labels —
  "**5** total words", "**54** wpm", "**1** day streak"; a hairline; then
  "Your Voice Profile" / "Discover how you use your voice." / a thin progress bar
  with a dot at the left and the caption "Unlocks in 2K words".
- **User task**: glance at today's activity, re-copy something, see the hotkey.

### 8.png — Main app · Insights · Your usage
- Title "Insights"; tabs "Your usage" | "Your voice" (underline-active).
- A circular "SHARE • SHARE •" sticker button (rotated text ring around an upload
  icon) at the top-right of the panel.
- **Stat card row (3 cards)**:
  1. `54` / `WORDS PER MINUTE` + (i); below, a **teal arc gauge** filled ~55% with
     centred label "Top / 38%".
  2. `0` / `FIXES MADE BY FLOW`. Note: **the zero state is shown as a zero**, not
     hidden and not faked.
  3. `5` / `TOTAL WORDS DICTATED`, wider; a hairline, then a device breakdown row
     ("🖵 Desktop / 5 words") and a right-aligned "Download on mobile" button.
- **Second row (2 cards)**:
  - "Desktop usage" with "TOTAL APPS USED | 1" right-aligned; body is an app icon,
    a teal 100% bar, and "4 AI PROMPTS".
  - "1 day streak" with "LONGEST STREAK | 1 DAY" right-aligned; body is a
    **contribution heatmap**: month labels (May–Aug) with ‹ › paging chevrons,
    Sun–Sat row labels, empty cells in light grey, one filled cell, and a
    "More ■■■■ Less" 4-step teal legend.
- **Colour note**: charts use teal/green here, not the lilac of onboarding.

### 9.png — Main app · Insights · Your voice (locked state)
- Same header/tabs. A full-width progress bar (mostly unfilled) with caption right
  above it: "Generates in 1,995 words (i)".
- **Giant centred serif**: "Unlocks in 1,995 words", sub "We'll notify you when
  ready."
- Below: three cards — "Voice Profile" (full width), "Catchphrase" and "Your peak
  time & place" (half width each) — each with a serif title and **body text rendered
  blurred**. The content is real-looking but deliberately unreadable.
- **This is the most instructive empty state in the set**: it doesn't say "no data".
  It shows the shape of the reward, blurs it, and states the exact cost to unlock.

### 10.png — Main app · Dictionary (empty)
- Title "Dictionary" + black "Add new" button top-right.
- Tabs: "All" | "Personal" | "Shared with team", with right-aligned icon actions:
  search, sort (⇅), refresh (⟳).
- **Teaching hero card** (photographic warm background, dark scrim): serif headline
  "Flow spells the way *you* do."; body paragraph with **bold runs** for emphasis
  ("Add personal terms, company jargon, client names, or industry-specific lingo.");
  then a row of chips: a light "Add new word" action chip followed by example
  entries as translucent chips — `Wispr Flow`, `Samir`, `Sara`, `Karol`, `Spyder`.
- Below the hero: **nothing**. No table, no "0 items", no dashed placeholder. The
  hero *is* the empty state.

### 11.png — Main app · Snippets (empty)
- Identical skeleton to Dictionary: title, "Add new", All/Personal/Shared tabs,
  same three icon actions.
- Hero: "The stuff *you* shouldn't have to re-type." / "Save text you type often —
  an email, intro, or prompt — then say a word to drop it in instantly." Then three
  **mapping rows** rendered as chip → chip pairs:
  - `"my LinkedIn"` → `https://www.linkedin.com/in/john-doe/`
  - `"rewrite prompt"` → `Rewrite this to be more concise…`
  - `"intro email"` → `Hey, would love to find some time to chat later…`
  - Then an "Add new snippet" action chip.
- The trigger-phrase → expansion relationship is taught entirely by that chip pair
  visual. No documentation needed.

### 12.png — Main app · Style
- Title "Style". Tabs are **contexts**, not filters: "Personal messages" |
  "Work messages" | "Email" | "Other" | "Auto cleanup" + a lilac `Beta` pill.
- Hero card (dark photo, two people): "Make Flow sound like *you*" /
  "Set up different writing styles for different apps." / "Start now" button.
- Body below the hero is empty — unconfigured state.
- This is the reference's equivalent of our per-app profiles, surfaced as
  *writing style per context* rather than *per process name*.

### 13.png — Main app · Transforms
- Title "Transforms" + dark `Beta` pill. Header right side: "Opt in" + (i) +
  **black toggle in the ON state**, then a key-cap group `Win` `Alt` `o` followed by
  "to view changes".
- Hero: "Transform works anywhere you write" / "Apply a Transform to rewrite, clean
  up, or restructure text after you dictate." / "Try it out" (filled) + "How it
  works" (text link). Background shows floating app icons (Claude, Slack, Gmail,
  LinkedIn, Notes, Copilot) — the "works everywhere" claim made visual.
- **"My Transforms"** section header with "↺ Reset to defaults" (tertiary) and a
  black "Create New" button.
- **Card grid (3 up)**: each card shows a key-cap shortcut row at the top
  (`Win` `Alt` `1`), then a bold title and one muted description line:
  - "Polish — Improve clarity and conciseness" (`Win Alt 1`)
  - "Prompt Engineer — Constructs optimal prompts" (`Win Alt 2`)
  - "Create your own — Upload your own prompt" with a `+` tile in place of the
    shortcut row (the add-card is styled as a peer of the real cards).

### 14.png — Main app · Scratchpad
- Title "Scratchpad" + dark `Beta` pill. Header right: "Add to Flow Bar" + (i) +
  toggle **off**, and a **disabled-looking outlined button** "Click to enable
  shortcut" — a control that is visibly present but inert until the toggle is on.
- Hero: "For quick thoughts you want to come back to" / body / "Start new note"
  button. Overlapping the hero's right edge is a **mock of the floating scratchpad
  window** (tab strip with `Untitled ×` and `+`, close, split, expand icons, and
  body text) — showing the thing the feature produces.
- "Recents" section header with right-aligned search, `+`, refresh icons.
- **Empty state**: centred muted text "No notes found" in an otherwise blank area.

---

## 2. Information architecture

### Two modes, hard separated
1. **Wizard mode** (1–6): no sidebar, no window chrome beyond the step rail, one
   task per screen, linear with a Back affordance. The user cannot wander.
2. **App mode** (7–14): persistent sidebar, no wizard chrome. Reached only after
   onboarding completes.

### Onboarding rail
`SIGN UP › PERMISSIONS › SET UP › LEARN › PERSONALIZE`
- Five top-level phases, rendered as uppercase letter-spaced labels with `›`
  separators, plus a **continuous purple progress underline** beneath the whole rail
  whose width tracks overall completion (≈45% at screen 2, ≈52% at 4, ≈60% at 5,
  ≈70% at 6). Phase labels are not individually highlighted; the bar carries state.
- The rail persists across every wizard screen, including the dark one.
- Each phase clearly contains **multiple screens** (SET UP holds at least the
  language summary and the hotkey test), so the rail is coarse-grained on purpose.
- A language selector sits in the rail's right end throughout — a global setting,
  available at every step.
- "Back" is per-screen and lives in the body, not the rail. The rail is not clickable
  navigation *(inferred — no hover state visible)*.

### App navigation — one flat level
Sidebar, top group (primary, 8 items, each icon + label):
`Dictation` · `Notetaker` [New!] · `Insights` · `Dictionary` · `Snippets` ·
`Style` · `Transforms` · `Scratchpad`

Sidebar, bottom group (secondary, after a divider):
`Invite your team` · `Get a free month` · `Settings` [1] · `Help`

- **There is no nesting.** Every destination is one click from every other.
- Sub-navigation within a page is always **tabs directly under the page title**, and
  tabs are of two kinds:
  - *Scope filters*: All / Personal / Shared with team (Dictionary, Snippets)
  - *Sub-pages*: Your usage / Your voice (Insights); the five contexts in Style
- Settings is **demoted to the bottom group** — deliberately not a primary
  destination, but badged when it needs attention.
- Ordering is by frequency of use: the thing you do every day (Dictation) is first;
  configuration is last.

### Modal vs persistent
- Persistent: everything in the sidebar; tabs; the right rail on Dictation.
- Modal: exactly one instance in the set — the language picker (3.png), used because
  it needs a search + long grid + a selection basket that would not fit inline.
- Overlay-ish: hover action clusters on history rows.
- Not shown: any confirm/destructive dialog.

---

## 3. Layout system

### Window
- Main window ≈ **1400×960** logical px, **rounded corners** (~10px), with a
  **custom title bar** ~68px tall:
  - Left: sidebar-collapse icon, account/profile icon.
  - Right: notification bell with a red numeric badge, then minimise / maximise /
    close. Standard Windows control order and spacing, custom-drawn.
  - No title text, no menu bar, no icon.
- The window background is a warm off-white; the **content is a separate white
  panel inset ~12px from the window edges with its own radius**, producing a subtle
  frame. The sidebar sits on the window background, not on the panel — so the panel
  visually "floats" and the sidebar recedes.
- Splash window ≈ 655×360, square corners, no chrome.

### Grid
- Sidebar: **~245px fixed**. Not resizable *(inferred)*; collapsible via the title
  bar icon.
- Content panel: fills the remainder, with **~56px internal left padding** for page
  titles and content. Content is not centred — it is left-aligned to that padding
  and stretches right, with the right edge respecting matching padding.
- Dictation is the only two-column page: content column + **~310px right rail**,
  gutter ~32px.
- Card grids are 3-up (Insights row 1, Transforms) and 2-up (Insights row 2), with
  ~28–32px gutters, equal-height cards.

### Vertical rhythm (measured off 8/10/11/12/13/14, which share a skeleton)
- Panel top edge → page title baseline: ~60px
- Page title → tab row: ~70px
- Tab row → underline/rule: ~30px, then a full-width hairline
- Rule → hero card top: ~35px
- Hero card height: ~250px (Dictionary/Snippets) to ~215px (Style/Transforms)
- Hero → next section header: ~50px
- Section header → first row/card: ~30px
- List row height: ~66px, hairline separated
- Sidebar item height: ~50px, ~4px gaps, 12px radius on the active fill

### Density
Deliberately **low**. Large type, generous padding, few items per screen, a lot of
empty canvas below the fold. The app looks unfinished-by-design when empty rather
than dense-and-configurable. This is a consumer product's density, not a tool's.

---

## 4. Component vocabulary

| Component | Appearance | Behaviour observed |
|---|---|---|
| **Nav item** | 24px icon + 15px label, 12px radius; active = solid light-grey fill; badges right-aligned | Exactly one active at a time |
| **Badge** | Amber pill with dark text ("New!"); red circle with white numeral ("1", "2") | New! = feature-new; red = actionable count |
| **Page title** | 30px semibold sans, left-aligned; optional `Beta` pill inline | — |
| **Page action** | Black rounded button, white 14px semibold text, top-right of panel | One per page max ("Add new", "Create New") |
| **Tab** | 15px, active = near-black + 2px underline; inactive = muted grey, no underline | Underline is short (label-width) |
| **Icon action row** | 18px line icons (search, sort, refresh, +), right-aligned on the tab baseline | Unlabelled; no visible tooltip in stills |
| **Hero card** | Full width, ~14px radius, photographic bg + dark scrim, serif headline, sans body, one filled button | Appears on every empty/unconfigured page |
| **Stat card** | White, hairline border, ~14px radius; 34px numeral, uppercase 12px muted label, optional (i) | Zero renders as `0` |
| **Chip** | Pill, ~34px tall; lilac fill + purple border when selected; translucent on hero cards; used as tag, action, and value | Also used in pairs with `→` for mappings |
| **Key cap** | White rounded square, hairline border, subtle shadow; label top-left inside on large caps, centred on small | Three sizes: inline-in-text, small group (`Win Alt 1`), large interactive (76px). Turns purple while physically held |
| **Toggle** | Pill switch; off = grey track/white knob; on = black track/white knob | Paired with a label and (i) to its left |
| **List row** | Hairline separated, ~66px; fixed left meta column; hover reveals a right-aligned icon cluster | Actions: play, copy, flag, overflow |
| **Feature card** | White, hairline, key-cap row + title + one description line | "+ Create your own" is styled as a peer card |
| **Section header** | Either uppercase 12px letter-spaced muted (`TODAY`) or 22px serif (`My Transforms`) | Right side carries the section's actions |
| **Progress bar** | 3–4px, full width or inline, with a caption stating what completes it | Always paired with an explicit remaining-count |
| **Modal** | ~1065×660, 16px radius, shadow, dimmed backdrop; header/body/footer; primary action bottom-right | Only used for the long-list picker |
| **Empty state** | Four distinct kinds — see §6 | — |
| **Promo card** | Dismissible ("—" in the corner), lives in the sidebar | "Flow on mobile" |
| **Share sticker** | Circular, rotated ring text around an upload glyph | Insights only |

---

## 5. Typography and colour

### Families — two, used semantically
1. **Serif display** (a transitional/Tiempos-like face): headlines on splash,
   onboarding H1s, hero-card headlines, large numerals on Dictation's rail, section
   headers like "My Transforms", and Insights' locked-state headline. **Italic** is
   used for emphasis inside sans headlines ("Make Flow sound like *you*",
   "Use *Flow* to send a message", "4x *faster!*"). This is the brand voice.
2. **Sans UI** (geometric-humanist, Söhne/Neue-Montreal-like): all navigation,
   labels, body copy, buttons, stat labels, list content.

The split is consistent and load-bearing: **serif = the app talking to you;
sans = the app being used.** Never mixed within a single functional string except
for the deliberate italic emphasis.

### Hierarchy — five levels, not more
1. Hero/serif display — 40–56px (onboarding H1 ~44px; "Unlocks in 1,995 words" ~56px)
2. Page title — 30px semibold sans
3. Card/section title — 20–22px (serif on hero cards, semibold sans in stat cards)
4. Body / nav / tabs — 15–16px regular
5. Meta — 12–13px uppercase letter-spaced muted, or 13px muted sentence case

Weights in play: regular, medium, semibold. **No black weight, no light weight.**

### Palette (sampled)
| Role | Value (approx) | Where |
|---|---|---|
| App canvas | `#F5F3EF` warm off-white | window bg behind the panel, sidebar |
| Panel / card | `#FFFFFF` | content panel, stat cards, modal |
| Inset well | `#F0EEE9` | chip wells, key-cap wells, active nav fill |
| Onboarding canvas | `#EAE7E0` beige + tonal watermarks | 2–5 |
| Dark surface | `#1A1A1A` – `#1E1E1E` | hero cards, "Nice job!" screen, primary buttons |
| Text primary | `#16150F` near-black | — |
| Text muted | `#77736B` warm grey | labels, timestamps, descriptions |
| Hairline | `#E6E3DD` | row separators, card borders |
| **Lilac / purple** | fill `#EFE4FE`, line ~`#8B5CF6` | onboarding selection, progress underline, held key caps, `Beta` pill, the "faster" bar |
| **Amber** | `#FFB84D` | the hotkey key caps in the Dictation greeting, `New!` badge |
| **Teal** | `#17706B` + 4-step ramp | all Insights charts (gauge, bar, heatmap) |
| **Red** | `#E5484D` | notification and Settings count badges only |

Note the discipline: **four accents, each with exactly one job.** Lilac = brand /
onboarding / beta. Amber = "this is your hotkey" and newness. Teal = data. Red =
count-that-needs-you. Nothing is coloured decoratively.

### How state is signalled
- **Active nav**: fill, not colour or bar.
- **Active tab**: weight + a short underline.
- **Selected item**: lilac fill + purple 1px border (language tiles, chips).
- **Toggle on**: black track (not coloured).
- **Enabled vs inert**: the Scratchpad "Click to enable shortcut" button is drawn
  in full but reads as inert — outlined, muted text, no fill — next to an off toggle.
- **Attention**: red numeric badge; never a colour change to the item itself.
- **Locked / not-yet-earned**: blurred real content + an exact remaining count.
- **Hover**: reveals the row's icon cluster; presumably a faint row fill *(inferred)*.
- **Error**: not shown anywhere in the 14 images.

---

## 6. Micro-UX

### Cold start
The splash (1.png) sets an explicit expectation — *"up to 30 seconds"* — instead of
a bare spinner. It names the app and shows the mark. Nothing is clickable.

### Empty states — four different strategies, chosen by cause
1. **Never-configured** (Dictionary, Snippets, Style, Transforms, Scratchpad hero):
   a large teaching card that shows *worked examples of the thing that would be
   there* (chips of real words; trigger → expansion pairs; a mock of the floating
   note window) plus one obvious create action. No dashed boxes, no "nothing here".
2. **Genuinely zero data** (Insights): the number `0` is printed under
   `FIXES MADE BY FLOW`. Zero is a fact, and it is shown as one.
3. **Earned-but-not-yet** (Insights › Your voice): the reward's layout is rendered
   with **blurred real-looking content**, headlined by the exact remaining cost
   ("Unlocks in 1,995 words") and a promise ("We'll notify you when ready").
4. **Filtered/none-found** (Scratchpad › Recents): plain centred muted text,
   "No notes found". Minimal, because the hero above already explains the feature.

The rule underneath: *the emptier the screen, the more it teaches.*

### Recording / processing / result
- **Not shown in any screenshot.** No pill overlay, no waveform, no "thinking"
  state, no toast on paste. See §7.
- What *is* shown is the pre-recording affordance: the key caps in 4.png turn purple
  **while held**, and the fake Slack input in 5.png instructs "Hold down Ctrl Win,
  say something, then release." So the interaction model is taught, but its runtime
  feedback is not depicted.

### After the fact
- Every transcript row offers **play** (so the audio is retained), **copy**, **flag**
  (a feedback/correction signal), and an overflow menu, all hover-revealed.
- The result of dictation is measured and reported back immediately in onboarding
  (6.png) — the loop is closed with a personalised number within the first minute.

### Feature discovery
- `New!` badge on a nav item; `Beta` pills next to page titles; a red `1` on
  Settings. Three distinct notification vocabularies, never mixed.
- Features that are opt-in show the opt-in control **in the page header, not buried
  in Settings** (Transforms' "Opt in", Scratchpad's "Add to Flow Bar").

### Personalisation
The name is used in the greeting ("Hey Aashutosh"), in stat labels ("AASHUTOSH'S
VOICE"), and in the onboarding payoff. The product addresses the user directly, in
serif, in the second person, on every hero card ("the way *you* do", "sound like
*you*", "stuff *you* shouldn't have to re-type").

---

## 7. Explicit gaps — not shown, would otherwise be invented

**Runtime dictation states (the most important gap)**
- No recording indicator / overlay / pill in any image.
- No processing or "transcribing" state.
- No paste confirmation or result toast.
- No VU meter, waveform, or level feedback.
- No cancel-mid-recording affordance.

**Errors and failure**
- No error state of any kind: no network failure, no rate-limit, no mic-in-use,
  no permission-denied, no failed-transcription row.
- No offline or degraded mode.

**Screens referenced but never shown**
- **Settings** — badged `1` in the sidebar, never opened. So there is no evidence of
  how input device, hotkey editing, launch-at-login, or account settings are laid out.
- **Notetaker** — nav item with a `New!` badge, never opened.
- **Help** — never opened.
- **Invite your team / Get a free month** — never opened.
- **Notifications panel** — the bell shows `2`, never opened.
- **Account menu** — title-bar icon, never opened.
- **SIGN UP** and **PERMISSIONS** onboarding phases — the rail names them; no
  screenshot shows either. **We therefore have no reference for the microphone
  permission screen**, which is a required step in our target scope.

**Interactions not depicted**
- Add/edit/delete flows for dictionary words, snippets, and transforms (only the
  entry points are visible).
- The "Edit shortcut" capture UI (4.png offers the button; the capture modal isn't
  shown).
- Any destructive-confirm dialog.
- History **search results** and the search-active state (the icon exists on both
  Dictation and Scratchpad).
- Sort and refresh behaviour on Dictionary/Snippets.
- Scroll/pagination for a long history (only 4 rows exist).
- Sidebar collapsed state (the toggle exists in the title bar).
- Hover, focus, pressed and disabled states, except where inferred above.
- Keyboard navigation and focus rings.

**Systemic unknowns**
- **Dark mode for the main app.** Only one dark screen exists (6.png, the onboarding
  payoff) and it is an editorial choice, not a theme. All eight app screenshots are
  light. We have no dark reference for the sidebar, cards, or charts.
- Window resize behaviour / minimum size / responsive breakpoints.
- Tray menu and its relationship to window state — no tray UI in any image.
- Exact typefaces (both are licensed commercial faces; substitutes will be needed).
- Localisation of the UI itself (the language picker sets *dictation* languages;
  the rail's "English ⌄" may control UI language — unclear).

**Deliberately out of scope for us** (present in the reference, absent from our
product): account/sign-up, team sharing ("Shared with team" tabs, Invite your team),
the mobile app and its promo card, referral ("Get a free month"), Notetaker, and the
share-to-social sticker.

---

## 8. The five things worth stealing outright

1. **The splash with a stated duration.** Cheapest possible fix for "is it running?"
2. **The hotkey rendered as a physical key cap that reacts to the physical key.**
   It proves capture works without any technical language.
3. **Teaching empty states with worked examples**, not placeholders — especially the
   `"trigger"` → `expansion` chip pair, which explains snippets with no prose.
4. **The onboarding payoff screen.** One dictation in, and the user gets a number
   about themselves. Our D0 latency tracker already holds the data to do a truthful
   version of this.
5. **Accent discipline**: four colours, one job each. It is the thing that makes a
   restrained layout read as a product rather than a control panel.
