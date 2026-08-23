# UI gap analysis — current Flow vs. the reference

Companion to [`ui-reference-analysis.md`](ui-reference-analysis.md). Section
references in the form §N point at that document. Code references are to the
repo as it stands on `d0-audit-baseline`.

**Closed decisions carried into this document** (not re-opened anywhere below):
light theme only; one theme; no theme switching; no theme abstraction layer.
The reference's single dark screen (6.png) is an editorial choice on one
screen, not a mode. Where a surface below is dark, it is dark permanently and
for a stated reason, not as a theme.

---

## 0. What "current Flow" actually is

Four separate UI surfaces exist today, built on three different technologies:

| Surface | Module | Tech | Reached how |
|---|---|---|---|
| Status pill | `src/flow/overlay.py` (450 ln) | tkinter + PIL compositing | Always on screen |
| Tray icon + menu | `src/flow/tray.py` (319 ln) | pystray + PIL | Notification area |
| History window | `src/flow/history_window.py` (176 ln) | tkinter + `ttk.Treeview` | Tray → Show History |
| Dashboard | `src/flow/dashboard/` (1259 ln Python + 1190 ln HTML/CSS/JS) | `http.server` on loopback + a browser tab | Tray → Open Dashboard |

Plus `flow.gui` (177 ln), which is not a UI so much as four `MessageBox` calls
standing in for one.

The task's framing is correct and worth restating precisely: the dashboard is
the only surface with real information architecture, and it is the one surface
that is not an application. The three that *are* native are a 44px pill, a
menu, and a `ttk.Treeview`.

---

## 1. Every view the http.server dashboard exposes → where it lands

Read off `static/index.html`, `static/app.js` (`views.*`), and
`dashboard/server.py`'s routes.

### 1.1 Chrome

| Current | Lands | Note |
|---|---|---|
| Sidebar, two labelled groups ("Productivity", "Engine") | **Kept**, restructured to the reference's two-group model (§2) | Reference uses an unlabelled divider, not section captions. Drop the captions. |
| Brand block: `F` tile + "Flow" | **Kept** | Reference's is a mark + wordmark (§1 7.png). We have a real mark in `flow.branding.mark_image` — use it, drop the letter `F`. |
| `.nav-item` with emoji-ish glyph icons (`◈ ≡ ◔ ⌁ ⚙`) | **Cut the glyphs** | PRODUCTION_BRIEF D2: "no emoji-as-icons". Reference uses line icons (§4). |
| Sidebar-foot mic toggle (`#mic-toggle`, disabled until engine attached) | **Home page**, as the agent-status control | Reference has no agent concept (§3 "What Flow has"). Its sidebar foot is a dismissible promo card, which we cut. |
| Topbar: page title + sub + toolbar slot | **Kept**, restyled | Reference has no sub-line under the title and no persistent toolbar; per-page actions sit top-right of the panel and sub-nav is tabs (§3, §4). The sub-line is ours to keep or drop — see Q3. |
| Range picker (7d/30d/90d/all), `segmented()` | **Kept on Insights only** | Reference has no global time-range control at all. Keeping it globally would be inventing chrome; keeping it where it changes a chart is honest. |
| Session drawer (`#drawer`, right-side slide-over) | **Kept**, as History's detail view | No reference equivalent. See §3. |
| Toast (`#toast`) | **Kept** | Used for "Copied". Reference shows none, but it is invisible until used. |

### 1.2 Views

