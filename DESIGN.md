# Sway — Design System & UX Guide

The single reference for how Sway looks, moves, and speaks. Read this before
adding or changing any screen, component, overlay state, or user-facing string.

**v2 — 2026-10-26.** v1 was copied from the working tree on 2026-10-22. v2
changes values on purpose; every changed value is marked **Δ** and listed in
§0 with the file that has to follow. Until §0 is empty, the code and this file
disagree *by design* — work through §0, then delete each line as it lands.
After that the old rule applies again: when code and this file disagree, fix
one of them in the same change.

| Surface | Source of truth |
|---|---|
| Dashboard tokens and components | [`src/sway/dashboard/static/styles.css`](../src/sway/dashboard/static/styles.css) |
| Dashboard markup helpers and copy | [`src/sway/dashboard/static/app.js`](../src/sway/dashboard/static/app.js) |
| Icon sprite and app shell | [`src/sway/dashboard/static/index.html`](../src/sway/dashboard/static/index.html) |
| Dashboard window host (WebView2) | wherever the WebView2 controller is created |
| Floating bar (overlay) | [`src/sway/bar.py`](../src/sway/bar.py) |
| Overlay palette, logo mark | [`src/sway/branding.py`](../src/sway/branding.py) |
| Motion durations and easing | [`src/sway/motion.py`](../src/sway/motion.py) |
| Orb animation | [`src/sway/orb.py`](../src/sway/orb.py) |

Background reading: [ui-reference-analysis.md](ui-reference-analysis.md) ·
[ui-derived-screens.md](ui-derived-screens.md) ·
[ui-gap-analysis.md](ui-gap-analysis.md).

---

## 0. Why v1 read as a prototype, and what v2 changes

v1's principles were right. The finish was wrong in six places, and each one
is a thing people *feel* without being able to name:

1. **White on white, held together by borders.** Panel `#fff`, cards `#fff` +
   hairline, buttons `#fff` + hairline. Everything is an outlined box, which
   is what a wireframe looks like. The reference is *tonal*: an off-white
   panel, borderless fill cards, filled secondary buttons, and pure white
   reserved for the things you type into. (Measured from the reference:
   panel `#fcfcfb`, card `#f5f4f0` with no border, row separators `#eeebe3`,
   secondary buttons `#eeebe3` fill, inputs `#ffffff`.)
2. **Spacing with no scale.** 60, 70, 35, 50, 28, 22×24, 56 — none share a
   base, so nothing lines up by accident and every new screen invents its own.
3. **Five control heights** (32, 34, 38, 40, 42). A 38px button next to a 40px
   input is off by 2px, and people see it.
4. **Half the states were never specified.** No elevation, no easing curves,
   no dashboard loading state, no toast behaviour, no dashboard tooltips, no
   input error/hover/disabled states, no pressed state.
5. **The browser leaks through the window.** A WebView2 app with default
   settings reloads on F5, zooms on Ctrl+wheel, shows "Inspect" on right-click,
   flashes white on launch, and lets you drag the logo out as an image. That
   is exactly "feels like a test site".
6. **The overlay's edges.** A colour-keyed Tk window has 1-bit transparency, so
   anti-aliased PIL edges get a fringe and corners look jagged — and if the
   process isn't per-monitor DPI aware it's also blurry at 125–150% scaling.

### Code that must follow v2

| Δ | File | Change |
|---|---|---|
| Surfaces + ink tokens | `styles.css` | §3.1 — `--panel`, `--card`, `--fill`, `--field`, `--separator`, `--muted`, `--faint`, `--red-ink`, `--amber-ink` |
| Card / button / list restyle | `styles.css` | §7 — remove borders from `.card`, `.btn`; add `.list` container |
| Spacing scale | `styles.css` | §5.2 — replace raw px with `--s-*` tokens |
| Control heights | `styles.css` | §5.3 — `.btn` 38→40, chips 34→32 |
| Radii | `styles.css` | §5.4 |
| Type: optical sizes | `styles.css` | §4 — Display/Text/Small Segoe variants |
| Elevation | `styles.css` | §5.5 — new shadow tokens |
| Easing + dashboard motion | `styles.css`, `motion.py` | §10 |
| Loading, toasts, tooltips, feedback | `app.js`, `styles.css` | §9, §11 |
| Form states + validation | `app.js`, `styles.css` | §7 Inputs |
| Key-held colour | `styles.css` | §3.1 — `.cap.held` indigo → amber pressed |
| WebView2 hardening | window host | §13 |
| Overlay alpha + DPI | `bar.py` | §8.5 |

---

## 1. Principles

These decide arguments. If a proposal breaks one, it needs a decision-log entry.

1. **The product disappears when it isn't being used.** At rest the bar is an
   empty outlined tray. Nothing pulses, nothing reminds you it exists.
2. **Real data only.** Never show a number we did not measure. Unmeasured is an
   em dash (`—`), not `0`. Anything built on an assumption is labelled
   **Estimate**. (CLAUDE.md, Phase 6 — non-negotiable.)
3. **State facts, not moods.** "Transcribing", not "Thinking". "47 words", not
   "✓". "Ready in 47s", not "Please wait".
4. **Every colour has one job.** Four accents, each with a single meaning
   (§3). Nothing is coloured to look nice.
5. **The emptier the screen, the more it teaches.** Empty states show worked
   examples of what would be there, plus one clear action.
6. **Never steal focus.** Paste goes to the focused window. Any surface that
   activates itself mid-dictation sends the user's words to the wrong place.
7. **Fast feels calm.** Motion lives in the 90–260 ms band, on the curves in
   §10. Decoration competes with the one animation that matters — the bar
   reacting to your voice.
8. **Keyboard users are first-class.** Every action reachable by mouse is
   reachable by keyboard, with a visible focus ring.
9. **Δ Every action answers.** Within 100ms of any click, key, or toggle,
   something on screen changes. Silence reads as broken. (§9.)
10. **Δ It's an app, not a page.** No browser behaviour reaches the user:
    no reload, no zoom, no "Inspect", no white flash, no image dragging. (§13.)

**Metric hierarchy** for any data screen, top to bottom:
productivity → usage → personalization → performance → infrastructure.
Never lead with API calls, latency or tokens.

---

## 2. Themes and surfaces

| Surface | Theme | Why |
|---|---|---|
| Dashboard window (WebView2) | **Light, only.** Closed. | No dark block, no `prefers-color-scheme`, no theme switcher, no theme abstraction. |
| Floating bar capsule | **Dark, fixed** | It floats over apps we don't control. Dark is legible over both light and dark content. One fixed surface, not a second theme. |
| Bar menu / popovers | Light (matches dashboard) | Opened on purpose, read like part of the app. |
| Hero cards, primary buttons, toasts, tooltips | Dark accents inside the light theme | Deliberate emphasis, not a mode. |