| Current view | Contents | Lands in | Verdict |
|---|---|---|---|
| **`views.overview`** | Hero "time saved" metric; 5 today cards (words, sessions, avg WPM, median latency, streak); daily bar chart; app-usage table with share bars; consistency heatmap + 3 streak metrics | **Home** (today cards, agent status) + **Insights** (chart, heatmap, app usage) | **Splits.** Reference's Dictation home (§1 7.png) carries *three* numbers and today's list — not eleven cards. The analytical half belongs on Insights (§1 8.png), which is exactly where the reference puts the heatmap, the app-share bar and the streak. |
| **`views.activity`** | Debounced search input; 7-column table (When / App / Text / Words / WPM / Latency / Status); row click → drawer | **History** | **Kept, re-formed.** Reference has no tables anywhere. This becomes the §4 list row: fixed left time column, text, hover-revealed actions. Words/WPM/latency move into the detail view; `statusPill()` stays only when it is not "pasted". |
| **`views.insights`** | 4 WPM metrics (recent day, personal best, window avg, all-time avg); speaking-rate bar chart; 3 time-saved metrics | **Insights › Your speed** | **Kept.** Maps almost 1:1 onto §1 8.png's stat-card row + chart. The existing honesty ("Compare against the user's own history only… we do not invent one", `app.js:363`) is *better* than the reference's "Top 38%" percentile against an unstated cohort. Keep ours. |
| **`views.performance`** | Health banner (status/headline/detail + orb); 4 counters (total/succeeded/failed/error rate); p50/p95/p99/mean/min/max; stage waterfall with `.slow` flagging; failures grouped by error | **Insights › Performance** tab, plus the health banner to **Home** | **Kept, demoted.** CLAUDE.md Phase 6 fixes the metric hierarchy as productivity → usage → personalization → performance → infrastructure. Today Performance is a peer of Overview in the sidebar; that inverts the rule. It becomes a tab, not a destination. |
| **`views.settings`** | Read-only `describe()` key/value dump; a card explaining that editing "isn't wired up yet"; a privacy card | **Settings** (rebuilt) + **About** (privacy card) | **Replaced.** This view's entire content is an apology. The `describe()` dump (`engine.py:1054`) becomes the field *values* in the real Settings screen. |
| **Session drawer** | Final text + Copy; raw-vs-cleaned when they differ; per-session stage waterfall; 10-row metadata table | **History → detail** | **Kept as-is in substance.** The raw-vs-cleaned diff has no reference equivalent and is the single most useful thing in the current dashboard. |

### 1.3 API routes — none are cut

`/api/overview`, `/api/activity`, `/api/session`, `/api/performance`,
`/api/status`, `POST /api/listening`, `POST /api/focus` all survive unchanged.
`POST /api/focus` in particular is load-bearing for D1's second-launch focus
(`flow/instance.py`) and must not be disturbed by any host change.

**Consequence worth stating early:** the data layer is not the problem. 802
lines of `queries.py` already answer everything the new structure needs. The
work is entirely presentation and shell.

---

## 2. Every reference screen → need / variant / cut

### 2.1 Launch and onboarding (§1, 1–6)

| Reference | Verdict | Reasoning |
|---|---|---|
| **Splash with a stated duration** (1.png) | **Variant** | Their "up to 30 seconds" is theirs; D7 targets startup under 2s and D0 measured 1249 ms first-touch / ~300 ms warm on a 92 MB onedir bundle (`docs/d0-audit.md` finding 4). A 30-second promise would be a lie and a 300 ms splash is a flash. Derivation: show it only if startup crosses a threshold, and state the real thing being waited on. Detail in Q6. |
| **Onboarding rail, 5 phases** (§2) | **Variant** | `SIGN UP` is cut (no accounts, no service). Ours: `WELCOME › PERMISSIONS › API KEY › SET UP › TRY IT` — still five, still coarse, still one continuous progress underline. |
| **Language summary card** (2.png) | **Cut for now** | We have one setting, `[groq].language` (`config.py:181`), default `None` = Whisper auto-detects. A "set all the languages you speak" screen would be ceremony over a single optional field. Becomes one row in Settings › Voice. |
| **Language picker modal** (3.png) | **Cut as a screen, keep as a pattern** | The only long-list interaction in the reference. We have no list long enough to need it today; per-app profiles (§2.3) is the one candidate and it is short. Recorded so it is not re-derived later. |
| **Hotkey test** (4.png) | **Need, near-verbatim** | This is the best screen in the set. It converts "did the hook register?" into something the user *observes*. We have the exact failure it protects against: pynput's hook silently not registering. |
| **Try-it-now in a fake Slack** (5.png) | **Need, variant** | Target scope item 4 requires a live "try it now" box. **The Slack chrome is cut** — rendering another company's logo and product frame inside our app is impersonation of a real organisation, and it buys nothing the pattern doesn't. A neutral "Try it here" card with the same inline key-cap instruction does the same job. |
| **"Nice job!" payoff** (6.png) | **Need, variant, with a truthfulness fix** | We can compute the speaking rate honestly (`analytics.words_per_minute`). The comparison baseline is `[analytics].typing_wpm` (default 45.0, `config.py:300`), which is an assumption and **must be labelled an estimate** — CLAUDE.md Phase 6, non-negotiable. Also n=1: one dictation is not a speaking rate. See Q7. **This screen stays light** — the reference's dark treatment here is the editorial choice we have already ruled out. |

### 2.2 Main app (§1, 7–14)

| Reference | Verdict | Reasoning |
|---|---|---|
| **Dictation home** (7.png) | **Need** → our **Home** | Greeting with amber key caps, today's list, three-number rail. All three are things we can populate truthfully today. The photographic promo banner is cut (nothing to promote). |
| **Insights › Your usage** (8.png) | **Need, variant** | Stat cards, the teal arc, the app-share bar and the contribution heatmap all map onto data `queries.py` already returns. Cut: the "Download on mobile" button, the share sticker, "4 AI PROMPTS". The `Top 38%` percentile is cut as unfounded — we have no cohort. |
| **Insights › Your voice, locked** (9.png) | **Cut the feature, steal the pattern** | An LLM-authored "voice profile" is a token cost with no phase behind it and is exactly the kind of metric CLAUDE.md Phase 6 forbids fabricating. **But** the locked-state pattern — show the reward's shape, state the exact remaining cost — is the right answer for our real thresholds (p95 needs runs; WPM needs measured sessions). Applied in `ui-derived-screens.md`, with one change: we do **not** blur fake content, because fake content is fabrication even when unreadable. |
| **Dictionary** (10.png) | **Need** | `DictionaryStore` already has `add_many` / `remove_many` / `list_terms` (`dictionary.py`) and a CLI. It has never had a UI. The chip-grid hero is the right empty state. Cut the "Shared with team" tab. |
| **Snippets** (11.png) | **Need, renamed** → **trigger phrases** | This *is* target scope item 2's "trigger phrases (add/edit/delete, no code changes)" and D8.1's `deterministic`/`macro` action types. The `"trigger"` → `expansion` chip pair is the clearest thing in the reference and should be copied exactly. |
| **Style** (12.png) | **Need, variant** → **per-app profiles** | We already have `flow.profiles` — three styles (casual / formal / verbatim) keyed off `focused_process_name()`. The reference frames the same idea as *writing style per context* rather than *per process name*, which is the better user-facing frame. Their five tabs become our three styles plus an app list. |
| **Transforms** (13.png) | **Defer** | Maps to D8's `mode` / `transform` action types, which are the expensive, model-calling half of D8. Overlaps trigger phrases heavily. Building both now would ship two competing manager UIs. See Q5. |
| **Scratchpad** (14.png) | **Cut** | No phase, no engine support, and it is a note-taking app inside a dictation app. |
| **Notetaker** (nav item) | **Cut** | Never opened in the reference; would mean meeting capture, which is a different product. |
| **Invite your team / Get a free month / Flow on mobile** | **Cut** | No accounts, no service, no mobile app, no referral programme. |
| **Notification bell + count** (title bar) | **Cut for now** | Our only notification-worthy events are errors, and those get a home on Home + Settings' error log. A bell that only ever shows errors is a worse error surface than an error surface. |
| **Account icon** (title bar) | **Cut** | No accounts. |
| **Share sticker** (Insights) | **Cut** | Sharing implies a destination. |

---

## 3. What Flow has that the reference doesn't