> **Δ Decision.** Dark mode is ruled out, not deferred. Update the "dark-first"
> lines in `CLAUDE.md` (Phase 6) and `PRODUCTION_BRIEF.md` to "light-only
> dashboard, dark overlay capsule" so no future change reintroduces it.

---

## 3. Colour

### 3.1 Dashboard tokens (`styles.css :root`)

**Surfaces — a tonal ladder.** Each step means something. An element gets a
fill *or* a border, never both (inputs are the one exception).

| Token | Value | Meaning |
|---|---|---|
| `--canvas` | `#f5f3ef` | Behind everything: window, sidebar |
| `--panel` | **Δ** `#fcfbf9` | The content panel. Warm off-white, not `#fff` |
| `--card` | **Δ** `#f5f3ef` | Cards, stat tiles, list containers' hover. Same value as canvas — the panel is a window onto the canvas |
| `--fill` | **Δ** `#eeebe4` | Secondary buttons, chips, active nav, segmented controls, wells |
| `--fill-hover` | **Δ** `#e7e3da` | Hover on anything with `--fill` |
| `--row-hover` | **Δ** `#f7f5f1` | Hover on list rows on the panel |
| `--field` | **Δ** `#ffffff` | **Things you type into or that float**: inputs, textareas, menus, popovers, editable cards inside sheets. White means "interactive surface" |
| `--separator` | **Δ** `#ebe8e1` | Row separators, rules under tab rows, card dividers (was `--hairline`) |
| `--field-border` | **Δ** `#dedad2` | Input borders only. Hover `#cdc8be` |
| `--dark` | **Δ** `#1c1a17` | Hero cards, primary buttons, toggle-on, toasts, tooltips. Warm black, not neutral `#1a1a1a` |
| `--dark-hover` | **Δ** `#2e2b26` | Hover on `--dark` |
| `--onboard` | `#eae7e0` | Onboarding canvas |

**Ink**

| Token | Value | Use | Contrast |
|---|---|---|---|
| `--ink` | `#16150f` | Primary text | 17.7:1 on panel |
| `--muted` | **Δ** `#6b675f` | Labels, timestamps, hints | 5.4 panel · 5.1 canvas · 4.7 fill |
| `--faint` | **Δ** `#8f8a80` | Placeholders, disabled text, the unmeasured em dash | ≥ 3.1:1 everywhere — never for information a user must read |
| `--on-dark` | `#ffffff` | Text on `--dark` | 17.4:1 |
| `--on-dark-muted` | **Δ** `#a8a49b` | Secondary text on `--dark` | 7.0:1 |

v1's `#77736b` was 4.2:1 on canvas — the sidebar sits on canvas, so sidebar
hints failed AA. `--muted` now passes on every surface it lands on.

**The four accents** — each has exactly one job:

| Token | Value | Fill | Text on fill | Its one job |
|---|---|---|---|---|
| `--indigo` | `#4f46e5` | `--indigo-fill #eef2ff` | `--indigo` | **Sway, and where you are.** Brand mark, onboarding progress, selection, focus rings, Beta |
| `--amber` | `#ffb84d` | `--amber-fill #fff1d9` | **Δ** `--amber-ink #7a4a00` | **The hotkey — in every state**, and newness |
| `--teal` | `#17706b` | `--teal-fill #d9ebe9` | `--teal` | **Data, and only data.** Charts, progress, quotas, healthy dot, diff insertions |
| `--red` | `#e5484d` | `--red-fill #fdecec` | **Δ** `--red-ink #c8373c` | **The user must act.** Badges, actionable errors, conflicts, danger buttons |

- **Δ `--red-ink` for all red text.** `#e5484d` as text is 3.8:1 — it fails.
  Use `--red` only for dots, borders and icon strokes; red *text*, danger
  button labels, and count badges (white digits on a `--red-ink` pill, 5.2:1)
  use `--red-ink`.
- **Δ "Key is held" moves from indigo to amber.** v1 gave indigo five jobs.
  The hotkey is amber whether it's resting, highlighted, or pressed; pressed
  is a state of the same key, not a new meaning (§7 Key caps).

Teal ramp for heatmaps: `--fill` → `#cfe3e1` → `#8fbfbb` → `#479690` → `--teal`.

### 3.2 Overlay palette (`branding.py`, RGB)

| Name | Value | Use |
|---|---|---|
| `SURFACE` | `(26, 27, 32)` | Capsule fill |
| `SURFACE_RAISED` | `(38, 40, 47)` | Buttons inside the capsule |
| `SURFACE_HOVER` | **Δ** `(48, 50, 58)` | Hover on raised buttons |
| `INK` | `(235, 236, 240)` | Capsule text |
| `INK_MUTED` | **Δ** `(150, 153, 165)` | Secondary capsule text (timer, word count) |
| `ACCENT_BRAND` / `ACCENT_THINKING` | `(79, 70, 229)` | Transcribing, Pasting |
| `ACCENT_RECORDING` | `(255, 184, 77)` | Listening — the hotkey being held |
| `ACCENT_ERROR` | `(229, 72, 77)` | Errors the user must fix |
| `ACCENT_IDLE` | `(108, 111, 125)` | Idle, Done, Paused — deliberately *not* a fifth hue |

The bar's popover menu uses the dashboard palette: background `--field`,
ink `--ink`, muted `--muted`, rule `--separator`, hover `--fill`, with the
`--shadow-pop` elevation (§5.5).

### 3.3 Colour rules

- **Red means "act on this."** A transient network error or a rate limit is
  **muted**, not red — there is nothing for the user to do.
- **Red never goes on recording.** Recording is amber because it *is* the hotkey.
- **Active nav is a fill, not a colour.** Active tab is ink weight + a 2px ink
  underline. Toggle "on" is a `--dark` track.
- **Colour is never the only signal.** Every state also changes shape, glyph,
  or words.
- **No new accent tokens.** If a fifth colour seems necessary, the design is
  wrong, not the palette.
- **Δ No raw hex outside `:root`.** Every colour in `styles.css` is a `var()`.
  A grep for `#` outside the token block should return nothing.

---

## 4. Typography

| Family | Stack | Meaning |
|---|---|---|
| **Serif** `--serif` | `"Iowan Old Style", Georgia, "Times New Roman", serif` | **The app talking to you.** Onboarding headlines, hero cards, sheet titles, big personal numbers |
| **Sans** `--sans` | see optical sizes below | **The app being used.** Nav, labels, body, buttons, data |
| Mono `--mono` | `"Cascadia Mono", Consolas, monospace` 13px | Paths, raw values, logs |

**Δ Optical sizes.** Segoe UI Variable ships three cuts, and using the Text cut
at 30px is a big part of why titles looked heavy and loose:

| Token | Stack | Use at |
|---|---|---|
| `--sans-display` | `"Segoe UI Variable Display", "Segoe UI", system-ui, sans-serif` | 20px and up |
| `--sans` | `"Segoe UI Variable Text", "Segoe UI", system-ui, sans-serif` | 14–19px |
| `--sans-small` | `"Segoe UI Variable Small", "Segoe UI", system-ui, sans-serif` | 13px and below |

Never mix serif and sans in one functional string.

**Δ The scale — exact sizes, no ranges.** v1's "16–20px" and "25–28px" let
every screen pick its own size.

| Level | Token | Family | Size / line-height | Weight | Tracking | Example |
|---|---|---|---|---|---|---|
| 1 Display | `--t-display` | serif | 44 / 52 | 400 | -0.015em | Onboarding "Talk. It types." |
| 1 Hero | `--t-hero` | serif | 28 / 36 | 400 | -0.01em | Hero card, sheet title |
| — Big number | `--t-number` | serif | 34 / 40 | 400 | -0.01em | "1,204" in a stat tile |
| 2 Page title | `--t-title` | sans-display | 28 / 36 | 600 | -0.02em | "Dictionary" |
| 3 Section | `--t-section` | sans-display | 20 / 28 | 600 | -0.01em | "Recent errors" |
| 3 Card title | `--t-card` | sans | 16 / 24 | 600 | 0 | Status headline, setting group |
| 4 Body | `--t-body` | sans | 15 / 22 | 400 | 0 | Rows, settings, nav |
| 4 Secondary | `--t-secondary` | sans | 14 / 20 | 400 / 500 | 0 | Descriptions, buttons (500) |
| 5 Small | `--t-small` | sans-small | 13 / 18 | 400 | 0 | Hints, helper text, timestamps |
| 5 Meta | `--t-meta` | sans-small | 12 / 16 | 600 | 0.08em, uppercase | "WORDS THIS WEEK" |

- Weights: **400, 500, 600 only.**
- Every number that can change uses `font-variant-numeric: tabular-nums`.
  Δ Apply it on `.stat-value`, timestamps, counts, badges, timers and table
  cells by default, not per case.
- Δ Use `text-wrap: balance` on titles and hero headlines (no single-word last
  lines) and `text-wrap: pretty` on body paragraphs.
- Δ Numbers ≥ 10,000 abbreviate: `12.4K`, `1.2M`. Below that, full digits
  with a thousands separator: `1,204`.
- The overlay uses Segoe UI Semibold/Regular at 13px (labels), 12px (pills),
  11px (key chips, tooltips). Non-Latin scripts fall back via `_SCRIPT_FONTS`.

---

## 5. Layout, spacing, sizing

### 5.1 Dashboard shell

```
┌────────────────┬──────────────────────────────────────────────────┐
│  canvas        │ ┌── panel (#fcfbf9, radius 16, inset 12) ──────┐ │
│                │ │  48 ↓                                         │ │
│  Sway          │ │  Page title                          [action] │ │
│                │ │  32 ↓                                         │ │
│  ▢ Home        │ │  Tab  Tab  Tab ──────────────────── [tools]   │ │
│  ▢ History     │ │  ──────────────────────────────────────────── │ │
│  ▢ Dictionary  │ │  32 ↓                                         │ │
│  ▢ Snippets    │ │  Hero / content                               │ │
│  ▢ Transforms  │ │  48 ↓ between sections                        │ │
│  ▢ Insights    │ │                                               │ │
│                │ │  ←48→                           max-width 1120│ │
│  ────────────  │ │                                               │ │
│  ▢ Settings    │ │                                               │ │
│  ▢ About       │ └───────────────────────────────────────────────┘ │
└────248px───────┴──────────────────────────────────────────────────┘
```

The sidebar sits on the canvas; the content panel floats. That's what makes
the nav recede.