| Flow-only | Stays? | Why |
|---|---|---|
| **Agent on/off** (tray + `POST /api/listening`) | **Stays** — promoted | The reference's agent is always on because it is a hosted service. Ours can be paused, and target scope item 3 requires start/stop/quit from both window and tray, in sync. This is a genuine addition to the reference's IA, not a gap in ours. |
| **Per-stage latency waterfall** (p50/p95/p99, stage shares, `.slow` flagging) | **Stays** | D6 requires it; CLAUDE.md Phase 6 places it at hierarchy level 4. The reference has nothing comparable because it never exposes engine internals. Demoted to an Insights tab, not cut. |
| **Health banner** (`queries.health()` → status/headline/detail) | **Stays**, merged into Home's agent status | One agent-state component, not two. |
| **Raw-vs-cleaned transcript diff** (session drawer) | **Stays** | No reference equivalent. Their nearest thing is the ⚑ flag icon. Ours is strictly more useful and is the only place the cleanup layer is auditable. |
| **`skip_min_words` / cleanup-skipped sessions** | **Stays** (in detail view) | CLAUDE.md Phase 2 requires logging the skip rate. |
| **Time range picker** (7d/30d/90d/all) | **Stays on Insights** | Reference offers only month paging on the heatmap. |
| **Voice commands over selection** (`flow.commands`) | **Stays** | No reference equivalent at all. Needs a home in the new IA — currently it has none beyond a second hotkey. See Q4. |
| **"Estimate" labelling on time-saved** | **Stays, mandatory** | CLAUDE.md Phase 6. The reference labels nothing as an estimate, including its 4× typing claim. We do not copy that. |
| **Tray icon + menu** | **Stays** | No reference. Target scope item 3 requires state sync with the window. |
| **Status pill overlay** | **Stays** | No reference. Derived in `ui-derived-screens.md` §1. |
| **`flow-cli.exe` / `python -m flow`** | **Stays, out of the user path** | Dev surface. The Done criteria only require that the *user* never sees a terminal. |
| **Privacy card** (audio never stored, text local, what reaches Groq) | **Stays** → **About** | The reference never explains where anything goes. We should. |
| **Loopback token + origin checks** | **Stays, invisible** | The Done criterion is that nothing in the *user path* mentions localhost, ports or tokens — not that the transport changes. PRODUCTION_BRIEF D2 has already decided to keep the socket. |

---

## 4. Feasibility

Flags as specified: **trivial** / **needs design work** / **needs a dependency
decision** / **not possible in tkinter**.

"Not possible in tkinter" is used strictly: it means no reasonable amount of
PIL compositing gets there, not that it would be tedious. Where PIL
compositing *does* get there, the flag is "needs design work" and the real
cost is noted, because `overlay.py` already proves the technique works at 44px
and equally proves what it costs — every visual becomes an immutable bitmap.

### 4.1 Shell and chrome

| Item | Flag | Detail |
|---|---|---|
| Real desktop window, taskbar entry, icon, remembered geometry | **trivial** | Either host. `packaging/flow.ico` exists. |
| Rounded window corners (§3) | **trivial** | Win11 DWM attribute via ctypes in `platform/windows.py`; already the right home for it. |
| Custom title bar: sidebar toggle + window controls, no OS bar (§3) | **needs design work** | tkinter: `overrideredirect(True)` then hand-reimplement drag, double-click-maximise, Aero Snap, and the Win11 snap-layouts flyout. That is a genuine multi-day tar pit for chrome. Recommend keeping the OS title bar if we stay on tkinter, and accepting the delta from the reference. |
| Floating white panel inset from a warm window background (§3) | **trivial** | Two nested frames. |
| Sidebar, 245px, active item as a filled 12px-radius rect (§3, §4) | **needs design work** in tkinter | No border-radius. Each item becomes a PIL-composited bitmap in two states, or accepts square fills. |
| Collapsible sidebar | **trivial** | |
| 150%/175% DPI scaling | **needs design work** | tkinter needs manual `SetProcessDpiAwareness` plus per-font scaling, and every PIL-composited bitmap must be re-rendered per scale factor — a cache dimension `overlay.py` does not currently have. |

### 4.2 Component vocabulary (§4)

| Item | Flag | Detail |
|---|---|---|
| List row, hairline separated, fixed left meta column | **trivial** | |
| Hover-revealed row action cluster | **trivial** | `<Enter>`/`<Leave>` bindings. |
| Toggle (pill switch, black track when on) | **needs design work** | No native widget. PIL bitmap in two states. ~30 lines, then reusable. |
| Key cap, three sizes, purple while held | **trivial** | `overlay.py` already renders rounded rects with borders and shadows in PIL. |
| Chip, and the `"trigger"` → `expansion` chip pair | **needs design work** | Same PIL-bitmap cost; the pair also needs to wrap at the panel edge, which a bitmap does not do — it needs a small layout pass. |
| Stat card, big numeral + uppercase label | **needs design work** | See letter-spacing below. |
| Tab row with a short label-width underline | **trivial** | |
| **Uppercase letter-spaced meta labels** (`TODAY`, `WORDS PER MINUTE`) | **not possible in tkinter** | Tk has no letter-spacing/tracking property at any level. These labels appear on almost every screen (§4, §5 level 5) and are a large part of why the reference reads as designed. Workaround is rendering each as a PIL bitmap — which means no selection, no theming, and a re-render per DPI scale. This is the single clearest "the toolkit is fighting the design" item in the whole analysis. |
| Teal arc gauge, bar chart, contribution heatmap | **trivial** | PIL or Canvas; `app.js` already computes all three datasets. |
| Photographic hero card with a dark scrim | **needs design work** | Trivial to composite, but we would have to source or generate the photography. Recommend flat dark hero cards instead — the reference's *headline + body + one button* structure is what carries the teaching, not the photo. |
| Blurred locked-state content (§6 strategy 3) | **cut** | `PIL.ImageFilter.GaussianBlur` makes it trivial, but blurring invented content is fabrication. Replaced by a greyed skeleton + exact remaining count. |
| Modal with dimmed backdrop | **trivial** | |
| Serif display face | **needs a dependency decision** | Georgia ships with Windows and is a defensible stand-in. A closer match means bundling a webfont — licence check plus ~200 KB. |
| Smooth scrolling over a long list | **needs design work** in tkinter | `Canvas` + scrollbar scrolls in jumps; `ttk.Treeview` looks like Windows XP and cannot render the row design at all. History is unbounded (90-day retention, `config.py:286`), so this is not hypothetical. |

### 4.3 Screens

| Item | Flag | Detail |
|---|---|---|
| Home | **needs design work** | Data all exists. |
| History list + search + copy/delete | **needs design work** | Search and copy exist; **delete does not** — no `HistoryStore` delete method and no route. D6 requires delete and delete-all. New backend work, small. |
| History detail (drawer) | **trivial** | Port of existing markup. |
| Dictionary CRUD | **trivial** | Store API complete. |
| Insights (usage / speed / performance tabs) | **trivial** | All three datasets already returned by `queries.py`. |
| Settings — hotkey display | **trivial** | `describe()` returns it. |
| Settings — hotkey **rebinding** with conflict detection | **needs design work** | New: a capture mode, a conflict check, and a persisted override. D5 scope. |
| Settings — input device picker | **trivial** | `flow devices` already enumerates via sounddevice; `[audio].device` already accepts it. |
| Settings — launch at login | **trivial** | `platform.set_launch_at_login` / `launches_at_login` already implemented (`platform/windows.py:445`). |
| Settings — masked API key + test connection | **needs a dependency decision** | The field is trivial. Where the key *lives* is not: D5 moves it to `keyring` (Windows Credential Manager), and the task forbids shipping the `.env`-copying build path before that lands. So this field cannot ship ahead of the keyring decision. |
| Settings — per-app profiles | **needs design work** | `flow.profiles` is currently three hardcoded `frozenset`s. Making it user-editable means a persisted override store and a resolution order. |
| Settings — trigger phrases CRUD | **needs design work** | No store, no resolver, no UI. This is D8.1's action model in miniature. Deriving the UI now (as Step 2B does) is fine; shipping it depends on how much of D8 comes with it. See Q5. |
| Onboarding: welcome / permissions / API key / hotkey test / try-it | **needs design work** | All five are new. Permissions is derived in 2B §4. |
| Error log grouped by type | **trivial** | `queries.performance()` already returns `errors` grouped. |
| Live updates without manual refresh | **trivial** | D6 requirement; polling `/api/status` already exists (`app.js:660`). |