- Δ The page's single primary action sits on the **title row, right-aligned**
  (as the reference's "Add new"). Search, sort and filter tools sit on the
  **tab row, right-aligned**, as 32px icon buttons.
- Δ When the panel scrolls, the tab row sticks to the top of the panel and a
  `--separator` rule fades in under it (140ms). The title scrolls away.

### 5.2 Δ Spacing scale (4px base)

Every margin, padding and gap is one of these. Nothing else.

| Token | px | Typical use |
|---|---|---|
| `--s-1` | 4 | Icon ↔ label in a chip, nav item gap |
| `--s-2` | 8 | Icon ↔ text, button group gap, tooltip offset |
| `--s-3` | 12 | Panel inset, label ↔ input, padding in small controls |
| `--s-4` | 16 | Button horizontal padding, row padding, stack gap inside a card |
| `--s-5` | 20 | Row padding (horizontal) |
| `--s-6` | 24 | Card padding, gutter between cards |
| `--s-8` | 32 | Title → tabs, rule → content, modal padding |
| `--s-10` | 40 | Onboarding vertical rhythm |
| `--s-12` | 48 | Panel horizontal padding, panel top → title, between sections |
| `--s-16` | 64 | Onboarding block gaps |

**Mapping from v1:** 56→48, 60→48, 70→32, 35→32, 50→48, 28→24, 22×24→24.

**Fixed dimensions**

| Token | Value | Meaning |
|---|---|---|
| `--sidebar-w` | Δ 248px | Fixed sidebar |
| `--sidebar-collapsed-w` | Δ 64px | Icon rail (§5.6) |
| `--panel-inset` | 12px | Panel gap from window edge |
| `--content-max` | Δ 1120px | Content column cap |
| `--row-h` | Δ 64px | List row and setting row min height |
| `--nav-h` | Δ 44px | Sidebar item; items are 4px apart |

### 5.3 Δ Control heights — three, everywhere

| Size | Height | Used by | Padding-x | Radius |
|---|---|---|---|---|
| Small | **32** | `.btn.small`, `.icon-btn`, chips, tab-row tools, key caps in rows | 12 | `--r-sm` |
| Default | **40** | `.btn`, inputs, selects, search, segmented controls | 16 | `--r-md` |
| Large | **48** | Onboarding CTA, the single action on a hero | 20 | `--r-md` |

Anything that sits on one line with something else must share a height. A
button beside an input is 40, not 38.

### 5.4 Δ Radii

| Token | Value | Use |
|---|---|---|
| `--r-sm` | 8px | Small controls, key caps, chips' inner, tooltips |
| `--r-md` | 10px | Buttons, inputs, toasts, banners, menu items |
| `--r-lg` | 16px | Panel, cards, list containers, hero, drawer |
| `--r-xl` | 20px | Modals, wide sheets |
| `--r-pill` | 999px | Badges, toggles, chips that are pills by design |

**Nesting rule:** an inner corner = outer radius − the gap between them,
minimum 6px. A 16px card with 8px padding holds 8px-radius children. Mismatched
concentric corners are one of the most visible "unfinished" tells.

### 5.5 Δ Elevation

Cards are flat. Only things that **float above the page** get a shadow, and
shadows are warm-tinted so they don't go grey on the warm canvas.

| Token | Value | Use |
|---|---|---|
| `--shadow-none` | `none` | Panel, cards, rows, hero |
| `--shadow-pop` | `0 0 0 1px rgb(22 21 15 / .06), 0 4px 12px -2px rgb(22 21 15 / .10)` | Menus, popovers, selects' list, tooltips |
| `--shadow-toast` | `0 6px 20px -4px rgb(22 21 15 / .28)` | Toasts |
| `--shadow-modal` | `0 0 0 1px rgb(22 21 15 / .06), 0 24px 48px -12px rgb(22 21 15 / .22)` | Modals, sheets, drawer |
| `--scrim` | `rgb(22 21 15 / .28)` | Behind modals, sheets, drawer. No backdrop blur |

### 5.6 Δ Window sizing

v1 left this open. Closed:

- **Minimum window size 960 × 640.** Enforced by the host, not CSS.
- **≥ 1180px wide:** full sidebar.
- **960–1179px wide:** sidebar auto-collapses to a 64px icon rail; labels
  appear as tooltips. A toggle at the top of the sidebar overrides this and
  the choice persists.
- Card grids reflow by `minmax()`, never by a breakpoint per page:
  `grid-template-columns: repeat(auto-fill, minmax(280px, 1fr))`.
- Window size, position, maximized state and sidebar state are restored on
  next launch.

### 5.7 Density

Deliberately low. Large type, generous padding, few items per screen. When in
doubt, remove something rather than shrink it.

---

## 6. Information architecture

### 6.1 Two modes, hard separated

1. **Onboarding** — full-screen, no sidebar. A rail across the top, one
   decision per screen, a Back button in the body. The user can't wander.
2. **App** — persistent sidebar, reached only after onboarding completes.

### 6.2 Navigation

One flat level. Ordered by frequency of use.

| Group | Items |
|---|---|
| Primary | Home · History · Dictionary · Snippets · Transforms · Insights |
| Secondary (after divider) | Settings · About |

- Sub-navigation is **tabs directly under the page title**, never nested nav.
  - Insights: Your usage · Your speed · Performance
  - Settings: General · Voice · Apps · Data
- Δ Tabs: 15px, `--muted` at rest, `--ink` 500 on hover, `--ink` 600 when
  active, with a 2px `--ink` underline that **slides** between tabs (200ms,
  `--ease-standard`). Tab state is in the URL hash so Back/refresh keep it.
- Settings is demoted to the bottom group but **badged** when it needs the user.
- Red count badges only count problems **that screen can fix**.
- Δ Keyboard: `Ctrl+1`…`Ctrl+6` jump to the primary items, `Ctrl+,` opens
  Settings, `Ctrl+K` focuses the page's search when it has one.

### 6.3 Onboarding rail

`WELCOME › PERMISSIONS › API KEY › SET UP › TRY IT`

- Uppercase `--t-meta` labels with `›` separators.
- One continuous indigo underline whose **width carries progress**.
- The rail is not clickable navigation.
- The last step is the real pipeline, not a simulation — and closes the loop
  with a measured number ("47 words — that is Sway working.").
- Δ Each step has exactly one `.btn.primary` (Large, 48px), bottom-right of
  the content; Back is `.btn.ghost`, bottom-left. `Enter` triggers the primary
  action when it's enabled.

### 6.4 Where things open

| Pattern | Width | Use for |
|---|---|---|
| **Page** | — | Anything in the sidebar |
| **Drawer** (right) | 560px | Detail of one item — raw vs. cleaned text, timings |
| **Modal** | 560px | A short focused task or a destructive confirm |
| **Wide sheet** (two columns) | 960px | Editing a transform — prompt one side, live example the other |
| **Toast** | ≤ 420px | Confirmation of a completed action, optional Undo (§11) |
| **Banner** | full content width | A persistent condition on the current page |

Δ All overlays: open with focus on the first field (or the safe button in a
confirm), trap focus, close on `Esc` and on scrim click (except while a form
inside has unsaved, unvalidated input), and **return focus to the element
that opened them**.

---

## 7. Components

All built once in `app.js` (helpers at the top) and styled once in
`styles.css`. Reuse them; don't restyle per page.

### Δ Interaction states — every interactive element has all five

| State | Treatment |
|---|---|
| Rest | As specified per component |
| Hover | Background steps one rung up the ladder (`--fill` → `--fill-hover`, transparent → `--row-hover`, `--dark` → `--dark-hover`). 120ms. Never a colour change on text alone |
| Pressed (`:active`) | `transform: scale(.98)` 90ms in, 160ms out. Icon buttons `scale(.94)` |
| Focus (`:focus-visible`) | 2px `--indigo` outline, 2px offset. Never on mouse click |
| Disabled | 45% opacity, `cursor: default`, no hover, `aria-disabled`. Δ A disabled primary button gets a tooltip saying what enables it ("Add a name to save") |

Δ Cursor: `default` (arrow) on buttons, tabs, nav and rows — that's the
Windows-native convention, and a hand cursor on every control is one of the
"this is a web page" tells. The hand is for real links only.

### Buttons

| Class | Look | Rule |
|---|---|---|
| `.btn.primary` | `--dark` fill, white text | **At most one per page.** The page's main action |
| `.btn` | Δ `--fill`, ink text, **no border** | Secondary actions |
| `.btn.ghost` | No fill; `--row-hover` on hover | Back, Cancel, low-emphasis |
| `.btn.danger` | Δ `--red-fill` fill, `--red-ink` text | Destructive only, always confirmed or undoable |
| `.btn.small` | 32px | Inside cards and rows |
| `.icon-btn` | 32px square, `--muted` icon → `--ink` on hover | Always `aria-label` and a tooltip |

Default 40px, 14px/500, `--r-md`, 16px horizontal padding, 8px icon gap.
Labels are **verb + noun, ≤ 3 words**: "Add word", "Save transform". Never
"OK", "Submit", "Yes".

Δ **Busy state.** A button whose action takes > 150ms keeps its width, swaps
its icon (or prepends one) for a 14px spinner, changes the label to the
present participle ("Saving…"), and ignores further clicks. On success it
shows the past tense ("Saved") with a check for 1.2s, then reverts.

### Inputs

- 40px, `--field` fill, 1px `--field-border`, `--r-md`, 12px horizontal padding.
- Δ Hover: border `#cdc8be`. Focus: `--indigo` border + 3px `--indigo-fill`
  halo. Error: `--red` border + `--red-fill` halo when focused.
  Disabled: `--card` fill, `--faint` text.
- Δ Label above (14/500 ink, 8px gap). Helper text below (13 muted, 8px gap).
  The error message **replaces** the helper text in the same slot, so the
  layout doesn't jump: 13px `--red-ink`, leading 14px alert icon.
- Δ Validate **on blur and on submit**, never on each keystroke. Clear the
  error as soon as the input becomes valid.
- Δ Placeholders are examples, not instructions: "Boss mode", not "Enter a
  name". `--faint`.
- Δ Fields with a character limit show `12 / 40` (13, `--faint`, tabular)
  only once within 80% of the limit.
- Selects use a custom chevron; the open list uses `--shadow-pop` and is
  keyboard navigable (arrows, type-ahead, Enter, Esc).
- Search inputs carry a 16px leading icon and a clear (×) button once they
  have a value. `Esc` clears, then blurs.

### Toggle

42×24 pill. Off = `#c9c4ba` track. On = `--dark` track. White 20px knob with a
`0 1px 2px rgb(22 21 15 / .2)` shadow, travels in 160ms `--ease-standard`.
Always `role="switch"` + `aria-checked`. Label and hint sit to its left in a
`.setting` row; clicking the label toggles it. Δ Toggles apply **immediately**
— a toggle never needs a Save button.

### Key caps

| Class | Meaning |
|---|---|
| `.cap` | Neutral key: `--field` fill, 1px `--field-border`, 0 1px 0 `--field-border` bottom edge, `--t-small` 500 |
| `.cap.hot` | Amber — "this is your hotkey": `--amber-fill`, `--amber` border, `--amber-ink` text |
| `.cap.held` | Δ Amber, **pressed**: `--amber` fill, ink text, bottom edge removed, `translateY(1px)` — the key is physically down |
| `.cap.big` | 76px capture box; `.capturing` pulses (1.6s, opacity .55→1) to invite a press |
| `.cap-row.conflict` | One 240ms shake on a detected collision. Not a persistent wobble |

### Chips and pairs

Pill, 32px, `--fill`, 12px padding, 14/500. Hover `--fill-hover`.
`.selected` = `--indigo-fill` + `--indigo` 1px inset ring. `.on-dark` for
chips on hero cards (`rgb(255 255 255 / .12)` fill, white text, `.2` on
hover). A **pair** (`"trigger" → expansion`) explains a mapping with no prose.

### Cards and stats

- `.card` — Δ `--card` fill, **no border**, `--r-lg`, 24px padding.
  A card on the canvas (sidebar) uses `--field` instead so it still separates.
- `stat({label, value, foot, estimate})` — `--t-number` serif over `--t-meta`.
  - `value: null` → em dash in `--faint`, with `title="Not measured yet"`.
    **Never 0.**
  - `estimate: true` → an "Estimate" line in `--t-small` muted, with an info
    tooltip naming the assumption ("Assumes you type at 40 wpm").
  - Δ A changed number animates by **cross-fade (140ms), not by counting up**.
    Counting up implies a measurement in progress that isn't happening.
- `.hero` — `--dark`, `--r-lg`, 32px padding, serif headline, sans body, one
  action. Δ Depth without photography (CSP-safe, no images):
  `background: radial-gradient(120% 90% at 100% 0%, rgb(79 70 229 / .22), transparent 60%), radial-gradient(90% 80% at 0% 100%, rgb(255 184 77 / .10), transparent 55%), var(--dark);`
  This is Sway's identity against Wispr's photo heroes — keep it subtle.
  A dismissable hero has a 32px `.icon-btn` close (top-right, 16px inset) and
  the dismissal persists.

### Δ Lists — rows live in a container

Rows are grouped in one `.list` container, not floated as loose lines:
`--panel` fill, 1px `--separator` border, `--r-lg`, `overflow: hidden`.
A date or section header (`--t-meta`) sits 12px above its container, and
containers are 32px apart.

- Row: 64px min, 20px horizontal padding, 1px `--separator` between rows
  (not after the last). Muted fixed-width left column (time, 88px,
  tabular-nums); body truncates with an ellipsis and a full-text tooltip.
- Hover: `--row-hover` (120ms). The whole row is the click target for its
  primary action (open drawer).
- **Row actions** are 32px icon buttons, right-aligned, 4px apart, revealed
  on row hover and `:focus-within` with a 120ms fade, and always in the DOM.
  At most three; anything more goes in a `⋯` menu.
- Delete: the row collapses (height + opacity, 200ms) and an "Deleted · Undo"
  toast appears. Undo restores it in place.
- New items appear at their sorted position with a one-time `--indigo-fill`
  background that fades out over 1.2s, so the user sees where it went.

### Status

- `.dot` — 8px. `.ok` teal, `.warn` muted, `.act` red.
- Status card — dot + `--t-card` headline + 13px muted explanation + at most
  one button that goes **straight to the field that fixes it** (and focuses it).

### Progress

4px track in `--fill`, teal fill, `--r-pill`. **Always paired with a caption
saying what completes it** ("Needs 50 · 12 to go", "Ready in 47s").
Width changes in 240ms `--ease-standard`.

### Charts

Teal only. Bars on a `--teal-fill` track; heatmap uses the 5-step ramp with
3px-radius cells and 4px gaps. Latency waterfall uses teal, with amber
only to mark a stage over budget. Δ Every bar, cell and point has a tooltip
with the exact value and its unit/date. Axes and gridlines use `--separator`;
labels `--t-small` muted.

### Diff

Deletions: `--muted` + strikethrough. Insertions: `--teal-fill` background,
ink text, 3px radius, `box-decoration-break: clone` so wrapped highlights keep
their corners.

### Icons

Inline SVG sprite in `index.html`, 20×20 viewBox. Δ **One stroke width:
1.5px** (v1's 1.4–1.9 range shows up as icons of different weights side by
side). Round caps and joins, `currentColor`. Rendered at 20px in nav and
buttons, 16px in inputs and dense rows. Line icons only — no emoji as icons.
Δ Every `<svg>` and `<img>` in the UI has `draggable="false"`.

---

## 8. The floating bar

The surface the user lives with all day. Rendered with PIL into a Tk window,
**never** a webview: a webview activates when shown and would steal the paste
target.

### 8.1 Three tiers

1. **At rest** — a 48×16 outlined tray, interior at 30% opacity. Swells 1.5px
   on hover.
2. **Engaged** — the tray morphs (one renderer, `expand` 0→1) into a 44px dark
   capsule: orb avatar (34px), state label, level meter, and in Idle the two
   controls people change mid-session — language and microphone.
3. **Menu** — right-click or click the body. Pause/Resume, Hide for 1 hour /
   Unhide, Settings, Language ›, Microphone ›, Transcript history, Paste last,
   Copy last. Rows reflect the current state.

Hover must rest **400ms** before the capsule opens and be gone **250ms**
before it closes.

### 8.2 States

| State | Label | Accent | Orb | Holds |
|---|---|---|---|---|
| Idle | — | grey | breathing | collapses to tray |
| Listening | "Listening · 0:03" + level meter | **amber** | listening | while key held |
| Transcribing | "Transcribing" (names the real stage) | indigo | working | until done |
| Pasting | "Pasting" | indigo | composing | sub-100ms usually |
| Done | a fact: "47 words · 0.8s" | grey | still glyph | 1.6s, then fades |
| Error | short plain-language cause + recovery pill | **red** if actionable | still glyph | 4s, **Δ paused while hovered** |
| Paused | "Paused" + Resume | grey | frozen | until resumed |
| Coach | a hint, may contain `[[Key]]` chips | amber | breathing | 2.8s |

- **Only states the engine actually reports get an animation.**
- Accent changes cross-fade over 150ms.
- Controls show only in Idle.
- Δ The timer in "Listening · 0:03" is tabular and in `INK_MUTED`, so the
  label width doesn't twitch every second.
- Δ The label cross-fades (120ms) between states; the capsule width animates
  (`WIDTH_S`) to the new label **after** the fade-out half, so text never
  overflows a shrinking capsule.

### 8.3 Level meter

Three discrete bars: silent `▯▯▯` / speech `▮▮▯` / loud `▮▮▮`. Re-renders on a
bucket change, not every frame. If the whole hold was silent, show
"No speech detected" and **skip the API call**.

### 8.4 Errors and recovery

Every error offers one inline recovery pill, and only one that actually works:

| Cause | Label | Pill |
|---|---|---|
| Paste blocked (elevated window) | "Can't paste into this window" | Copy |
| Clipboard failure | "Clipboard was busy" | Paste |
| No speech | "No speech detected" | Mic → microphone menu |
| Mic in use / missing | "Microphone unavailable" | Fix → microphone menu |
| Bad or missing API key | "API key needs attention" | Fix → Settings › General, key field focused |
| Network / timeout | "Couldn't reach Groq" | Copy (only if there's a transcript) |
| Rate limited | "Groq limit — ready in 23s" | *none* — counts down, rounds up |

A button that claims to retry and doesn't is worse than no button.

### 8.5 Δ Rendering quality

These are what separate a native-feeling overlay from a prototype one:

- **Per-pixel alpha, not a colour key.** Tk's `-transparentcolor` is 1-bit:
  every anti-aliased edge PIL draws gets a fringe of the key colour and the
  capsule's corners look stair-stepped. Render the RGBA frame and present it
  with `UpdateLayeredWindow(..., ULW_ALPHA)` on a `WS_EX_LAYERED` window.
  This also makes a soft shadow possible: draw
  `0 4px 16px rgba(0,0,0,.35)` into the frame around the capsule.
- **Never activatable.** `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST`
  (no taskbar button, no Alt+Tab entry, can't take focus on click).
- **Per-monitor DPI aware (v2).** Call
  `SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)`
  before any window exists, read the monitor's DPI, and render PIL frames at
  `logical size × scale`. At 125/150% without this the bar is bitmap-scaled
  and blurry. Re-render on `WM_DPICHANGED` when it moves between monitors.
- **Text** is drawn at the device scale too, with the same Segoe UI weights as
  §4, so the bar and dashboard read as one product.
- **Position** is relative to the work area of the monitor with the focused
  window (above the taskbar, 16px clearance), and recomputed when the
  taskbar or display changes.

---

## 9. Empty, loading, and not-yet states

### 9.1 Empty and zero — pick by cause, not by page

| Cause | Treatment | Helper |
|---|---|---|
| Never configured | Hero card with **worked examples** of what would be here + one create action | `hero()` |
| Measured zero | Print `0`. Zero is a fact | `stat({value: 0})` |
| Not measured | Em dash in `--faint`, never `0` | `stat({value: null})` |
| Not enough data yet | The real skeleton at 42% opacity + exact remaining count. **No blur, no fake content** | `locked()` |
| Filter matched nothing | Centred muted text naming the query + a "Clear search" ghost button | `nothingFound()` |
| Feature unavailable here | Info banner saying why | `.banner.info` |

Bad: "No data yet!", "All good ✓", a dashed box with "Nothing here".
Good: "0 errors in the last 30 days, across 412 dictations."
Good: "No words match “karol”." + Clear search.

### 9.2 Δ Loading — by how long it takes

| Duration | Treatment |
|---|---|
| < 150ms | **Nothing.** Showing a spinner for 80ms is a flicker, which reads as a glitch |
| 150ms – 1s | Busy state on the thing that was clicked (§7 Buttons). Page content stays put |
| > 1s, or first load of a page | **Skeleton of the real layout**: `--card`/`--fill` blocks at the exact size and position of the content they'll become, with a slow pulse (1.4s, opacity .6 → 1). Never a centred page spinner |
| > 10s | Add a caption with the real stage and progress if known ("Rebuilding index · 1,204 of 3,000") |

- Once shown, a loading state stays at least 300ms, so it doesn't flash off.
- The app shell (sidebar, panel, page title, tabs) renders **instantly**; only
  data regions load.
- Δ Content arriving after a skeleton cross-fades in (140ms). Nothing shifts:
  skeletons reserve the exact height, and images/charts have fixed aspect.
- Reduced motion: skeletons are static.

### 9.3 Δ Optimistic by default

Toggles, renames, reorders, favourites and deletes update the UI **immediately**
and sync in the background. If the save fails, revert the change, and say so
inline next to it ("Couldn't save — try again" with a Retry ghost button that
actually retries). Only network-bound creation (validating an API key) waits.

---

## 10. Motion

Durations live in one table in `motion.py`, mirrored as CSS custom properties.
Animation is a **function of wall clock**, sampled per frame — never a stepped
timer.

### 10.1 Δ Easing — three curves, nothing else

v1 named durations but no curves; every transition fell back to `ease`, which
is what makes motion feel "web default".

| Token | Curve | Use |
|---|---|---|
| `--ease-standard` | `cubic-bezier(.2, 0, 0, 1)` | State changes on screen: colours, toggles, tab underline, widths |
| `--ease-enter` | `cubic-bezier(.16, 1, .3, 1)` | Things arriving: modals, drawers, menus, toasts |
| `--ease-exit` | `cubic-bezier(.4, 0, 1, 1)` | Things leaving. Always shorter than the enter |

`motion.py` implements the same three as functions of `t ∈ [0,1]` so the bar
and the dashboard move with the same character.

### 10.2 Overlay durations (`motion.py`)

| Constant | ms | Use |
|---|---|---|
| `PRESS_DOWN_S` | 90 | Button press |
| `LEVEL_S` | 90 | Level meter step |
| `PANEL_CLOSE_S` | 110 | Menu close |
| `BUTTON_FILL_S` | 120 | Button fill |
| `LABEL_FADE_S` | Δ 120 | State label cross-fade |
| `TOOLTIP_S` | 120 | Tooltip fade |
| `DOT_HOVER_S` | 140 | Tray hover swell |
| `ACCENT_S` | Δ 150 | Accent cross-fade |
| `PANEL_OPEN_S` | 160 | Menu open |
| `PRESS_UP_S` | 160 | Button release |
| `WIDTH_S` | 170 | Capsule width change |
| `DOT_UNHOVER_S` | 180 | Tray un-hover |
| `COLLAPSE_S` | 200 | Capsule → tray |
| `SHAKE_S` | 240 | Error shake |
| `FADE_OUT_S` | 260 | Done fade |
| `TOOLTIP_DELAY_S` | 600 | Rest before tooltip |

### 10.3 Δ Dashboard motion

| What | Enter | Exit | Detail |
|---|---|---|---|
| Hover backgrounds | 120ms standard | 120ms | `background-color` only — never `transition: all` |
| Press | 90ms | 160ms | `scale(.98)` |
| Toggle knob | 160ms standard | — | |
| Tab underline | 200ms standard | — | Slides between tabs |
| Page change | 140ms | 0 | New page fades in + rises 4px. Old page is removed at once — no cross-page choreography |
| Menu / popover | 140ms enter | 100ms exit | Opacity + `scale(.97 → 1)` from the trigger's side |
| Modal / sheet | 200ms enter | 140ms exit | Opacity + `scale(.98 → 1)` + `translateY(8px → 0)`; scrim fades 180ms |
| Drawer | 220ms enter | 160ms exit | `translateX(24px → 0)` + opacity |
| Toast | 200ms enter | 140ms exit | Rises 8px |
| Row delete | 200ms | — | Height + opacity collapse |
| Progress width | 240ms standard | — | |
| Onboarding rail | 320ms standard | — | The one deliberate exception to the band: it carries progress |

Rules:

- Stay in **90–260ms** (rail excepted). Exit faster than enter.
- Animate only `opacity`, `transform`, `background-color`, `border-color`,
  `box-shadow`. Never `width`/`height`/`top`/`left` except row collapse and
  progress, and never `transition: all`.
- No staggered list entrances. Lists appear whole.
- The bar runs ~60fps only while something moves or a dictation is live;
  150ms polling at rest.
- `prefers-reduced-motion`: transitions collapse to opacity-only at 0–80ms,
  the orb uses its frozen frame, skeletons don't pulse.

---

## 11. Feedback, tooltips, toasts, copy

### 11.1 Δ Which feedback, when

| The user… | Feedback |
|---|---|
| Changed a setting or toggle | The control itself. **No toast.** If it needs a restart, an inline note under the row: "Restart Sway to apply" |
| Edited a sheet with autosave | "Saving…" → "Saved" in `--t-small` muted next to the sheet title. **No toast** |
| Copied something | The copy icon swaps to a check for 1.2s and its tooltip reads "Copied". **No toast** |
| Did something undoable or off-screen (delete, bulk action, import) | Toast, with Undo when undoable |
| Hit an error they can fix | Inline, next to the cause, with one fix button. **Never only a toast** |
| Hit an error they can't fix | Muted inline note or banner, stating what happens next ("Retrying in 12s") |

### 11.2 Δ Toasts

- Bottom centre of the panel, 24px above its bottom edge. `--dark`,
  `--r-md`, 40px min height, 16px padding, `--shadow-toast`, ≤ 420px wide.
- Text `--t-secondary` white; action link `--amber`, 500, separated by `·`.
- **One at a time.** A new toast replaces the current one (exit, then enter).
- 4s, or 6s with an action. The timer **pauses on hover and on focus**.
- `role="status"`, `aria-live="polite"`. Undo is reachable by keyboard
  (`Ctrl+Z` triggers it while the toast is up).
- Copy: past tense, short. "Saved", "Word added", "Deleted · Undo",
  "3 entries deleted · Undo".

### 11.3 Δ Tooltips (dashboard)

- `--dark`, white `--t-small` 500, 6px × 8px padding, `--r-sm`, `--shadow-pop`,
  max 240px wide. No arrow. 8px from the trigger, above by default, flipped to
  stay in the window.
- **600ms delay** for the first tooltip; **0ms** when moving to another
  tooltipped element within 300ms of the last one closing. Fade 120ms.
- Keyboard focus shows the tooltip immediately; `Esc` hides it.
- Tooltips may include a shortcut as key caps: "Search  `Ctrl` `K`".
- Every icon-only button has one. Truncated text has one with the full text.
  Nothing essential lives *only* in a tooltip.

### 11.4 Voice and copy

- **Second person, plain words.** "Your text is on the clipboard — press
  Ctrl+V." Not "Clipboard injection failed (E_PASTE)".
- **Name the cause and the fix.** "Microphone is in use — another app has
  exclusive control." Then one button.
- **No error codes, no stack traces, no tokens** in the UI. Diagnostics live
  in Settings › Data and the log file.
- **Overlay labels ≤ ~34 characters.**
- **Sentence case** everywhere except `.meta-label` (uppercase by CSS — write
  it in sentence case in the source).
- **Em dash** (`—`) joins a headline and its reason: "Paused — no API key".
- **Curly quotes** (“ ”) and apostrophes (’) in UI text, a real ellipsis (`…`)
  in "Saving…".
- Δ **Ellipsis on a button label** only when the action needs more input
  before it happens ("Rename…", "Export…"), per Windows convention.
- **Countdowns round up.**
- **Promises must be true.**
- **Estimates say so**, every time, next to the number.
- Δ **Destructive confirms** name the thing and the consequence:
  title "Delete “intro email”?", body "This snippet will stop expanding.
  You can't undo this.", buttons "Delete snippet" (danger) · "Cancel" (ghost).
  Prefer Undo over a confirm whenever undo is possible.
- Δ **Microcopy patterns**

  | Moment | Write | Not |
  |---|---|---|
  | Empty search | No words match “karol”. | No results found |
  | Locked insight | Unlocks at 2,000 words · 1,900 to go | Not enough data |
  | Save failed | Couldn't save — check your connection | Error saving |
  | Key invalid | That key was rejected by Groq — check it's copied in full | Invalid API key |
  | Shortcut taken | Win + Alt is already used by “Prompt engineer” | Conflict detected |
  | Hotkey prompt | Hold `Right Ctrl` and speak | Press hotkey to start dictation |

---

## 12. Accessibility

- Visible focus via `:focus-visible` — 2px indigo outline, 2px offset. Never a
  ring on mouse click.
- Skip link to `#page` as the first focusable element.
- Hover-revealed actions also reveal on `:focus-within`.
- Icon-only buttons need `aria-label` and a tooltip.
- Toggles: `role="switch"` + `aria-checked`. Modals: `role="dialog"`,
  `aria-modal`, `aria-labelledby`, Esc closes, focus is trapped and restored.
- Colour is never the only signal (§3.3).
- Δ All text pairs in §3.1 meet AA (≥ 4.5:1). `--faint` is ≥ 3:1 and only for
  placeholders, disabled text and the unmeasured dash. Don't introduce a
  lighter text colour.
- Δ Live regions: toasts `polite`; inline errors `role="alert"` only when they
  appear in response to submit.
- The window is an app, not a document: `user-select: none` by default;
  transcripts, inputs, and `.selectable` stay selectable.
- Δ Targets are ≥ 32×32 px (small controls) — 24px icons get 32px hit areas.

---

## 13. Δ Native-window behaviour (WebView2 host)

The difference between "a web page in a frame" and "an app". All of these are
host settings, set once:

| Setting | Value | Why |
|---|---|---|
| `DefaultBackgroundColor` | `--canvas` (`#f5f3ef`) | No white flash before first paint |
| Show window | only after the first `NavigationCompleted` | No blank frame on launch |
| `AreBrowserAcceleratorKeysEnabled` | `false` | No F5 reload, Ctrl+P print, Ctrl+F find bar, Ctrl+R |
| `IsZoomControlEnabled` / `IsPinchZoomEnabled` | `false` | No Ctrl+wheel zoom; scaling follows Windows display scale |
| `AreDefaultContextMenusEnabled` | `false` | No "Inspect"/"Back" menu. Text fields get our own Cut/Copy/Paste menu |
| `AreDevToolsEnabled` | `false` in release builds | |
| `IsStatusBarEnabled` | `false` | No URL popup at bottom-left on link hover |
| `IsSwipeNavigationEnabled` | `false` | No accidental back on touchpads |
| New-window requests | handled: external `https` links open in the default browser; everything else is blocked | |

And in CSS / markup:

- `draggable="false"` on images and SVGs; `-webkit-user-drag: none` on links.
- `overscroll-behavior: none` on the panel — no rubber-banding.
- Δ Scrollbars: 10px, transparent track, `--fill-hover` thumb with 3px inset
  border in the panel colour (so it looks 4px wide), `--faint` thumb on hover.
  `scrollbar-gutter: stable` so content doesn't shift when a scrollbar appears.
- The custom title bar keeps Windows behaviour: drag region on the empty
  title area, double-click maximises, and the maximise button returns
  `HTMAXBUTTON` from `WM_NCHITTEST` so **Snap Layouts** appear on hover
  (Windows 11). Caption buttons are 46×32, the Windows standard.

---

## 14. Security and privacy in the UI

- Every string from the user, a transcript, or Groq is escaped with `esc()`
  before reaching `innerHTML`. No exceptions.
- The session token is scrubbed from the address bar on load and never
  rendered or logged.
- CSP stays strict: `default-src 'none'`, `script-src 'self'`. No external
  fonts, scripts, images, or analytics. Inline `style` only for data-driven
  values (widths, heights). The hero gradient (§7) lives in `styles.css`.
- The API key shows its *source* (Credential Manager vs. environment), never
  its value. Δ When the user pastes a key, the field shows it masked with its
  last 4 characters ("•••• 9fK2") and a Show toggle.

---

## 15. Checklist for a new screen or component

- [ ] Only tokens: no raw hex, no font sizes outside §4, and every margin, padding and gap on the §5.2 scale (component-internal values specified in §7, like tooltip padding, excepted).
- [ ] Fill *or* border, never both (inputs excepted). White only for fields and floating surfaces.
- [ ] Controls on one line share a height (32 / 40 / 48).
- [ ] Nested corners follow the nesting rule.
- [ ] Each accent used for its one job; red text uses `--red-ink`.
- [ ] At most one `.btn.primary`, on the title row.
- [ ] Every interactive element has rest, hover, pressed, focus and disabled.
- [ ] Every action gives feedback within 100ms (§11.1); slow ones have a busy state.
- [ ] Loading follows §9.2: nothing < 150ms, busy state < 1s, skeleton beyond.
- [ ] Every number is measured; unmeasured is `—`; assumptions say **Estimate**.
- [ ] Empty state chosen by cause (§9.1).
- [ ] Errors are inline, name cause + fix, red only if the user must act, one recovery action.
- [ ] Keyboard reachable, visible focus, labelled icon buttons with tooltips.
- [ ] Motion uses the three curves, within 90–260ms, transitions name their properties.
- [ ] Works at 960 × 640 and at 125% / 150% display scale.
- [ ] User and transcript text passed through `esc()`.
- [ ] Nothing on the overlay can take focus.
- [ ] Copy follows §11.4.
- [ ] This file updated if a rule or value changed.

---

## 16. Open questions

- **Style and Voice Profile pages** are named in CLAUDE.md Phase 6 but don't
  exist in the nav yet. When they ship, they follow the page skeleton
  (title → tabs → hero/content) and get a row in §6.2.
- **Brand mark.** `--indigo #4f46e5` is Tailwind's indigo-600 exactly, and
  `--red #e5484d` is Radix red-9 — both read as framework defaults to anyone
  who has seen them a hundred times. Worth one deliberate pass on a Sway-only
  indigo (e.g. a slightly warmer `#5146d9`–`#5b4fd6` range, re-checked for
  ≥ 4.5:1 on `--panel`) before launch. Not a blocker.