### 4.4 The host question

Five items above are the whole argument, and they cluster:

- Letter-spaced meta labels — **not possible in tkinter**
- Custom title bar — **needs design work**, and the work is reimplementing Windows
- Smooth scrolling over unbounded history — **needs design work**, badly
- DPI scaling across PIL-composited bitmaps — **needs design work**, multiplicatively
- Every rounded/filled component — **needs design work**, once each, then immutable

Against that: **PRODUCTION_BRIEF.md D2 has already decided this** — "wrap the
existing dashboard in `pywebview` (OS WebView2 runtime; no Node, no Electron)"
with "report its bundle-size delta before adding it", and "Transport: DECIDED —
keep the loopback socket". The task's constraint ("no new heavyweight GUI
dependency without an approved justification — the build is already 92 MB") and
D2's "report the delta before adding it" are the same requirement stated twice.

I am **not** arguing the decision here, per the task's structure. What Step 2
establishes is that the decision is load-bearing: the five items above are not
polish, they are §4 and §5 of the reference, and §5 is what makes the whole
thing read as a product. Step 3 will carry the numbers — measured pywebview +
WebView2 bundle delta, measured cold-start delta, and a measured
`scripts/baseline.py` run — and will state the tkinter fallback explicitly,
including which parts of the reference we would be choosing not to have.

---

## 5. Open decisions for Step 3

Listed here, not answered.

- **Q1 — Sidebar shape.** Target scope names five destinations (Home, History,
  Dictionary, Settings, About) but item 5 also requires usage / counts /
  p50-p95-by-stage / error log, which has no home among those five. Proposal:
  primary group `Home · History · Dictionary · Insights`; bottom group
  `Settings · About`. Confirm or correct.
- **Q2 — Per-app profiles and trigger phrases: tab or destination?** Target
  scope puts both inside Settings. The reference puts both equivalents (Style,
  Snippets) at the *top level* and demotes Settings — it treats personalisation
  as a feature and configuration as plumbing. I will build to the target scope
  unless told otherwise, but the reference disagrees with it.
- **Q3 — Page sub-lines.** Our dashboard has a sub-line under every page title
  ("Technical health of the voice engine."); the reference has none. Keep, or
  match the reference?
- **Q4 — Voice commands over selection** (`flow.commands`, shipped, working)
  currently has no UI anywhere. Does it get a home in the new IA, or stay a
  hotkey with no surface?
- **Q5 — How much of D8 comes with trigger phrases?** Target scope item 2 asks
  for trigger-phrase CRUD. D8.1 defines four action types; only two
  (`deterministic`, `macro`) need no model call. Ship those two now and defer
  `mode`/`transform` (and the Transforms page) to D8 proper?
- **Q6 — Splash threshold.** Show the splash only when startup exceeds N ms, or
  always, or never? D0 measured ~300 ms warm / 1249 ms first-touch.
- **Q7 — The payoff screen with n=1.** After the first dictation we have one
  speaking-rate sample. Report it with an explicit caveat, wait for a third
  dictation, or state a range?
- **Q8 — History delete.** D6 requires copy/delete/delete-all including R4
  queued audio. `HistoryStore` has no delete method today. Does that land with
  the History screen, or as separate backend work first?

---

## 6. Summary

- **Nothing in the data layer is cut.** All seven routes and all 802 lines of
  `queries.py` survive. The gap is entirely shell and presentation.
- **Five dashboard views become four destinations plus three tabs**, and the
  one view whose content was an apology (`views.settings`) is replaced.
- **Nine reference screens are cut** — every one of them for the same reason:
  they belong to a hosted service with accounts, teams, a mobile app and a
  referral programme, and we are a local app with none of those.
- **Three things we have are better than the reference** and stay: the
  raw-vs-cleaned diff, the honest self-relative WPM comparison, and estimate
  labelling. The reference fabricates a percentile and an unlabelled 4× claim;
  CLAUDE.md forbids us both.
- **Four things must be derived from scratch** because the reference is silent:
  runtime dictation states, error states, Settings contents, and the
  permissions screen. Those are `ui-derived-screens.md`.
