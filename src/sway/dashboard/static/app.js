/*
 * Sway: the desktop UI.
 *
 * DESIGN.md is the reference (§N below points at it): a flat sidebar, one page
 * per destination, tabs under the title for sub-navigation, and the component
 * vocabulary built once at the top of this file and reused everywhere.
 *
 * Two rules that matter more than style here:
 *
 *  - Every string that came from the user, from a transcript, or from Groq is
 *    escaped before it reaches innerHTML (§14). Transcripts are arbitrary text
 *    the app pastes into other applications; treating them as markup would be
 *    a self-inflicted XSS in a window that can also read the user's history.
 *  - The token never leaves this file's fetch calls. It is not put in the
 *    address bar, not logged, and not rendered.
 */

'use strict';

/* ------------------------------------------------------------------ api */

// Taken from the URL once, then scrubbed from the address bar so a screenshot
// or a shoulder-surfer never captures it.
const TOKEN = new URLSearchParams(location.search).get('token') || '';
if (location.search) history.replaceState(null, '', location.pathname + location.hash);

async function request(path, { method = 'GET', params = {}, body = null } = {}) {
  const url = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  const init = { method, headers: { 'X-Sway-Token': TOKEN } };
  if (body !== null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, init);
  } catch (err) {
    // The only way this fires is the engine going away underneath us.
    throw new Error('Sway stopped responding.');
  }
  const text = await response.text();
  let payload = null;
  try { payload = text ? JSON.parse(text) : null; } catch { /* non-JSON error page */ }
  if (!response.ok) {
    throw new Error((payload && payload.error) || `Request failed (${response.status})`);
  }
  return payload;
}

const api = (path, params) => request(path, { params });
const post = (path, body) => request(path, { method: 'POST', body: body || {} });

/* --------------------------------------------------------------- format */

// Unmeasured is never 0 (DESIGN.md §9.1). Inline it reads "n/a";
// stat tiles render "Not yet" (see stat()).
const DASH = 'n/a';
const isNone = (n) => n === null || n === undefined;

const fmt = {
  int: (n) => (isNone(n) ? DASH : Math.round(Number(n)).toLocaleString()),
  /** §4: below 10,000 full digits with a separator; from there 12.4K, 1.2M. */
  num(n) {
    if (isNone(n)) return DASH;
    const v = Number(n);
    const abs = Math.abs(v);
    if (abs < 10000) return Math.round(v).toLocaleString();
    if (abs < 1e6) return `${(v / 1e3).toFixed(abs < 1e5 ? 1 : 0).replace(/\.0$/, '')}K`;
    return `${(v / 1e6).toFixed(1).replace(/\.0$/, '')}M`;
  },
  dec: (n, d = 0) => (isNone(n) ? DASH : Number(n).toFixed(d)),
  ms: (n) => (isNone(n) ? DASH : `${Math.round(n).toLocaleString()} ms`),
  pct: (n, d = 0) => (isNone(n) ? DASH : `${(n * 100).toFixed(d)}%`),
  clock: (epoch) =>
    new Date(epoch * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
  date: (epoch) =>
    new Date(epoch * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }),
  shortDay: (iso) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString([], { day: 'numeric', month: 'short' }),
  longDay: (iso) =>
    new Date(`${iso}T00:00:00`).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' }),
  app: (name) => (name ? name.replace(/\.exe$/i, '') : 'Unknown app'),
  plural: (n, word, many) => `${fmt.int(n)} ${Number(n) === 1 ? word : (many || `${word}s`)}`,
  duration(seconds) {
    if (isNone(seconds)) return DASH;
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  },
  /** Groups history by local calendar day: "Today", "Yesterday", or a date. */
  dayHeading(epoch) {
    const d = new Date(epoch * 1000);
    const today = new Date();
    const yesterday = new Date(); yesterday.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return 'Today';
    if (d.toDateString() === yesterday.toDateString()) return 'Yesterday';
    return d.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  },
};

function esc(value) {
  if (value === null || value === undefined) return '';
  return String(value)
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

/* ----------------------------------------------------------- components */

const icon = (name, cls = '') =>
  `<svg viewBox="0 0 20 20" class="${cls}" draggable="false" aria-hidden="true"><use href="#i-${name}"/></svg>`;

/** Icon-only buttons always carry an aria-label and a tooltip (§7, §12). */
const iconBtn = (name, label, attrs = '') =>
  `<button class="icon-btn" aria-label="${esc(label)}" data-tip="${esc(label)}" ${attrs}>${icon(name)}</button>`;

/**
 * A statistic (§7). `value === null` renders an em dash in --faint, never a
 * zero: an unmeasured value and a measured zero are different claims (§9.1).
 * `estimate` names the assumption behind the number, in a tooltip.
 */
function stat({ label, value, unit, foot, estimate, tip }) {
  const shown = isNone(value) || value === DASH
    ? `<div class="stat-none" data-tip="Not measured yet">Not yet</div>`
    : `<div class="stat-value">${esc(value)}${unit ? `<span class="unit">${esc(unit)}</span>` : ''}</div>`;
  return `
    <div class="stat">
      ${shown}
      <div class="meta-label">${esc(label)}${tip ? `<span data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}">${icon('info')}</span>` : ''}</div>
      ${foot ? `<div class="stat-foot">${esc(foot)}</div>` : ''}
      ${estimate ? `<div class="estimate" data-tip="${esc(estimate)}" tabindex="0">${icon('info')} Estimate</div>` : ''}
    </div>`;
}

/**
 * Not enough data yet (§9.1): the real skeleton at reduced opacity and the
 * exact remaining count. No blur, no invented content.
 */
function locked({ label, need, have, unit = 'dictations' }) {
  const left = Math.max(0, need - have);
  const pct = need > 0 ? Math.min(100, (have / need) * 100) : 0;
  return `
    <div class="stat locked">
      <div class="stat-none">Not yet</div>
      <div class="meta-label">${esc(label)}</div>
      <div class="bar mt-3"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="stat-foot">Unlocks at ${fmt.plural(need, unit.replace(/s$/, ''), unit)} · ${fmt.int(left)} to go</div>
    </div>`;
}

const card = (inner, cls = '') => `<div class="card ${cls}">${inner}</div>`;

/** A card with a heading row: a sans section title and optional tools. */
const titledCard = (title, inner, tools = '', titleCls = 't-section') => card(`
  <div class="card-head"><h3 class="${titleCls}">${title}</h3>${tools}</div>
  ${inner}`);

/** A teaching empty state (§9.1): worked examples, not placeholders. */
function hero({ title, body, chips = [], action }) {
  return `
    <div class="hero">
      <h2>${esc(title)}</h2>
      <p>${esc(body)}</p>
      ${action || chips.length ? `<div class="chips">
        ${action ? `<button class="chip action" data-act="${esc(action.act)}">${action.icon ? icon(action.icon) : ''}${esc(action.label)}</button>` : ''}
        ${chips.join('')}
      </div>` : ''}
    </div>`;
}

/** A filter that matched nothing (§9.1): name the query, offer to clear it. */
const nothingFound = (text, clearAct) => `
  <div class="empty">
    <div>${esc(text)}</div>
    ${clearAct ? `<button class="btn ghost small" data-act="${clearAct}">Clear search</button>` : ''}
  </div>`;

const toggle = (checked, attrs = '') =>
  `<button class="toggle" role="switch" aria-checked="${checked ? 'true' : 'false'}" ${attrs}></button>`;

/** The page's title row, with its single primary action on the right (§5.1). */
const pageHead = (title, actions = '') => `
  <div class="page-head">
    <h1 class="page-title">${title}</h1>
    ${actions}
  </div>`;

/** A tab row: a sliding indigo underline, tools right-aligned (§6.2). */
function tabRow(tabs, active, attr, tools = '') {
  return `
    <div class="tabs" role="tablist">
      ${tabs.map(([id, label, count]) => `
        <button class="tab ${active === id ? 'active' : ''}" role="tab" aria-selected="${active === id}" ${attr}="${id}">
          ${esc(label)}${isNone(count) ? '' : `<span class="tab-count">${fmt.int(count)}</span>`}
        </button>`).join('')}
      ${tools ? `<div class="tab-actions">${tools}</div>` : ''}
      <span class="tab-underline"></span>
    </div>`;
}

/** Search field (§7 Inputs): leading icon, clear button once it has a value. */
const searchField = (id, placeholder, value, cls = 'sized') => `
  <div class="search-wrap ${cls}">
    ${icon('search')}
    <input class="input" id="${id}" data-search placeholder="${esc(placeholder)}" value="${esc(value)}" autocomplete="off" spellcheck="false">
    ${value ? iconBtn('x', 'Clear search', `class="icon-btn search-clear" data-act="clear-search" data-for="${id}"`) : ''}
  </div>`;

/** A section heading over a container: meta label 12px above it (§7 Lists). */
const sectionTitle = (label, tools = '') =>
  `<div class="section-title"><span class="meta-label">${esc(label)}</span>${tools}</div>`;

/** Key caps (§7). `hot` = "this is your hotkey" (amber). */
function caps(spec, cls = 'hot') {
  const parts = String(spec || '').split('+').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return '';
  return `<span class="cap-row">${parts.map((p) => `<span class="cap ${cls}">${esc(prettyKey(p))}</span>`).join('')}</span>`;
}

/**
 * Key caps from a binding's already-formatted label (e.g. "Ctrl + Alt + P",
 * "Middle Click") -- what every activation and transform binding reaches
 * the dashboard as (see sway.hotkey.Binding.pretty). Splits on " + " since
 * the server has already done the parsing and naming.
 */
function capsFromLabel(label, cls = 'hot') {
  const parts = String(label || '').split(' + ').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return '';
  return `<span class="cap-row">${parts.map((p) => `<span class="cap ${cls}">${esc(p)}</span>`).join('<span class="plus">+</span>')}</span>`;
}

/** The first (primary) effective binding for a target, or null -- {value,
 * label, shape}. Used wherever the UI says "your hotkey" outside Settings. */
function primaryBinding(target) {
  const a = state.settings && state.settings.activation && state.settings.activation[target];
  const bindings = a && a.bindings;
  return bindings && bindings.length ? bindings[0] : null;
}

/** pynput names are not user-facing text. */
function prettyKey(name) {
  const map = {
    ctrl_r: 'Right Ctrl', ctrl_l: 'Left Ctrl', ctrl: 'Ctrl',
    shift_r: 'Right Shift', shift_l: 'Left Shift', shift: 'Shift',
    alt_r: 'Right Alt', alt_l: 'Left Alt', alt: 'Alt', alt_gr: 'AltGr',
    cmd: 'Win', cmd_r: 'Win', cmd_l: 'Win', caps_lock: 'Caps Lock',
    f13: 'F13', space: 'Space',
  };
  const key = String(name || '').toLowerCase();
  return map[key] || name;
}

const levelMeter = (level) =>
  `<span class="level" data-level="${Number(level) || 0}"><i></i><i></i><i></i></span>`;

/** Icon for an app category (sway.profiles.AppCategory). */
const CATEGORY_ICON = { chat: 'chat', mail: 'mail', terminal: 'terminal', code: 'code', other: 'doc' };
const CATEGORY_LABEL = {
  chat: 'Chat apps', mail: 'Mail', terminal: 'Terminals', code: 'Code editors', other: 'Everything else',
};

/* ------------------------------------------------------------- app state */

const state = {
  view: 'home',
  home: null,
  activity: null,
  insights: null,
  performance: null,
  voice: null,
  settings: null,
  dictionary: null,
  devices: null,
  diagnostics: null,
  query: '',
  range: '30d',
  insightsTab: 'usage',
  settingsTab: 'general',
  snippetQuery: '',
  dictScope: 'all',
  lastDeleted: null,
};

const el = (id) => document.getElementById(id);

/** Per-viewer conveniences only (§ browser storage): never required state. */
const prefs = {
  get(key, fallback) { try { return localStorage.getItem(`sway.${key}`) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(`sway.${key}`, value); } catch { /* ignored */ } },
};

/* ================================================================ chrome */

/* ---- Toasts (§11.2) ----------------------------------------------------
   One at a time; a new one replaces the current. 4s, or 6s with an action.
   The timer pauses on hover and focus; Ctrl+Z triggers the action.        */

const toastState = { timer: null, action: null, remaining: 0, started: 0 };

function toast(message, action) {
  const node = el('toast');
  const show = () => {
    node.innerHTML = `<span>${esc(message)}</span>`
      + (action ? `<span class="sep">·</span><button id="toast-action">${esc(action.label)}</button>` : '');
    toastState.action = action || null;
    node.classList.add('show');
    toastState.remaining = action ? 6000 : 4000;
    startToastTimer();
    if (action) {
      el('toast-action').onclick = runToastAction;
    }
  };
  clearTimeout(toastState.timer);
  if (node.classList.contains('show')) {
    node.classList.remove('show');
    setTimeout(show, 140);
  } else {
    show();
  }
}

function startToastTimer() {
  clearTimeout(toastState.timer);
  toastState.started = Date.now();
  toastState.timer = setTimeout(hideToast, toastState.remaining);
}

function hideToast() {
  clearTimeout(toastState.timer);
  el('toast').classList.remove('show');
  toastState.action = null;
}

async function runToastAction() {
  const action = toastState.action;
  hideToast();
  if (action) await action.run();
}

(function bindToast() {
  const node = el('toast');
  const pause = () => {
    clearTimeout(toastState.timer);
    toastState.remaining = Math.max(1200, toastState.remaining - (Date.now() - toastState.started));
  };
  const resume = () => { if (node.classList.contains('show')) startToastTimer(); };
  node.addEventListener('mouseenter', pause);
  node.addEventListener('mouseleave', resume);
  node.addEventListener('focusin', pause);
  node.addEventListener('focusout', resume);
}());

/* ---- Tooltips (§11.3) --------------------------------------------------
   600ms before the first; 0ms when moving to another within 300ms of the
   last closing. Keyboard focus shows immediately; Esc hides. Above by
   default, flipped to stay in the window.                                  */

const tip = { timer: null, target: null, lastHidden: 0 };

function tipText(target) {
  if (target.hasAttribute('data-tip-trunc')) {
    // Only for text that is actually cut off.
    if (target.scrollWidth <= target.clientWidth) return '';
    return target.textContent.trim();
  }
  if (target.closest('.app.collapsed') === null && target.classList.contains('nav-item')) return '';
  return target.getAttribute('data-tip') || '';
}

function showTip(target) {
  const text = tipText(target);
  if (!text) return;
  const node = el('tooltip');
  const keys = target.getAttribute('data-tip-keys');
  node.innerHTML = `<span>${esc(text)}</span>${keys ? capsFromLabel(keys, '') : ''}`;
  const r = target.getBoundingClientRect();
  node.style.left = '0px';
  node.style.top = '0px';
  node.classList.add('show');
  const t = node.getBoundingClientRect();
  const side = target.closest('.sidebar') && target.closest('.app.collapsed');
  let left; let top;
  if (side) {
    left = r.right + 8;
    top = r.top + (r.height - t.height) / 2;
  } else {
    left = r.left + (r.width - t.width) / 2;
    top = r.top - t.height - 8;
    if (top < 8) top = r.bottom + 8;
  }
  left = Math.max(8, Math.min(left, window.innerWidth - t.width - 8));
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
  tip.target = target;
}

function hideTip() {
  clearTimeout(tip.timer);
  if (tip.target) tip.lastHidden = Date.now();
  tip.target = null;
  el('tooltip').classList.remove('show');
}

document.addEventListener('mouseover', (event) => {
  const target = event.target.closest('[data-tip], [data-tip-trunc]');
  if (target === tip.target) return;
  hideTip();
  if (!target) return;
  const warm = Date.now() - tip.lastHidden < 300;
  tip.timer = setTimeout(() => showTip(target), warm ? 0 : 600);
});
document.addEventListener('focusin', (event) => {
  const target = event.target.closest && event.target.closest('[data-tip]');
  hideTip();
  if (target && target.matches(':focus-visible')) showTip(target);
});
document.addEventListener('focusout', hideTip);
document.addEventListener('mousedown', hideTip, true);
document.addEventListener('scroll', hideTip, true);

/* ---- Busy state (§7 Buttons) -------------------------------------------
   Nothing for the first 150ms; then a spinner and the present participle;
   on success the past tense with a check for 1.2s. Keeps its width.       */

async function busy(button, { doing, done }, work) {
  if (!button) return work();
  const original = button.innerHTML;
  const width = button.offsetWidth;
  let shown = false;
  const timer = setTimeout(() => {
    shown = true;
    button.style.minWidth = `${width}px`;
    button.classList.add('busy');
    button.innerHTML = `<span class="spinner"></span>${esc(doing)}`;
  }, 150);
  try {
    const result = await work();
    clearTimeout(timer);
    if (button.isConnected && done) {
      button.classList.remove('busy');
      button.style.minWidth = `${width}px`;
      button.innerHTML = `${icon('check')}${esc(done)}`;
      setTimeout(() => { if (button.isConnected) { button.innerHTML = original; button.style.minWidth = ''; } }, 1200);
    } else if (button.isConnected && shown) {
      button.classList.remove('busy');
      button.innerHTML = original;
      button.style.minWidth = '';
    }
    return result;
  } catch (err) {
    clearTimeout(timer);
    if (button.isConnected) {
      button.classList.remove('busy');
      button.innerHTML = original;
      button.style.minWidth = '';
    }
    throw err;
  }
}

/** Copy feedback (§11.1): the icon swaps to a check for 1.2s, no toast. */
function copiedFeedback(button) {
  if (!button || !button.classList.contains('icon-btn')) { toast('Copied'); return; }
  const original = button.innerHTML;
  button.innerHTML = icon('check');
  button.classList.add('done');
  button.setAttribute('data-tip', 'Copied');
  if (tip.target === button) showTip(button);
  setTimeout(() => {
    if (!button.isConnected) return;
    button.innerHTML = original;
    button.classList.remove('done');
    button.setAttribute('data-tip', button.getAttribute('aria-label') || 'Copy');
  }, 1200);
}

/* ---- Form validation (§7 Inputs) ---------------------------------------
   The error replaces the helper text in the same slot, so nothing jumps.   */

function fieldError(inputId, message) {
  const input = el(inputId);
  const help = el(`${inputId}-help`);
  if (input) input.classList.toggle('invalid', !!message);
  if (!help) return;
  if (help.dataset.help === undefined) help.dataset.help = help.innerHTML;
  if (message) {
    help.className = 'helper error';
    help.innerHTML = `${icon('alert')}<span>${esc(message)}</span>`;
    help.setAttribute('role', 'alert');
  } else {
    help.className = 'helper';
    help.innerHTML = help.dataset.help;
    help.removeAttribute('role');
  }
}

/** A field: label above, helper below in a slot errors can replace (§7). */
const field = (id, label, control, helper = '') => `
  <div class="field">
    <label class="field-label" for="${id}">${label}</label>
    ${control}
    <div class="helper" id="${id}-help">${helper}</div>
  </div>`;

/* ---- Modal (§6.4) ------------------------------------------------------
   Focus goes to the first field (or the safe button in a confirm), is
   trapped, and returns to whatever opened it. Esc and scrim click close.  */

let modalReturn = null;

function openModal({ title, body, actions, wide = false, focus }) {
  if (!el('modal-scrim').classList.contains('open')) modalReturn = document.activeElement;
  el('modal-title').textContent = title || '';
  el('modal-body').innerHTML = body;
  el('modal-foot').innerHTML = actions;
  document.querySelector('.modal').classList.toggle('wide', wide);
  el('modal-scrim').classList.add('open');
  enhanceSelects(el('modal-body'));
  const target = focus ? el('modal-scrim').querySelector(focus)
    : el('modal-body').querySelector('input:not([type=checkbox]), textarea, select');
  if (target) target.focus();
}

function closeModal() {
  if (!el('modal-scrim').classList.contains('open')) return;
  closePopover({ restoreFocus: false });
  el('modal-scrim').classList.remove('open');
  if (modalReturn && modalReturn.isConnected) modalReturn.focus();
  modalReturn = null;
}

let drawerReturn = null;

function openDrawerShell() {
  drawerReturn = document.activeElement;
  el('drawer').classList.add('open');
  el('scrim').classList.add('open');
  el('drawer-close').focus();
}

function closeDrawer() {
  if (!el('drawer').classList.contains('open')) return;
  el('drawer').classList.remove('open');
  el('scrim').classList.remove('open');
  if (drawerReturn && drawerReturn.isConnected) drawerReturn.focus();
  drawerReturn = null;
}

function trapFocus(event, container) {
  const focusable = [...container.querySelectorAll(
    'button:not([disabled]), input:not([disabled]), textarea, select:not(.native-hidden), [tabindex]:not([tabindex="-1"]), summary',
  )].filter((n) => n.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

/* ============================================================== the views */

const views = {};

/* ---- Home -------------------------------------------------------------- */

views.home = {
  title: 'Home',
  async load() {
    state.home = await api('/api/home');
    if (!state.settings) state.settings = await api('/api/settings');
  },
  render() {
    const d = state.home;
    const binding = primaryBinding('dictation');
    const bindingCaps = binding ? capsFromLabel(binding.label) : '';

    const title = `Get back into the flow with ${bindingCaps || '<span class="muted">your hotkey</span>'}`;

    const recent = d.recent.length
      ? groupedHistory(d.recent)
      : `<div class="list"><div class="empty"><div class="inline wrap center">Hold ${bindingCaps || 'your hotkey'} and say something. It will appear here.</div></div></div>`;

    const all = d.all_time || {};
    const line = (value, label, tipText) => `
      <div class="stat-line" ${tipText ? `data-tip="${esc(tipText)}"` : ''}>
        <span class="t-number ${isNone(value) ? 'none' : ''}">${isNone(value) ? 'Not yet' : esc(value)}</span>
        <span class="label">${esc(label)}</span>
      </div>`;

    const rail = `
      <div class="card flush">
        <div class="card-section stack-3">
          ${line(fmt.num(d.today.words), Number(d.today.words) === 1 ? 'word today' : 'words today')}
          ${line(fmt.num(all.words), 'total words')}
          ${line(isNone(all.avg_wpm) ? null : fmt.dec(all.avg_wpm, 0), 'wpm',
            isNone(all.avg_wpm) ? 'Not measured yet' : `Average across ${fmt.plural(all.measured_sessions, 'measured dictation')}`)}
          ${line(fmt.int(d.streaks.current), 'day streak', `Longest: ${fmt.plural(d.streaks.longest, 'day')}`)}
        </div>
        <div class="card-rule flush"></div>
        <div class="card-section stack-2">
          <div class="t-card">Your voice profile</div>
          <div class="small muted">How fast you speak, when, in which languages, and which fillers Sway takes out.</div>
          <div class="mt-2"><button class="btn small" data-act="go-voice-profile">Open profile ${icon('arrow')}</button></div>
        </div>
      </div>`;

    return `
      ${pageHead(title)}
      <div class="page-body">${agentCard(d)}</div>
      <div class="grid with-rail mt-8">
        <div>
          <div class="list-head">
            <span class="meta-label">Recent</span>
            ${iconBtn('search', 'Search history', 'data-act="go-history" data-tip-keys="Ctrl + K"')}
          </div>
          ${recent}
        </div>
        ${rail}
      </div>`;
  },
};

/**
 * Agent status: the merged health banner and on/off control. Red is used
 * only where the user must act (§3.3); a transient condition gets no colour.
 */
function agentCard(d) {
  const status = d.status || {};
  const health = d.health || {};
  const listening = status.listening;
  const keyMissing = state.settings && state.settings.key && !state.settings.key.configured;
  // The engine's richest error copy (engine._describe_failure): the only
  // place that says *what* broke and whether the user can do something.
  const failure = status.last_error;

  let dot = 'ok';
  let headline = 'Listening for your hotkey';
  let detail = health.detail || '';
  let action = '';

  if (keyMissing) {
    dot = 'act';
    headline = 'Paused: no API key';
    detail = 'Sway cannot transcribe until a Groq key is set.';
    action = '<button class="btn primary" data-act="go-key">Add API key</button>';
  } else if (listening === false) {
    dot = 'warn';
    headline = 'Paused';
    detail = 'Your hotkey is disarmed. Nothing is being recorded.';
  } else if (failure) {
    // Actionability decides the colour, not severity.
    dot = failure.actionable ? 'act' : 'warn';
    headline = failure.headline || 'That dictation failed';
    detail = failure.detail || detail;
    if (failure.kind === 'auth' || failure.kind === 'no_key') {
      action = '<button class="btn primary" data-act="go-key">Fix API key</button>';
    } else if (failure.kind === 'audio') {
      action = '<button class="btn" data-act="go-voice">Check microphone</button>';
    }
  } else if (health.status === 'problem') {
    dot = 'act';
    headline = health.headline || 'Recent dictations are failing';
  } else if (health.status === 'degraded') {
    dot = 'warn';
    headline = health.headline || 'Degraded';
  }

  const toggleControl = status.can_toggle
    ? `<div class="inline">
         <span class="small muted">${listening === false ? 'Off' : 'On'}</span>
         ${toggle(listening !== false, 'data-act="toggle-agent" aria-label="Listen for the hotkey"')}
       </div>`
    : '';

  return card(`
    <div class="status-card">
      <span class="dot ${dot}"></span>
      <div class="grow">
        <div class="status-head">${esc(headline)}</div>
        ${detail ? `<div class="small muted">${esc(detail)}</div>` : ''}
      </div>
      ${action}
      ${toggleControl}
    </div>`);
}

function historyRow(entry) {
  const preview = entry.preview || '';
  return `
    <div class="row clickable" data-id="${esc(entry.id)}" tabindex="0" role="button" aria-label="Open dictation from ${esc(fmt.clock(entry.created_at))}">
      <span class="when">${esc(fmt.clock(entry.created_at))}</span>
      <div class="body">
        <div class="text selectable" data-tip-trunc>${esc(preview) || '<span class="muted">empty</span>'}</div>
      </div>
      <span class="value app-col ellipsis" data-tip-trunc>${esc(fmt.app(entry.app))}</span>
      <div class="actions">
        ${iconBtn('copy', 'Copy', `data-act="copy" data-id="${esc(entry.id)}"`)}
        ${iconBtn('trash', 'Delete', `data-act="delete" data-id="${esc(entry.id)}"`)}
      </div>
    </div>`;
}

/** History rows in one container per day, a date heading above each (§7). */
function groupedHistory(entries) {
  const groups = [];
  for (const entry of entries) {
    const heading = fmt.dayHeading(entry.created_at);
    const last = groups[groups.length - 1];
    if (last && last.heading === heading) last.rows.push(entry);
    else groups.push({ heading, rows: [entry] });
  }
  return groups.map((g) => `
    <div class="list-group">
      <span class="meta-label">${esc(g.heading)}</span>
      <div class="list">${g.rows.map(historyRow).join('')}</div>
    </div>`).join('');
}

/* ---- History ----------------------------------------------------------- */

views.history = {
  title: 'History',
  async load() {
    state.activity = await api('/api/activity', { q: state.query, limit: 200 });
  },
  render() {
    const rows = state.activity.sessions;
    const body = rows.length
      ? groupedHistory(rows)
      : state.query
        ? `<div class="list">${nothingFound(`No dictations match “${state.query}”.`, 'clear-history-search')}</div>`
        : hero({
            title: 'Everything you dictate lands here.',
            body: 'Each dictation is stored on this machine with its text, its timing, and the app it went to. Nothing is uploaded.',
          });

    const hasAny = rows.length || state.query;
    return `
      ${pageHead('History', hasAny ? '<button class="btn danger" data-act="delete-all">Delete all…</button>' : '')}
      ${hasAny ? `
        <div class="tabs">
          ${searchField('search', 'Search your dictations', state.query, 'sized small')}
          <div class="tab-actions"><span class="small muted">${fmt.plural(state.activity.total, 'dictation')} stored</span></div>
        </div>` : ''}
      <div class="page-body">${body}</div>`;
  },
  bind(root) {
    const search = root.querySelector('#search');
    if (!search) return;
    let timer;
    search.addEventListener('input', (event) => {
      clearTimeout(timer);
      const value = event.target.value;
      // Debounced: a LIKE scan per keystroke on a long history makes typing
      // feel laggy for no benefit.
      timer = setTimeout(async () => {
        state.query = value;
        await refresh({ keepFocus: '#search' });
      }, 180);
    });
  },
};

/* ---- Dictionary -------------------------------------------------------- */

views.dictionary = {
  title: 'Dictionary',
  async load() { state.dictionary = await api('/api/dictionary'); },
  render() {
    const entries = state.dictionary.entries || [];
    if (!state.dictionary.available) {
      return `${pageHead('Dictionary')}
        <div class="page-body"><div class="banner info">${icon('info')} The dictionary is not available in this window.</div></div>`;
    }

    if (!entries.length) {
      return `${pageHead('Dictionary', '<button class="btn" data-act="import-vocab">Import…</button>')}
        <div class="page-body">${hero({
          title: 'Sway spells the way you do.',
          body: 'Add names, jargon, and product words Sway keeps getting wrong. They are sent '
              + 'to the transcriber as a hint, so it knows to expect them. Sway also picks up '
              + 'names you dictate often, and shows you what it learned.',
          chips: ['Ashwini', 'Groq', 'Kubernetes', 'Mayfair'].map(
            (w) => `<span class="chip on-dark">${esc(w)}</span>`),
          action: { act: 'add-term', label: 'Add a word', icon: 'plus' },
        })}</div>`;
    }

    const scope = state.dictScope || 'all';
    const counts = {
      all: entries.length,
      manual: entries.filter((e) => e.source === 'manual').length,
      learned: entries.filter((e) => e.source === 'learned').length,
      shared: entries.filter((e) => e.source === 'shared').length,
    };
    // Only offer a filter that would actually show something.
    const tabs = [['all', 'All'], ['manual', 'Added by me'],
                  ['learned', 'Learned'], ['shared', 'Shared']]
      .filter(([id]) => id === 'all' || counts[id] > 0)
      .map(([id, label]) => [id, label, counts[id]]);

    const shown = scope === 'all' ? entries : entries.filter((e) => e.source === scope);
    const pinned = entries.filter((e) => e.pinned).length;

    return `
      ${pageHead('Dictionary', `
        <button class="btn" data-act="import-vocab">Import…</button>
        <button class="btn" data-act="export-vocab">Export</button>
        <button class="btn primary" data-act="add-term">${icon('plus')} Add word</button>`)}
      <p class="page-sub">
        ${fmt.plural(entries.length, 'term')} biasing transcription${pinned ? `, ${fmt.int(pinned)} pinned` : ''}.
        Pin the ones that matter most. The transcriber only has room for a couple of hundred characters.
      </p>
      ${tabs.length > 1 ? tabRow(tabs, scope, 'data-scope') : ''}
      <div class="page-body">${shown.length ? termChips(shown) : nothingFound('Nothing here yet.')}</div>`;
  },
  bind(root) {
    root.querySelectorAll('[data-scope]').forEach((tab) => {
      tab.addEventListener('click', async () => {
        state.dictScope = tab.dataset.scope;
        await refresh({ keepScroll: true });
      });
    });
  },
};

const SOURCE_LABEL = { manual: 'Added by you', learned: 'Learned from your dictation', shared: 'From a shared pack' };

function termChips(entries) {
  return `<div class="chip-list">
    ${entries.map((e) => {
      const used = e.hits ? ` · said ${fmt.plural(e.hits, 'time')}` : '';
      const tipText = `${SOURCE_LABEL[e.source] || e.source}${used} · ${e.pinned ? 'Click to unpin' : 'Click to pin'}`;
      return `<span class="chip interactive ${e.pinned ? 'selected' : ''}">
        <span class="txt" role="button" tabindex="0" data-act="pin-term" data-tip="${esc(tipText)}"
              data-term="${esc(e.term)}" data-pinned="${e.pinned ? '1' : ''}"
              aria-pressed="${e.pinned ? 'true' : 'false'}">${e.pinned ? icon('pin', 'pin') : ''}${esc(e.term)}</span>
        <button class="chip-x" data-act="remove-term" data-term="${esc(e.term)}" aria-label="Remove ${esc(e.term)}" data-tip="Remove">${icon('x')}</button>
      </span>`;
    }).join('')}
  </div>`;
}

/* ---- Snippets ---------------------------------------------------------- */

views.snippets = {
  title: 'Snippets',
  async load() {
    // Snippets live in settings.json alongside everything else the user edits.
    state.settings = await api('/api/settings');
  },
  render() {
    const all = (state.settings && state.settings.triggers) || [];
    const query = (state.snippetQuery || '').trim().toLowerCase();
    const shown = query
      ? all.filter((t) => [t.label, t.phrase, t.payload, t.name].some(
          (f) => (f || '').toLowerCase().includes(query)))
      : all;

    if (!all.length) {
      return `
        ${pageHead('Snippets')}
        <div class="page-body">${hero({
          title: "The stuff you shouldn't have to say twice.",
          body: 'Save the text you retype (an intro email, a signature, an address) '
              + 'and say its phrase to insert it. Matched on this machine before anything '
              + 'is sent anywhere, so it costs nothing and adds no delay.',
          chips: [
            '<span class="chip on-dark">“intro email” → Hey, I’d love to find some time…</span>',
            '<span class="chip on-dark">“my email” → you@example.com</span>',
          ],
          action: { act: 'add-trigger', label: 'Add a snippet', icon: 'plus' },
        })}</div>`;
    }

    return `
      ${pageHead('Snippets', `<button class="btn primary" data-act="add-trigger">${icon('plus')} Add snippet</button>`)}
      <p class="page-sub">Say the phrase, or “use my …”, while dictating to insert one.</p>
      <div class="tabs">
        ${searchField('snippet-search', 'Search snippets', state.snippetQuery || '', 'sized small')}
        <div class="tab-actions"><span class="small muted">${fmt.plural(all.length, 'snippet')}</span></div>
      </div>
      <div class="page-body">
        ${shown.length ? snippetRows(shown) : `<div class="list">${nothingFound(`No snippets match “${state.snippetQuery.trim()}”.`, 'clear-snippet-search')}</div>`}
      </div>`;
  },
  bind(root) {
    const search = root.querySelector('#snippet-search');
    if (!search) return;
    search.addEventListener('input', async (event) => {
      state.snippetQuery = event.target.value;
      await refresh({ keepFocus: '#snippet-search', keepScroll: true });
    });
  },
};

function snippetRows(triggers) {
  return `<div class="list">${triggers.map((t) => `
    <div class="row">
      ${toggle(t.enabled, `data-act="toggle-trigger" data-id="${esc(t.id)}" aria-label="Snippet enabled"`)}
      <div class="body stack-1">
        ${t.name ? `<div class="text medium">${esc(t.name)}</div>` : ''}
        <div class="pair">
          <span class="chip"><span class="txt">“${esc(t.phrase)}”</span></span>
          ${icon('arrow', 'arrow')}
          <span class="chip"><span class="txt" data-tip-trunc>${esc(t.type === 'date' ? "Today's date" : t.payload)}</span></span>
        </div>
        ${t.conflicts && t.conflicts.length
          ? `<div class="helper error">${icon('alert')}<span>Overlaps “${esc(t.conflicts[0])}”. The longer phrase wins.</span></div>` : ''}
      </div>
      <div class="actions">
        ${t.type === 'date' ? '' : iconBtn('copy', 'Copy text', `data-act="copy-snippet" data-id="${esc(t.id)}"`)}
        ${iconBtn('edit', 'Edit', `data-act="edit-trigger" data-id="${esc(t.id)}"`)}
        ${iconBtn('trash', 'Delete', `data-act="delete-trigger" data-id="${esc(t.id)}"`)}
      </div>
    </div>`).join('')}</div>`;
}

/* ---- Transforms -----------------------------------------------------------

   A transform is a saved instruction. It reaches the text two ways, and the
   card says which are set up, because the two cost different things: a
   shortcut runs one model call over a selection, a phrase folds its
   instruction into the cleanup call that was happening anyway.            */

// Mirrors sway.transforms.MAX_INSTRUCTION_CHARS. A textarea maxlength
// silently truncates a pasted value that exceeds it, so this has to match
// the server's ceiling exactly.
const TRANSFORM_INSTRUCTION_MAX_CHARS = 8000;

views.transforms = {
  title: 'Transforms',
  async load() { state.settings = await api('/api/settings'); },
  render() {
    const all = (state.settings && state.settings.transforms) || [];

    if (!all.length) {
      return `
        ${pageHead('Transforms')}
        <div class="page-body">${hero({
          title: 'Say it once. Shape it after.',
          body: 'A transform is a saved instruction that rewrites text: polish it, '
              + 'make it concise, turn it into an email. Run one over any selection with '
              + 'its shortcut, or say its phrase while dictating.',
          chips: ['Polish', 'Make concise', 'Rewrite professionally'].map(
            (w) => `<span class="chip on-dark">${esc(w)}</span>`),
          action: { act: 'add-transform', label: 'Create a transform', icon: 'plus' },
        })}</div>`;
    }

    return `
      ${pageHead('Transforms', `
        <button class="btn ghost" data-act="reset-transforms">${icon('reset')} Reset to defaults</button>
        <button class="btn primary" data-act="add-transform">${icon('plus')} New transform</button>`)}
      <p class="page-sub">Transforms work anywhere you write. A transform only costs a request when you run it.</p>
      <div class="grid auto page-body">
        ${all.map(transformCard).join('')}
        ${transformCreateTile()}
      </div>`;
  },
};

function transformCreateTile() {
  return `<button class="transform-tile" data-act="add-transform">
    <span class="transform-tile-plus">${icon('plus')}</span>
    <span class="t-card">Create your own</span>
    <span class="small muted">Write your own prompt</span>
  </button>`;
}

function transformCard(t) {
  const shortcutCaps = (t.shortcut_labels || []).map((label) => capsFromLabel(label)).join('');
  const preview = t.instruction || t.description;
  return `<div class="card transform-card" data-act="edit-transform" data-id="${esc(t.id)}" tabindex="0" role="button" aria-label="Edit ${esc(t.name)}">
    <div class="transform-card-caps">
      ${shortcutCaps || (t.phrase ? '' : '<span class="small muted">No shortcut yet</span>')}
      ${t.phrase ? `<span class="chip"><span class="txt">“${esc(t.phrase)}”</span></span>` : ''}
    </div>
    <div class="t-card transform-card-name">${esc(t.name)}</div>
    <div class="small muted transform-card-preview">${esc(preview)}</div>
    ${!t.runnable
      ? '<div class="small muted">No shortcut or phrase yet, so it can’t run.</div>' : ''}
    ${t.conflicts && t.conflicts.length
      ? `<div class="helper error">${icon('alert')}<span>Same shortcut as “${esc(t.conflicts[0])}”. Only one of them will run.</span></div>`
      : ''}
    <div class="transform-card-actions">
      ${iconBtn('edit', 'Edit', `data-act="edit-transform" data-id="${esc(t.id)}"`)}
      ${iconBtn('trash', 'Delete', `data-act="delete-transform" data-id="${esc(t.id)}"`)}
    </div>
  </div>`;
}

/* ---- The transform editor sheet -----------------------------------------

   A two-pane wide sheet (§6.4): the left side is what the transform *is*
   (its name, how you run it, an example), the right side is what it *does*
   (shortcuts, phrase, the prompt). Autosaved on every change (debounced
   600ms for text, immediate for a shortcut or a checkbox), so there is no
   Save step to forget; status sits next to the title (§11.1).              */

let sheetState = null;
let sheetSaveTimer = null;
let sheetSavePending = false;
let sheetDirty = false;

function blankTransform() {
  return {
    id: '', name: '', description: '', instruction: '', phrase: '',
    shortcuts: [], shortcut_labels: [], builtin: false,
    example_input: '', example_output: '', example_diff: [],
    allows_markdown: false, may_shorten: false, may_lengthen: false,
  };
}

function openTransformSheet(existing) {
  sheetState = existing ? { ...existing } : blankTransform();
  renderTransformSheet();
}

function closeTransformSheet() {
  clearTimeout(sheetSaveTimer);
  sheetState = null;
  closeModal();
}

function transformExampleHtml(t) {
  if (t.example_diff && t.example_diff.length) {
    return t.example_diff.map((part) => {
      if (part.op === 'delete') return `<s class="diff-del">${esc(part.text)}</s>`;
      if (part.op === 'insert') return `<mark class="diff-ins">${esc(part.text)}</mark>`;
      return esc(part.text);
    }).join('');
  }
  if (t.example_input && t.example_output) {
    return `<s class="diff-del">${esc(t.example_input)}</s> <mark class="diff-ins">${esc(t.example_output)}</mark>`;
  }
  return '<span class="muted">No example yet. Run it once from its shortcut or phrase, and what it produced will show up here.</span>';
}

function sheetStatusHtml(text, done) {
  return `${done ? icon('check') : ''}${esc(text)}`;
}

function renderTransformSheet() {
  const t = sheetState;
  const isNew = !t.id;
  const shortcutRows = (t.shortcut_labels || []).map((label, i) => {
    const advisory = (t.shortcut_advisories || [])[i];
    return `
      <div class="stack-1">
        <div class="inline between">
          ${capsFromLabel(label)}
          <span class="inline-1">
            ${iconBtn('edit', 'Change shortcut', `data-act="edit-transform-shortcut" data-index="${i}"`)}
            ${iconBtn('trash', 'Remove shortcut', `data-act="remove-transform-shortcut" data-index="${i}"`)}
          </span>
        </div>
        ${advisory ? `<div class="helper">${esc(advisory)}</div>` : ''}
      </div>`;
  }).join('');

  const hasUse = (t.shortcut_labels || []).length || t.phrase;
  const useLine = hasUse
    ? `<div class="use-line">${(t.shortcut_labels || []).slice(0, 1).map((l) => capsFromLabel(l)).join('')}
         ${t.phrase ? `<span class="chip"><span class="txt">“${esc(t.phrase)}”</span></span>` : ''}
         <span>to use</span></div>`
    : '<div class="use-line none">No shortcut or phrase set yet</div>';

  el('modal-title').textContent = '';
  el('modal-body').innerHTML = `
    <div class="sheet-grid">
      <div class="sheet-col">
        <input class="sheet-name-input" id="x-name" value="${esc(t.name)}"
          placeholder="Boss mode" maxlength="80" aria-label="Transform name">
        ${useLine}
        ${field('x-desc', 'Description <span class="muted">(optional)</span>',
          `<input class="input" id="x-desc" value="${esc(t.description)}" placeholder="Turns rambling into a crisp update" maxlength="200">`)}
        <div class="stack-3">
          <div class="field-label">Example of transformed text</div>
          <div class="transform-example">${transformExampleHtml(t)}</div>
        </div>
      </div>
      <div class="sheet-col side">
        <div class="inline between">
          <span class="sheet-status" id="x-autosave">Autosave on</span>
          ${t.builtin ? `<button class="btn ghost small" data-act="reset-transforms">${icon('reset')} Reset</button>` : ''}
        </div>
        <div class="well raised stack-3">
          <div>
            <div class="field-label">Keyboard shortcuts</div>
            <div class="helper">Runs it over whatever you have selected.</div>
          </div>
          ${shortcutRows || '<div class="small muted">None yet.</div>'}
          <div><button class="btn small" data-act="add-transform-shortcut">${icon('plus')} Add shortcut</button></div>
        </div>
        <div class="well raised">
          ${field('x-phrase', 'Spoken phrase <span class="muted">(optional)</span>',
            `<input class="input" id="x-phrase" value="${esc(t.phrase)}" placeholder="make that concise" maxlength="80">`,
            'Say it at the end of a dictation. Folded into the cleanup request, so it costs no extra call.')}
        </div>
        <div class="field">
          <div class="inline between baseline">
            <label class="field-label" for="x-instruction">Prompt</label>
            <span class="char-count ${t.instruction.length >= TRANSFORM_INSTRUCTION_MAX_CHARS * 0.8 ? '' : 'hidden'}" id="x-charcount">${fmt.int(t.instruction.length)} / ${fmt.int(TRANSFORM_INSTRUCTION_MAX_CHARS)}</span>
          </div>
          <textarea class="input sheet-prompt" id="x-instruction" maxlength="${TRANSFORM_INSTRUCTION_MAX_CHARS}" rows="10"
            placeholder="Rewrite the text into a highly effective AI prompt while preserving the intent.">${esc(t.instruction)}</textarea>
          <div class="helper" id="x-instruction-help">Written to the model as-is. Say what to preserve, not just what to change.</div>
        </div>
        <details>
          <summary>${icon('chevron')} What it may do to the text</summary>
          <div class="stack-2 mt-3">
            <div class="helper">Tick what this prompt is <em>supposed</em> to do, so Sway knows what's worth flagging. Run from its shortcut, you always see the result and decide whether to use it. Said as a phrase mid-dictation there's no review step: an answer that looks off is dropped and your raw words are pasted instead, so these are what keep a correct one from being dropped.</div>
            <label class="check-row"><input type="checkbox" id="x-md"${t.allows_markdown ? ' checked' : ''}>
              <span>Produces a list or headings <span class="muted">(bullets, numbered points)</span></span></label>
            <label class="check-row"><input type="checkbox" id="x-short"${t.may_shorten ? ' checked' : ''}>
              <span>Makes the text shorter <span class="muted">(summarize, condense)</span></span></label>
            <label class="check-row"><input type="checkbox" id="x-long"${t.may_lengthen ? ' checked' : ''}>
              <span>Makes the text longer <span class="muted">(adds a greeting, a sign-off)</span></span></label>
          </div>
        </details>
        <div id="x-error" class="helper error" role="alert"></div>
      </div>
    </div>`;
  el('modal-foot').innerHTML = `
    <span class="small muted">Changes save as you type.</span>
    <div class="spacer"></div>
    <button class="btn primary" data-act="close-transform-sheet">Done</button>`;
  if (!el('modal-scrim').classList.contains('open')) modalReturn = document.activeElement;
  document.querySelector('.modal').classList.add('wide');
  el('modal-scrim').classList.add('open');
  el('x-error').style.display = 'none';

  for (const [id, key] of [
    ['x-name', 'name'], ['x-desc', 'description'], ['x-phrase', 'phrase'],
    ['x-instruction', 'instruction'],
  ]) {
    const node = el(id);
    if (node) node.oninput = () => {
      sheetState[key] = node.value;
      if (id === 'x-instruction' && el('x-charcount')) {
        const count = el('x-charcount');
        count.textContent = `${fmt.int(node.value.length)} / ${fmt.int(TRANSFORM_INSTRUCTION_MAX_CHARS)}`;
        // §7: only once within 80% of the limit.
        count.classList.toggle('hidden', node.value.length < TRANSFORM_INSTRUCTION_MAX_CHARS * 0.8);
      }
      scheduleSheetSave();
    };
  }
  for (const [id, key] of [['x-md', 'allows_markdown'], ['x-short', 'may_shorten'], ['x-long', 'may_lengthen']]) {
    const node = el(id);
    if (node) node.onchange = () => { sheetState[key] = node.checked; saveSheetNow(); };
  }
  if (isNew) el('x-name').focus();
  else el('modal-scrim').querySelector('[data-act="close-transform-sheet"]').focus();
}

function scheduleSheetSave() {
  el('x-autosave').innerHTML = sheetStatusHtml('Saving…');
  sheetDirty = true;
  clearTimeout(sheetSaveTimer);
  sheetSaveTimer = setTimeout(saveSheetNow, 600);
}

async function saveSheetNow() {
  if (!sheetState) return;
  if (sheetSavePending) { clearTimeout(sheetSaveTimer); sheetSaveTimer = setTimeout(saveSheetNow, 300); return; }
  clearTimeout(sheetSaveTimer);
  sheetDirty = false;
  const t = sheetState;
  const error = el('x-error');
  if (!t.name.trim() || !t.instruction.trim()) {
    if (el('x-autosave')) el('x-autosave').innerHTML = sheetStatusHtml(t.name.trim() ? 'Add a prompt to save' : 'Add a name to save');
    return;
  }
  sheetSavePending = true;
  try {
    const settings = await post('/api/transforms/save', {
      id: t.id || undefined,
      name: t.name,
      description: t.description,
      instruction: t.instruction,
      shortcuts: t.shortcuts,
      phrase: t.phrase,
      allows_markdown: t.allows_markdown,
      may_shorten: t.may_shorten,
      may_lengthen: t.may_lengthen,
    });
    state.settings = settings;
    const saved = (settings.transforms || []).find((x) =>
      t.id ? x.id === t.id : x.name === t.name && x.instruction === t.instruction);
    if (saved && sheetState) { sheetState = { ...saved, name: sheetState.name, instruction: sheetState.instruction, description: sheetState.description, phrase: sheetState.phrase }; }
    if (el('x-autosave')) el('x-autosave').innerHTML = sheetStatusHtml('Saved', true);
    if (error) { error.textContent = ''; error.style.display = 'none'; }
    setBadge('transforms-count', countTransformConflicts());
  } catch (err) {
    if (error) { error.innerHTML = `${icon('alert')}<span>Couldn't save. ${esc(err.message)}</span>`; error.style.display = ''; }
    if (el('x-autosave')) el('x-autosave').innerHTML = sheetStatusHtml('Not saved');
  } finally {
    sheetSavePending = false;
  }
}

/* ---- Style ---------------------------------------------------------------

   Per-app cleanup styles (Phase 3). Moved here from Settings › Apps so
   there is one place to read the defaults and one place to override them.
   The groups and their styles come from sway.profiles via /api/settings;
   the before/after texts are labelled examples, not dictations.           */

const STYLE_INFO = {
  casual: {
    name: 'Casual',
    blurb: 'Removes fillers and false starts, fixes punctuation, and otherwise keeps your exact wording and tone.',
    said: 'um so we should uh move the call to friday, sorry, thursday',
    wrote: 'So we should move the call to Thursday.',
  },
  formal: {
    name: 'Formal',
    blurb: 'Everything Casual does, plus a light professional polish: contractions expanded, slang avoided.',
    said: "hey um can't make it tomorrow, gonna send the deck tonight",
    wrote: 'Hi, I cannot make it tomorrow. I am going to send the deck tonight.',
  },
  verbatim: {
    name: 'No cleanup',
    blurb: 'No cleanup request at all. Commands and code reach the cursor exactly as transcribed.',
    said: 'git status',
    wrote: 'git status',
  },
};
const styleLabel = (s) => (STYLE_INFO[s] ? STYLE_INFO[s].name : s);

views.style = {
  title: 'Style',
  async load() { state.settings = await api('/api/settings'); },
  render() {
    const s = state.settings || {};
    const groups = s.style_groups || [];
    const rules = s.app_rules || [];
    const styles = s.cleanup_styles || [];

    const cards = Object.keys(STYLE_INFO).map((key) => {
      const info = STYLE_INFO[key];
      const mine = groups.filter((g) => g.style === key);
      const names = [...new Set(mine.flatMap((g) => g.apps.map(fmt.app)))].sort();
      const shown = names.slice(0, 7);
      const apps = shown.map((a) => `<span class="badge">${esc(a)}</span>`)
        .concat(names.length > shown.length
          ? [`<span class="badge" tabindex="0" data-tip="${esc(names.slice(shown.length).join(', '))}">+${names.length - shown.length} more</span>`] : [])
        .concat(mine.some((g) => !g.apps.length) ? ['<span class="badge">Every other app</span>'] : []);
      return card(`
        <div class="style-card">
          <div class="stack-1">
            <div class="inline-3"><span class="feature-icon">${icon(key === 'verbatim' ? 'terminal' : key === 'formal' ? 'mail' : 'chat')}</span><h3 class="t-card">${esc(info.name)}</h3></div>
            <p class="small muted">${esc(info.blurb)}</p>
          </div>
          <div class="example">
            <span class="meta-label">Example · you say</span>
            <div class="muted">${esc(info.said)}</div>
            <span class="meta-label mt-3">Sway writes</span>
            <div>${esc(info.wrote)}</div>
          </div>
          <div class="stack-2">
            <span class="meta-label">Used by default in</span>
            <div class="apps">${apps.join('') || '<span class="small muted">No apps by default</span>'}</div>
          </div>
        </div>`);
    }).join('');

    const ruleRows = rules.length
      ? `<div class="list">${rules.map((r) => `
          <div class="row">
            ${icon(CATEGORY_ICON.other)}
            <div class="body"><div class="text">${esc(fmt.app(r.process))}</div><div class="sub">${esc(r.process)}</div></div>
            <select class="input small" data-act="set-rule" data-process="${esc(r.process)}" aria-label="Style for ${esc(fmt.app(r.process))}">
              ${styles.map((st) => `<option value="${esc(st)}" ${r.style === st ? 'selected' : ''}>${esc(styleLabel(st))}</option>`).join('')}
            </select>
            <div class="actions">${iconBtn('trash', 'Remove rule', `data-act="delete-rule" data-process="${esc(r.process)}"`)}</div>
          </div>`).join('')}</div>`
      : `<div class="banner info">${icon('info')}<span class="grow">No rules yet. Add one only where the default isn't right for an app.</span></div>`;

    return `
      ${pageHead('Style', `<button class="btn primary" data-act="add-rule">${icon('plus')} Add app rule</button>`)}
      <p class="page-sub">Sway picks how much to clean up from the app you're dictating into. Casual in chat, formal in mail, untouched in terminals and editors.</p>
      <div class="page-body stack-12">
        <section>
          ${sectionTitle('Built-in styles')}
          <div class="grid cols-3">${cards}</div>
        </section>
        <section>
          ${sectionTitle('Your app rules', rules.length ? `<span class="small muted">${fmt.plural(rules.length, 'rule')}</span>` : '')}
          ${ruleRows}
          <p class="small muted mt-3">Anything without a rule uses the defaults above. Apps not listed use Casual.</p>
        </section>
      </div>`;
  },
};

/* ---- Insights ---------------------------------------------------------- */

views.insights = {
  title: 'Insights',
  tabsFor: () => [['usage', 'Your usage'], ['speed', 'Your speed'], ['performance', 'Performance']],
  async load() {
    const [insights, performance] = await Promise.all([
      api('/api/overview', { range: state.range }),
      api('/api/performance', { range: state.range }),
    ]);
    state.insights = insights;
    state.performance = performance;
  },
  render() {
    const range = `<select class="input small" id="range" aria-label="Time range">
      ${[['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['90d', 'Last 90 days'], ['all', 'All time']].map(
        ([v, l]) => `<option value="${v}" ${state.range === v ? 'selected' : ''}>${l}</option>`).join('')}
    </select>`;
    return `
      ${pageHead('Insights')}
      ${tabRow(this.tabsFor(), state.insightsTab, 'data-tab', range)}
      <div class="page-body">${this.panel()}</div>`;
  },
  panel() {
    if (state.insightsTab === 'usage') return usagePanel();
    if (state.insightsTab === 'speed') return speedPanel();
    return performancePanel();
  },
  bind(root) {
    root.querySelectorAll('[data-tab]').forEach((tab) => {
      tab.addEventListener('click', async () => {
        state.insightsTab = tab.dataset.tab;
        syncHash();
        await refresh({ keepScroll: true, quiet: true });
      });
    });
    const range = root.querySelector('#range');
    if (range) range.addEventListener('change', async (e) => { state.range = e.target.value; await refresh({ keepScroll: true }); });
  },
};

const RANGE_LABEL = { '7d': 'the last 7 days', '30d': 'the last 30 days', '90d': 'the last 90 days', all: 'all time' };

function usagePanel() {
  const d = state.insights;
  if (!d.has_data) {
    return hero({
      title: 'Your numbers appear once you have dictated.',
      body: 'Words, speaking rate, which apps you use Sway in, and how consistent you have been. All measured from your own dictations, never estimated unless labelled.',
    });
  }
  const w = d.window;
  const appCount = d.apps.length;

  return `
    <div class="stack">
      <div class="grid cols-3">
        ${card(stat({ label: 'Words dictated', value: fmt.num(w.words), foot: `In ${RANGE_LABEL[state.range]}` }))}
        ${card(stat({ label: 'Dictations', value: fmt.num(w.sessions), foot: `${fmt.plural(d.streaks.active_days, 'active day')} all time` }))}
        ${card(stat({
          label: 'Time saved', value: fmt.duration(w.time_saved_s),
          estimate: `Assumes you type at ${d.typing_wpm} wpm. Change it with [analytics].typing_wpm.`,
        }))}
      </div>
      <div class="grid cols-2">
        ${titledCard('Where you use Sway', appBars(d.apps),
          `<span class="meta-label">${fmt.plural(appCount, 'app')}</span>`)}
        ${titledCard(`${fmt.int(d.streaks.current)}-day streak`, heatmap(d.heatmap || d.daily),
          `<span class="meta-label">Longest · ${fmt.plural(d.streaks.longest, 'day')}</span>`)}
      </div>
      ${titledCard('Words per day', vbars(d.daily.map((b) => ({ day: b.day, value: b.words })), {
        unit: 'words',
        tipFor: (b) => `${fmt.longDay(b.day)} · ${fmt.plural(b.value, 'word')}`,
      }))}
    </div>`;
}

function speedPanel() {
  const d = state.insights;
  const measured = d.daily.filter((b) => b.avg_wpm !== null);
  const all = d.all_time;

  if (!measured.length) {
    return `<div class="grid cols-3">
      ${card(locked({ label: 'Words per minute', need: 3, have: all.measured_sessions || 0, unit: 'measured dictations' }))}
      ${card(stat({ label: 'Words all time', value: fmt.num(all.words) }))}
      ${card(stat({ label: 'Dictations all time', value: fmt.num(all.sessions) }))}
    </div>
    <p class="small muted mt-4">
      Speaking rate needs the measured speech duration of a dictation, which is recorded from this version onward.
    </p>`;
  }

  const recent = measured[measured.length - 1];
  const best = measured.reduce((a, b) => (b.avg_wpm > a.avg_wpm ? b : a));
  const delta = all.avg_wpm ? (recent.avg_wpm - all.avg_wpm) / all.avg_wpm : null;

  return `
    <div class="stack">
      <div class="grid cols-3">
        ${card(stat({
          label: 'Most recent day', value: fmt.dec(recent.avg_wpm, 0), unit: 'wpm',
          // Compared against the user's own history only. There is no
          // reliable external cohort to rank them against, so we do not
          // invent a percentile.
          foot: delta === null ? `${fmt.longDay(recent.day)} · no baseline yet`
            : `${fmt.longDay(recent.day)} · ${Math.abs(delta * 100).toFixed(0)}% ${delta >= 0 ? 'faster' : 'slower'} than your average`,
        }))}
        ${card(stat({ label: 'Best day', value: fmt.dec(best.avg_wpm, 0), unit: 'wpm', foot: fmt.longDay(best.day) }))}
        ${card(stat({ label: 'All-time average', value: fmt.dec(all.avg_wpm, 0), unit: 'wpm', foot: `Across ${fmt.plural(all.measured_sessions, 'measured dictation')}` }))}
      </div>
      ${titledCard('Speaking rate by day', vbars(
        d.daily.map((b) => ({ day: b.day, value: b.avg_wpm === null ? null : Math.round(b.avg_wpm) })),
        {
          unit: 'wpm',
          average: all.avg_wpm,
          tipFor: (b) => (b.value === null ? `${fmt.longDay(b.day)} · not measured` : `${fmt.longDay(b.day)} · ${b.value} wpm`),
        },
      ), '<span class="meta-label">Dashed line · all-time average</span>')}
    </div>`;
}

function performancePanel() {
  const p = state.performance;
  if (!p.total) return `<div class="list">${nothingFound(`No dictations recorded in ${RANGE_LABEL[state.range]}.`)}</div>`;

  const l = p.latency;
  const target = p.target_ms || 1000;

  // Zero errors is printed as zero (§9.1): it is a fact, not an absence.
  const errors = p.errors.length
    ? `<div class="legend-rows">${p.errors.map((e) => `
        <div class="legend-row">
          <span class="dot act"></span>
          <span class="name">${esc(errorLabel(e.error))}</span>
          <span class="val">${fmt.int(e.count)}</span>
        </div>`).join('')}</div>`
    : stat({ label: 'Failures', value: '0', foot: `Across ${fmt.plural(p.total, 'dictation')} in ${RANGE_LABEL[state.range]}.` });

  // No zero-state card for guardrails: most windows never trip one, and a
  // page of empty cards is noise.
  const guardrails = p.guardrails.length
    ? titledCard('Guardrails', `
        <p class="small muted">Rewrites (transforms and voice commands) that a guardrail flagged before you saw them.</p>
        <div class="legend-rows mt-3">${p.guardrails.map((g) => `
          <div class="legend-row">
            <span class="name">${esc(guardrailLabel(g.reason))}</span>
            <span class="val">${fmt.int(g.count)}</span>
          </div>`).join('')}</div>`)
    : '';

  return `
    <div class="stack">
      <div class="grid cols-3">
        ${card(l.p50 === null
          ? locked({ label: 'Typical latency (p50)', need: 5, have: l.count || 0 })
          : stat({
            label: 'Typical latency (p50)', value: fmt.int(l.p50), unit: 'ms',
            foot: l.p50 <= target ? `Within the ${fmt.int(target)} ms target` : `Over the ${fmt.int(target)} ms target`,
            tip: 'Hotkey release to text pasted, median.',
          }))}
        ${card(l.count >= 20
          ? stat({ label: 'Slow latency (p95)', value: fmt.int(l.p95), unit: 'ms', foot: '1 in 20 dictations is slower than this' })
          : locked({ label: 'Slow latency (p95)', need: 20, have: l.count || 0 }))}
        ${card(stat({ label: 'Dictations', value: fmt.num(p.total), foot: p.failed ? `${fmt.int(p.failed)} failed` : 'All succeeded' }))}
      </div>
      <div class="grid cols-2">
        ${titledCard('Where the time goes', latencyBreakdown(p.stages, target),
          `<span class="meta-label">Median per stage</span>`)}
        ${titledCard('Failures', errors)}
      </div>
      ${guardrails}
      ${understandingPanel(p.understanding)}
    </div>`;
}

// Counted from what actually happened on real dictations -- corrections
// detected, transforms fired, snippets substituted. Nothing here is derived
// from an assumption, so nothing here is labelled an estimate.
function understandingPanel(u) {
  if (!u || !u.dictations) return '';
  return `
    <div class="grid cols-3">
      ${card(stat({
        label: 'Corrected mid-sentence',
        value: fmt.int(u.corrections),
        foot: `${fmt.pct(u.correction_rate)} of ${fmt.plural(u.dictations, 'dictation')}`,
      }))}
      ${card(stat({
        label: 'Resolved with no model call',
        value: isNone(u.no_model_call_rate) ? null : fmt.pct(u.no_model_call_rate),
        foot: `${fmt.int(u.no_model_call)} needed nothing from the network`,
      }))}
      ${card(stat({
        label: 'Snippets and transforms',
        value: fmt.int(u.snippets + u.transforms),
        foot: `${fmt.plural(u.snippets, 'snippet')}, ${fmt.plural(u.transforms, 'transform')}`,
      }))}
    </div>`;
}

/** Engine error codes are not user-facing text. */
function errorLabel(code) {
  const raw = String(code || '');
  if (raw.startsWith('rate_limited:')) {
    const which = raw.slice('rate_limited:'.length);
    return which && which !== 'unknown' ? `Rate limited: ${which.replace(/_/g, ' ')}` : 'Rate limited';
  }
  const map = {
    TimeoutError: 'Timed out',
    GroqError: 'Groq request failed',
    AudioError: 'Microphone unavailable',
    ClipboardError: 'Clipboard unavailable',
    PasteBlockedError: 'Could not paste into that window',
    ConnectError: 'Could not reach Groq',
  };
  return map[raw] || raw.replace(/_/g, ' ');
}

// Mirrors the family names sway.cleanup.guardrail_family collapses reason
// tokens into ("lost_entity:200" -> "lost_entity").
function guardrailLabel(family) {
  const map = {
    empty: 'Model returned nothing',
    shrank: 'Much shorter than selected',
    grew: 'Much longer than selected',
    markdown: 'Added list or heading formatting',
    assistant_voice: 'Replied instead of rewriting',
    answered_question: 'Answered instead of rewriting',
    language_changed: 'Dropped a script from the text',
    lost_entity: 'Dropped a number, date, or link',
  };
  return map[family] || String(family || '').replace(/_/g, ' ');
}

/* ================================================================ charts */
/* Teal only (§7 Charts). Every bar, cell and point carries a tooltip with
   the exact value and its date; axes and gridlines use --separator.       */

/** Round a maximum up to a step of 1, 2 or 5 × 10^k, for gridlines. */
function niceScale(max, steps = 3) {
  if (!max || max <= 0) return { top: steps, step: 1 };
  const rough = max / steps;
  const mag = 10 ** Math.floor(Math.log10(rough));
  const norm = rough / mag;
  const nice = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  const step = nice * mag;
  return { top: step * steps, step };
}

/** Indices for ~`count` evenly spaced x labels, always first and last. */
function labelIndices(length, count = 5) {
  if (length <= count) return [...Array(length).keys()];
  const out = new Set([0, length - 1]);
  for (let i = 1; i < count - 1; i += 1) out.add(Math.round((i * (length - 1)) / (count - 1)));
  return [...out].sort((a, b) => a - b);
}

/**
 * Vertical bars per day with a y scale, gridlines and date labels. Values
 * of null are unmeasured and drawn as an empty baseline, not as zero-height
 * data; today's bar is marked in the brand colour.
 */
function vbars(points, { tipFor, average = null } = {}) {
  if (!points.length) return nothingFound('Nothing in this window.');
  const values = points.map((p) => p.value).filter((v) => v !== null);
  const max = Math.max(...values, average || 0, 1);
  const { top, step } = niceScale(max);
  const todayIso = new Date().toLocaleDateString('en-CA');
  const ticks = [];
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(v);

  const cols = points.map((p) => {
    const empty = p.value === null || p.value === 0;
    const h = empty ? 0 : (p.value / top) * 100;
    return `<div class="col ${empty ? 'zero' : ''} ${p.day === todayIso ? 'today' : ''}" data-tip="${esc(tipFor(p))}">
      <i style="height:${h.toFixed(2)}%"></i></div>`;
  }).join('');

  const idx = labelIndices(points.length);
  const xLabels = idx.map((i, n) => {
    const left = ((i + 0.5) / points.length) * 100;
    const cls = n === 0 && left < 6 ? 'first' : n === idx.length - 1 && left > 94 ? 'last' : '';
    const pos = cls === 'first' ? 0 : cls === 'last' ? 100 : left;
    return `<span class="${cls}" style="left:${pos.toFixed(2)}%">${esc(fmt.shortDay(points[i].day))}</span>`;
  }).join('');

  return `
    <div class="vbars" role="img" aria-label="Bar chart, one bar per day">
      <div class="y-axis">${ticks.map((v) => `<span style="bottom:${((v / top) * 100).toFixed(2)}%">${esc(fmt.num(v))}</span>`).join('')}</div>
      <div class="plot">
        ${ticks.map((v, i) => `<div class="gridline ${i === 0 ? 'base' : ''}" style="bottom:${((v / top) * 100).toFixed(2)}%"></div>`).join('')}
        ${average ? `<div class="avgline" style="bottom:${((average / top) * 100).toFixed(2)}%"><span>${fmt.dec(average, 0)} avg</span></div>` : ''}
        <div class="cols">${cols}</div>
      </div>
      <div class="x-axis">${xLabels}</div>
    </div>`;
}

/**
 * Where you use Sway: horizontal bars with the app's category icon, its
 * share printed in the bar, and its measured words beside it.
 */
function appBars(apps) {
  if (!apps.length) return nothingFound('No application recorded yet.');
  const top = apps.slice(0, 6);
  const rest = apps.slice(6);
  const rows = top.map((a) => ({ ...a, label: fmt.app(a.app) }));
  if (rest.length) {
    rows.push({
      label: `${fmt.plural(rest.length, 'other app')}`,
      category: 'other',
      share: rest.reduce((s, a) => s + a.share, 0),
      words: rest.reduce((s, a) => s + a.words, 0),
      sessions: rest.reduce((s, a) => s + a.sessions, 0),
    });
  }
  const maxShare = Math.max(...rows.map((r) => r.share), 0.0001);
  return `<div class="hbars">${rows.map((r) => {
    const pct = r.share * 100;
    const width = Math.max(0, (r.share / maxShare) * 55);
    const tone = r.share >= 0.3 ? '' : r.share >= 0.1 ? 'mid' : 'low';
    return `
      <div class="hbar" data-tip="${esc(`${r.label} · ${fmt.plural(r.sessions, 'dictation')} · ${fmt.plural(r.words, 'word')}`)}">
        ${icon(CATEGORY_ICON[r.category] || 'doc')}
        <div class="hbar-main">
          <div class="hbar-track">
            <div class="hbar-fill ${tone}" style="width:${width.toFixed(1)}%">${pct < 1 && pct > 0 ? '<1' : pct.toFixed(0)}%</div>
            <span class="hbar-label">${esc(r.label)} <span class="muted">· ${esc(fmt.num(r.words))} words</span></span>
          </div>
        </div>
      </div>`;
  }).join('')}</div>
  <p class="small muted mt-4">Share of dictations. Icons follow the app groups on the Style page.</p>`;
}

/** Consistency heatmap: weekday rows, month labels, a Less→More legend. */
function heatmap(buckets) {
  if (!buckets.length) return nothingFound('Nothing yet.');
  const max = Math.max(...buckets.map((b) => b.words || 0), 1);
  const first = new Date(`${buckets[0].day}T00:00:00`);
  const pad = first.getDay(); // Sunday-first rows
  const todayIso = new Date().toLocaleDateString('en-CA');
  const cells = Array(pad).fill('<i class="pad"></i>').concat(buckets.map((b) => {
    const value = b.words || 0;
    const level = value === 0 ? 0 : Math.min(4, Math.ceil((value / max) * 4));
    return `<i data-l="${level}" class="${b.day === todayIso ? 'today' : ''}" data-tip="${esc(`${fmt.longDay(b.day)} · ${fmt.plural(value, 'word')}`)}"></i>`;
  }));

  // A month label over the first week column that contains its 1st (or the
  // first column, for the month the window starts in).
  const months = [];
  let lastMonth = -1;
  buckets.forEach((b, i) => {
    const d = new Date(`${b.day}T00:00:00`);
    if (d.getMonth() !== lastMonth) {
      const col = Math.floor((i + pad) / 7);
      if (!months.length || col - months[months.length - 1].col >= 3) {
        months.push({ col, label: d.toLocaleDateString([], { month: 'short' }) });
      }
      lastMonth = d.getMonth();
    }
  });
  const active = buckets.filter((b) => b.words > 0).length;
  // Laid out from the right, so a narrow card clips the oldest weeks, never
  // this week; month labels are anchored from the right to match.
  const totalCols = Math.ceil((buckets.length + pad) / 7);
  // Labels are left-anchored at their column, so a right offset must leave
  // room for the label itself.
  months.forEach((m) => { m.col = Math.min(m.col, totalCols - 2); });

  return `
    <div class="heatmap">
      <div class="months">${months.map((m) => `<span style="right:${(totalCols - m.col) * 18 - 4}px">${esc(m.label)}</span>`).join('')}</div>
      <div class="days"><span></span><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span></div>
      <div class="heat">${cells.join('')}</div>
    </div>
    <div class="inline between mt-4">
      <span class="small muted">${fmt.plural(active, 'active day')} in the last ${fmt.int(Math.round(buckets.length / 7))} weeks</span>
      <span class="heat-legend"><span>Less</span><i></i><i></i><i></i><i></i><i></i><span>More</span></span>
    </div>`;
}

/**
 * Where the time goes: the stage medians as one stacked bar against the
 * latency target, then a legend row per stage with median, p95 and share.
 * Amber marks only the largest stage, and only when the medians add up to
 * more than the target (§7 Charts).
 */
function latencyBreakdown(all, target) {
  // Only stages that run one after another after the key is released add
  // up to the felt latency. Sub-spans and the hold time are listed below
  // the chart, never summed into it.
  const order = (s) => (s.stage === 'dispatch' ? -1 : 0);
  const stages = all.filter((s) => s.part === 'pipeline' && s.median_ms > 0).sort((a, b) => order(a) - order(b));
  const rest = all.filter((s) => s.part !== 'pipeline');
  if (!stages.length) return nothingFound('No stage timings recorded.');
  const sum = stages.reduce((s, x) => s + x.median_ms, 0);
  const scale = Math.max(sum, target) * 1.08;
  const over = sum > target;
  const largest = stages.reduce((a, b) => (b.median_ms > a.median_ms ? b : a));
  const shade = (i) => (i % 3 === 1 ? 'alt2' : i % 3 === 2 ? 'alt3' : '');
  const markerLeft = (target / scale) * 100;
  const PART_NOTE = {
    within: 'part of a stage above, not added again',
    hold: 'how long you held the key: your speaking time, not waiting',
    other: 'recorded, not part of the total',
  };

  return `
    <div class="chart-wrap">
      <div class="stacked">${stages.map((s, i) => {
        const slow = over && s === largest;
        return `<i class="${slow ? 'slow' : shade(i)}" style="width:${((s.median_ms / scale) * 100).toFixed(2)}%" data-tip="${esc(`${s.stage} · ${fmt.ms(s.median_ms)} median`)}"></i>`;
      }).join('')}</div>
      <div class="budget-marker" style="left:${markerLeft.toFixed(2)}%" data-tip="${esc(`${fmt.int(target)} ms target`)}"></div>
    </div>
    <div class="budget-scale">
      <span class="${markerLeft > 80 ? 'last' : ''}" style="left:${markerLeft.toFixed(2)}%">${fmt.int(target)} ms target</span>
    </div>
    <div class="legend-rows mt-4">
      ${stages.map((s, i) => {
        const slow = over && s === largest;
        return `
        <div class="legend-row">
          <span class="swatch ${slow ? 'slow' : shade(i)}"></span>
          <span class="name">${esc(s.stage)}</span>
          <span class="sub" data-tip="1 in 20 runs is slower than this">p95 ${fmt.ms(s.p95_ms)}</span>
          <span class="val">${fmt.ms(s.median_ms)}</span>
        </div>`;
      }).join('')}
    </div>
    <p class="small muted mt-3">
      ${over
        ? `Stage medians add up to ${fmt.ms(sum)}, over the target. ${esc(largest.stage)} is the largest share.`
        : `Stage medians add up to ${fmt.ms(sum)}, within the target.`}
      A median of a sum isn't a sum of medians, so this can differ from the p50 above.
    </p>
    ${rest.length ? `
      <div class="card-rule"></div>
      <span class="meta-label">Also recorded</span>
      <div class="legend-rows mt-2">${rest.map((s) => `
        <div class="legend-row">
          <span class="name">${esc(s.stage)} <span class="small muted">· ${esc(PART_NOTE[s.part] || PART_NOTE.other)}</span></span>
          <span class="val">${fmt.ms(s.median_ms)}</span>
        </div>`).join('')}</div>` : ''}`;
}

/* ---- Voice profile -------------------------------------------------------

   How this user speaks, measured from their own dictations (all time). No
   unlock threshold: each figure shows as soon as it has data, and says how
   many dictations it covers; anything unmeasured is an em dash.            */

views.voice = {
  title: 'Voice profile',
  async load() { state.voice = await api('/api/voice'); },
  render() {
    const v = state.voice;
    if (!v.dictations) {
      return `${pageHead('Voice profile')}
        <div class="page-body">${hero({
          title: 'This fills in as you talk.',
          body: 'How fast you speak, how long you usually talk for, when you dictate, which languages you use, and which filler words Sway takes out. All measured from your own dictations on this machine.',
        })}</div>`;
    }
    const u = v.understanding || {};
    const f = v.fillers;

    const hourMax = Math.max(...v.hours, 1);
    const hourLabel = (h) => new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: 'numeric' });
    const peak = v.hours.indexOf(Math.max(...v.hours));
    const hours = `
      <div class="hours" role="img" aria-label="Dictations by hour of day">
        ${v.hours.map((n, h) => `<i class="${n ? '' : 'zero'}" style="height:${((n / hourMax) * 100).toFixed(1)}%"
            data-tip="${esc(`${hourLabel(h)}–${hourLabel((h + 1) % 24)} · ${fmt.plural(n, 'dictation')}`)}"></i>`).join('')}
      </div>
      <div class="hours-axis">${[0, 6, 12, 18].map((h) => `<span style="left:${((h / 24) * 100).toFixed(2)}%">${hourLabel(h)}</span>`).join('')}</div>
      <p class="small muted mt-4">You dictate most around ${esc(hourLabel(peak))}.</p>`;

    const langMax = Math.max(...v.languages.map((l) => l.share), 0.0001);
    const langs = `<div class="stack-1">${v.languages.slice(0, 8).map((l) => `
      <div class="share-row" data-tip="${esc(`${l.label} · ${fmt.plural(l.dictations, 'dictation')}`)}">
        <span class="ellipsis">${esc(l.label)}</span>
        <span class="track"><i style="width:${((l.share / langMax) * 100).toFixed(1)}%"></i></span>
        <span class="val">${fmt.pct(l.share)}</span>
      </div>`).join('')}</div>
      ${v.languages.length > 8 ? `<p class="small muted mt-3">And ${fmt.plural(v.languages.length - 8, 'more language')}.</p>` : ''}`;

    const fillers = f.top.length
      ? `<div class="chip-list">${f.top.map((x) => `<span class="chip">“${esc(x.filler)}”<span class="muted">${fmt.int(x.count)}</span></span>`).join('')}</div>`
      : `<p class="small muted">No filler words removed${f.counted ? ` in ${fmt.plural(f.counted, 'dictation')}` : ''}.</p>`;

    return `
      ${pageHead('Voice profile')}
      <p class="page-sub">Measured from ${fmt.plural(v.dictations, 'dictation')} on this machine, all time.</p>
      <div class="page-body stack">
        <div class="grid cols-3">
          ${card(stat({
            label: 'Speaking rate', value: isNone(v.wpm.avg) ? null : fmt.dec(v.wpm.avg, 0), unit: 'wpm',
            foot: v.wpm.measured ? `Average of ${fmt.plural(v.wpm.measured, 'measured dictation')}` : 'Needs measured speech time',
          }))}
          ${card(stat({
            label: 'Typical dictation', value: isNone(v.length.median_words) ? null : fmt.dec(v.length.median_words, 0), unit: 'words',
            foot: isNone(v.length.median_seconds) ? 'Speech time not measured' : `About ${fmt.duration(v.length.median_seconds)} of speech (median)`,
          }))}
          ${card(stat({
            label: 'Fillers removed', value: f.counted ? fmt.int(f.removed) : null,
            foot: isNone(f.per_100_words) ? 'Needs a language Sway has a filler list for'
              : `${fmt.dec(f.per_100_words, 1)} per 100 words you said`,
            tip: 'Filler words in what you said that did not make it into the pasted text.',
          }))}
        </div>
        <div class="grid cols-2">
          ${titledCard('When you dictate', hours, '<span class="meta-label">By hour</span>')}
          ${titledCard('Languages you speak', langs, `<span class="meta-label">${fmt.plural(v.languages.filter((l) => l.code).length, 'language')}</span>`)}
        </div>
        <div class="grid cols-2">
          ${titledCard('Fillers Sway takes out', fillers)}
          ${titledCard('Self-corrections', u.dictations
            ? stat({
              label: 'Corrected mid-sentence', value: fmt.int(u.corrections),
              foot: `${fmt.pct(u.correction_rate)} of ${fmt.plural(u.dictations, 'dictation')}, like “Friday, sorry, Thursday”`,
            })
            : stat({ label: 'Corrected mid-sentence', value: null, foot: 'Recorded on dictations from this version onward' }))}
        </div>
      </div>`;
  },
};

/* ---- Settings ---------------------------------------------------------- */

views.settings = {
  title: 'Settings',
  tabsFor: () => [['general', 'General'], ['voice', 'Voice'], ['data', 'Data']],
  async load() {
    state.settings = await api('/api/settings');
    if (state.settingsTab === 'voice' && !state.devices) {
      state.devices = await api('/api/devices');
    }
    if (state.settingsTab === 'data' && !state.diagnostics) {
      state.diagnostics = await api('/api/diagnostics');
    }
  },
  render() {
    return `
      ${pageHead('Settings')}
      ${tabRow(this.tabsFor(), state.settingsTab, 'data-tab')}
      ${state.settings.editable ? '' : `<div class="banner info mt-6">${icon('info')} This window can't change settings because no engine is attached.</div>`}
      <div class="page-body">${this.panel()}</div>`;
  },
  panel() {
    const tab = state.settingsTab;
    if (tab === 'general') return generalPanel();
    if (tab === 'voice') return voicePanel();
    return dataPanel();
  },
  bind(root) {
    root.querySelectorAll('[data-tab]').forEach((tab) => {
      tab.addEventListener('click', async () => {
        state.settingsTab = tab.dataset.tab;
        syncHash();
        await refresh({ keepScroll: true, quiet: true });
      });
    });
  },
};

function countConflicts() {
  const triggers = (state.settings && state.settings.triggers) || [];
  return triggers.filter((t) => t.conflicts && t.conflicts.length).length;
}

const settingRow = (name, hint, control, attrs = '') => `
  <div class="setting" ${attrs}>
    <div class="grow"><div class="name">${esc(name)}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>
    <div class="control">${control}</div>
  </div>`;

function generalPanel() {
  const s = state.settings;
  const key = s.key || {};
  const settings = s.settings || {};

  const keyControl = key.configured
    ? `<div class="inline">
         <span class="mono muted">${esc(key.masked)}</span>
         <button class="btn small" data-act="test-key">Test connection</button>
         <button class="btn small" data-act="set-key">Change…</button>
       </div>
       <div id="key-result"></div>`
    : `<button class="btn primary small" data-act="set-key">Add API key…</button>`;

  const sourceHint = key.configured
    ? (key.source === 'keyring'
        ? 'Stored in Windows Credential Manager.'
        : 'Coming from your environment or .env file, not from Credential Manager.')
    : 'Sway cannot transcribe without one. Free keys come from console.groq.com.';

  return `
    <div class="stack-12">
      <section>
        ${sectionTitle('Groq')}
        <div class="list">${settingRow('API key', sourceHint, keyControl)}</div>
      </section>
      <section>
        ${sectionTitle('App')}
        <div class="list">
          ${settingRow('Start Sway when I sign in', 'Sway runs in the notification area.',
            toggle(settings.launch_at_login, 'data-act="set" data-field="launch_at_login" aria-label="Start Sway when I sign in"'))}
          ${settingRow('Overlay position', 'Where the status pill sits while you dictate.',
            `<select class="input" data-act="set" data-field="overlay_position" aria-label="Overlay position">
              ${(s.overlay_positions || []).map((p) =>
                `<option value="${esc(p)}" ${settings.overlay_position === p ? 'selected' : ''}>${esc(positionLabel(p))}</option>`).join('')}
            </select>`)}
        </div>
      </section>
    </div>`;
}

const positionLabel = (p) => ({
  'top-center': 'Top centre', 'bottom-center': 'Bottom centre',
  'top-right': 'Top right', 'bottom-right': 'Bottom right',
}[p] || p);

function voicePanel() {
  const s = state.settings;
  const settings = s.settings || {};
  const engine = s.engine || {};
  const devices = (state.devices && state.devices.devices) || [];
  // Served from sway.languages rather than hardcoded here.
  const langs = s.languages || [];
  // Self-terminates the moment the tab is no longer showing.
  watchActivationPreview();

  const deviceList = devices.length
    ? `<div class="list">
        ${[{ label: 'System default', value: '' }].concat(
          devices.map((d) => ({ label: d.name + (d.default ? ' (default)' : ''), value: d.name }))
        ).map((d) => {
          const active = (settings.audio_device || '') === d.value;
          return `<div class="row clickable" data-act="pick-device" data-device="${esc(d.value)}" tabindex="0" role="radio" aria-checked="${active}">
            <span class="dot ${active ? 'ok' : 'empty'}"></span>
            <div class="body"><div class="text">${esc(d.label)}</div></div>
            ${active ? '<span class="badge">In use</span>' : ''}
          </div>`;
        }).join('')}
      </div>`
    // Genuinely zero, so the zero is printed (§9.1).
    : card(stat({ label: 'Microphones found', value: '0', foot: 'Windows is not reporting any input device.' }));

  const activation = s.activation || {};

  return `
    <div class="stack-12">
      <section>
        ${sectionTitle('Shortcuts')}
        <div class="list">
          ${activationRow(activation, 'dictation', 'Dictation hotkey', 'Hold it, speak, release.')}
          ${activationRow(activation, 'command', 'Voice command hotkey', 'Hold it over selected text to transform it.')}
          ${handsFreeRow(activation)}
        </div>
      </section>
      ${registeredShortcutsTable(engine)}
      <section>
        ${sectionTitle('Language')}
        <div class="list">
          ${settingRow("I'm speaking", 'Leave on auto-detect unless Sway keeps guessing wrong.',
            `<select class="input" data-act="set" data-field="language" aria-label="Spoken language">
              <option value="">Auto-detect</option>
              ${langs.map((l) =>
                `<option value="${esc(l.code)}" ${settings.language === l.code ? 'selected' : ''}>${esc(l.label)}</option>`).join('')}
            </select>`)}
          ${settingRow('Write it in',
            'Sway normally writes back whatever you spoke. Pick a language here to always get that one instead.',
            `<select class="input" data-act="set" data-field="target_language" aria-label="Output language">
              <option value="">Same as spoken</option>
              ${langs.map((l) =>
                `<option value="${esc(l.code)}" ${settings.target_language === l.code ? 'selected' : ''}>${esc(l.label)}</option>`).join('')}
            </select>`)}
        </div>
      </section>
      <section>
        ${sectionTitle('Understanding')}
        <div class="list">
          ${settingRow('Fix what I corrected mid-sentence',
            contextHint(settings.context_mode),
            `<select class="input wide" data-act="set" data-field="context_mode" aria-label="Self-correction handling">
              ${(s.context_modes || []).map((m) =>
                `<option value="${esc(m)}" ${settings.context_mode === m ? 'selected' : ''}>${esc(contextLabel(m))}</option>`).join('')}
            </select>`)}
          ${settingRow('Remove filler words', 'Drops “um”, “uh”, and “you know” from the final text.',
            toggle(settings.remove_fillers !== false, 'data-act="set" data-field="remove_fillers" aria-label="Remove filler words"'))}
          ${settingRow('Learn names I use often',
            'Adds names and jargon you dictate repeatedly to your Dictionary. Everything it learns is listed there, and you can remove any of it.',
            toggle(settings.auto_learn_vocabulary !== false, 'data-act="set" data-field="auto_learn_vocabulary" aria-label="Learn names I use often"'))}
        </div>
      </section>
      <section>
        ${sectionTitle('Input device')}
        ${deviceList}
      </section>
    </div>`;
}

/** One binding chip, wrapped so the live-preview poller can paint .held
 * while it is physically down right now. */
function bindingChip(target, b) {
  return `<div class="stack-1 end">
    <span class="inline-1" data-live-binding="${esc(target)}" data-binding-value="${esc(b.value)}">
      ${capsFromLabel(b.label)}
      ${iconBtn('edit', 'Change', `data-act="edit-binding" data-target="${esc(target)}" data-value="${esc(b.value)}"`)}
      ${iconBtn('trash', 'Remove', `data-act="remove-binding" data-target="${esc(target)}" data-value="${esc(b.value)}"`)}
    </span>
    ${b.advisory ? `<div class="helper">${esc(b.advisory)}</div>` : ''}
  </div>`;
}

function bindingList(target, a) {
  const bindings = a.bindings || [];
  const hasConflict = a.conflicts && a.conflicts.length > 0;
  return `
    <div class="stack-2 end">
      ${bindings.length ? bindings.map((b) => bindingChip(target, b)).join('') : '<span class="small muted">Not set</span>'}
    </div>
    <div class="inline">
      <button class="btn small ghost" data-act="add-binding" data-target="${target}">${icon('plus')} Add</button>
      ${!a.is_default ? `<button class="btn small ghost" data-act="reset-binding" data-target="${target}">${icon('reset')} Reset</button>` : ''}
    </div>
    ${hasConflict ? `<div class="helper error">${icon('alert')}<span>Same as ${esc(a.conflicts[0])}. Only one will fire.</span></div>` : ''}`;
}

/**
 * The Dictation/Voice-command rows: every current binding with its own
 * edit/remove, a reset once the row is no longer default, and an Add.
 */
function activationRow(activation, target, label, hint) {
  return settingRow(label, hint, bindingList(target, activation[target] || {}));
}

/**
 * Hands-free dictation: its own mode toggle (off by default), the same
 * binding list as the two hold-based rows, and -- once there is a binding
 * -- whether it must be double-tapped.
 */
function handsFreeRow(activation) {
  const a = activation.hands_free || {};
  const bindings = a.bindings || [];
  const gesture = bindings.length
    ? (a.double_tap ? 'Double-tap a binding to start, double-tap again to stop.' : 'Tap a binding to start, tap again to stop.')
    : 'Tap once to start recording, tap again to stop. No holding.';

  return `
    ${settingRow('Hands-free dictation', gesture,
      `<div class="inline"><span data-live-hands-free></span>${toggle(!!a.enabled, 'data-act="set" data-field="hands_free_enabled" aria-label="Hands-free dictation"')}</div>`)}
    ${a.enabled ? settingRow('Hands-free binding', '', bindingList('hands_free', a)) : ''}
    ${a.enabled && bindings.length ? settingRow('Double-tap instead of a single tap', 'Harder to trigger by accident.',
      toggle(!!a.double_tap, 'data-act="set" data-field="hands_free_double_tap" aria-label="Double-tap"')) : ''}`;
}

/**
 * Read-only diagnostic: every binding Sway is actually tracking right now,
 * whether it registered cleanly, and whether it has ever fired. A shortcut
 * that never shows "fired" never reached Sway's hook, so the cause is
 * upstream of Sway (another app or the OS claimed it first).
 */
function registeredShortcutsTable(engine) {
  const bindings = engine.bindings || [];
  if (!bindings.length) return '';

  const statusNote = (b) => {
    if (b.status === 'invalid') return `<span class="small error-text">Invalid${b.detail ? `: ${esc(b.detail)}` : ''}</span>`;
    if (b.status === 'duplicate') return `<span class="small error-text">Duplicate: “${esc(b.detail)}” claimed it first</span>`;
    return `<span class="value">${b.fire_count ? `Fired ${fmt.int(b.fire_count)}× · last ${fmt.clock(b.last_fired_at)}` : 'Never fired'}</span>`;
  };

  return `
    <section>
      ${sectionTitle('Registered shortcuts', '<span class="small muted">What Sway is listening for right now</span>')}
      <div class="list">
        ${bindings.map((b) => `
          <div class="row">
            <span class="dot ${b.status === 'registered' ? 'ok' : 'act'}"></span>
            <div class="body">
              <div class="text">${esc(b.pretty || b.value)}</div>
              <div class="sub">${esc(b.label)}</div>
            </div>
            ${statusNote(b)}
          </div>`).join('')}
      </div>
    </section>`;
}

// Named states, not a slider: each label says what Sway will do.
const contextLabel = (m) => ({
  off: 'Leave it as I said it',
  conservative: 'Only when I clearly corrected myself',
  balanced: 'Whenever it looks like a correction',
}[m] || m);

const contextHint = (m) => ({
  off: 'Say “meeting at 7, no, 8” and you get exactly that, word for word.',
  conservative: 'Say “meeting at 7, no, 8” and you get “meeting at 8”. Sway only '
    + 'rewrites when you clearly replaced a name, time, date, or figure.',
  balanced: 'Catches more corrections, and will occasionally trim an aside you '
    + 'meant to keep.',
}[m] || '');

function dataPanel() {
  const s = state.settings;
  const settings = s.settings || {};
  const engine = s.engine || {};
  const retention = (fieldName, value, label) => `
    <select class="input" data-act="set" data-field="${fieldName}" data-numeric="1" aria-label="${esc(label)}">
      ${[[0, 'Keep forever'], [7, '7 days'], [30, '30 days'], [90, '90 days'], [365, '1 year']].map(
        ([v, l]) => `<option value="${v}" ${Number(value) === v ? 'selected' : ''}>${l}</option>`).join('')}
    </select>`;

  return `
    <div class="stack-12">
      <section>
        ${sectionTitle('Storage')}
        <div class="list">
          ${settingRow('Keep history for', 'Older dictations are removed automatically.',
            retention('history_retention_days', settings.history_retention_days, 'Keep history for'))}
          ${settingRow('Keep timings for', 'Latency measurements behind the Insights charts.',
            retention('latency_retention_days', settings.latency_retention_days, 'Keep timings for'))}
          ${settingRow('Data folder', '', `<span class="mono muted ellipsis selectable" data-tip-trunc>${esc(engine.data_directory || DASH)}</span>`)}
        </div>
      </section>
      <section>
        ${sectionTitle('Recent warnings and errors', '<button class="btn small ghost" data-act="refresh-diagnostics">' + icon('reset') + ' Refresh</button>')}
        <p class="small muted section-lead">Read straight from sway.log, so this shows up even when there is no console window to watch.</p>
        ${diagnosticsPanel()}
      </section>
      <section>
        ${sectionTitle('Danger zone')}
        <div class="banner start">
          <div class="grow">
            <div class="medium">Delete everything</div>
            <div class="small muted">Removes every transcript and every timing from this machine. This cannot be undone.</div>
          </div>
          <button class="btn danger small" data-act="delete-all">Delete all…</button>
        </div>
      </section>
    </div>`;
}

// sway.logging_setup writes "YYYY-MM-DD HH:mm:ss,SSS" in local time --
// parsed field-by-field rather than handed to `new Date(string)`.
function parseLogTimestamp(ts) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})$/.exec(ts);
  if (!m) return null;
  const [y, mo, day, h, mi, s, ms] = m.slice(1).map(Number);
  return new Date(y, mo - 1, day, h, mi, s, ms);
}

function formatDiagnosticTime(ts) {
  const date = parseLogTimestamp(ts);
  if (!date) return { label: ts, title: ts };
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  const clock = date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const label = sameDay ? clock : `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${clock}`;
  const deltaMin = Math.floor((now - date) / 60000);
  let relative = '';
  if (deltaMin >= 0 && deltaMin < 60) relative = deltaMin < 1 ? 'just now' : `${deltaMin}m ago`;
  else if (deltaMin >= 60 && deltaMin < 24 * 60) relative = `${Math.floor(deltaMin / 60)}h ago`;
  return { label: relative ? `${label} · ${relative}` : label, title: date.toLocaleString() };
}

function diagnosticsPanel() {
  const d = state.diagnostics;
  if (!d) return '<div class="skeleton block"></div>';
  if (!d.available) {
    return `<div class="banner info">No log file yet${d.path ? ` (looked in ${esc(d.path)})` : ''}.</div>`;
  }
  if (!d.entries.length) {
    return `<div class="banner info">${icon('check')}<span>No warnings or errors recorded. The log file is at <span class="mono">${esc(d.path)}</span>.</span></div>`;
  }
  // A run of identical level+logger+message entries is one event that
  // happened N times: collapse it into one row with a count and a span.
  const groups = [];
  for (const e of d.entries) {
    const last = groups[groups.length - 1];
    if (last && last.level === e.level && last.logger === e.logger && last.message === e.message) {
      last.count += 1;
      last.oldest = e.timestamp;
    } else {
      groups.push({ level: e.level, logger: e.logger, message: e.message, newest: e.timestamp, oldest: e.timestamp, count: 1 });
    }
  }
  return `<div class="list">
    ${groups.map((g) => {
      const newest = formatDiagnosticTime(g.newest);
      return `
      <div class="row start">
        <span class="dot top ${g.level === 'WARNING' ? 'warn' : 'act'}"></span>
        <div class="body">
          <div class="inline wrap small muted">
            <span data-tip="${esc(newest.title)}">${esc(newest.label)}</span>
            <span>·</span><span>${esc(g.level === 'WARNING' ? 'Warning' : 'Error')}</span>
            <span>·</span><span>${esc(g.logger)}</span>
            ${g.count > 1 ? `<span class="badge">×${g.count} since ${esc(formatDiagnosticTime(g.oldest).label)}</span>` : ''}
          </div>
          <div class="mono log-message selectable">${esc(g.message)}</div>
        </div>
      </div>`;
    }).join('')}
  </div>`;
}

/* ---- About ------------------------------------------------------------- */

views.about = {
  title: 'About Sway',
  async load() { if (!state.settings) state.settings = await api('/api/settings'); },
  render() {
    const engine = (state.settings && state.settings.engine) || {};
    const feature = (ic, head, body) => `
      <div class="feature">
        <span class="feature-icon">${icon(ic)}</span>
        <div><div class="medium">${head}</div><div class="small muted">${body}</div></div>
      </div>`;
    const entries = Object.entries(engine).filter(([, v]) => typeof v !== 'object' || v === null);
    return `
      ${pageHead('About Sway')}
      <div class="grid cols-2 page-body">
        ${titledCard('Where your words go', `
          <div class="stack">
            ${feature('voice', 'Audio is never stored.', 'It lives in memory while you hold the key and is discarded as soon as the clip is sent.')}
            ${feature('folder', 'Text stays on this machine.', 'Transcripts are written to SQLite files in your data folder. Nothing syncs anywhere.')}
            ${feature('globe', 'What reaches Groq.', 'The trimmed audio clip, and the transcript for cleanup. Nothing else leaves the machine.')}
            ${feature('check', 'No account, no server.', 'Sway has no login and runs no public network listener.')}
          </div>`)}
        ${titledCard('This build', `
          <div class="kv">
            ${entries.map(([k, v]) =>
              `<div class="kv-row"><span class="kv-key">${esc(k.replace(/_/g, ' '))}</span><span class="kv-val mono selectable" data-tip-trunc>${esc(v ?? DASH)}</span></div>`).join('')
              || '<div class="small muted">No engine attached.</div>'}
          </div>`)}
      </div>`;
  },
};

/* ------------------------------------------------------------ the drawer */

async function openEntry(id) {
  openDrawerShell();
  const body = el('drawer-body');
  el('drawer-title').textContent = 'Dictation';
  el('drawer-sub').textContent = '';
  body.innerHTML = '<div class="skeleton block"></div><div class="skeleton block"></div>';

  let d;
  try {
    d = await api('/api/session', { id });
  } catch (err) {
    body.innerHTML = `<div class="banner">${esc(err.message)}</div>`;
    return;
  }

  el('drawer-title').textContent = fmt.app(d.app);
  el('drawer-sub').textContent = `${fmt.date(d.created_at)} · ${fmt.clock(d.created_at)}`;

  const changed = d.raw_text && d.raw_text !== d.text;
  // Only sequential stages add up; sub-spans and the hold are listed apart
  // (see queries.STAGE_PARTS), exactly as on the Performance chart.
  const seq = d.stages.filter((s) => s.part === 'pipeline');
  const extra = d.stages.filter((s) => s.part !== 'pipeline');
  const total = seq.reduce((sum, s) => sum + s.duration_ms, 0) || 1;
  const PART_NOTE = { within: 'part of a stage above', hold: 'how long you held the key', other: 'not part of the total' };

  const meta = [
    ['Words', fmt.int(d.word_count)],
    ['Speaking rate', d.wpm === null ? 'Not measured' : `${fmt.dec(d.wpm, 0)} wpm`],
    ['Felt latency', d.run && d.run.release_to_paste_ms !== null ? fmt.ms(d.run.release_to_paste_ms) : 'Not recorded'],
    ['Language', d.language || 'Not recorded'],
    ['Result', d.status || 'Not recorded'],
  ];

  body.innerHTML = `
    <div class="fade-in">
      <div class="inline between"><span class="meta-label">What was pasted</span>
        ${iconBtn('copy', 'Copy', `data-act="copy" data-id="${esc(d.id)}"`)}</div>
      <div class="text-block selectable">${esc(d.text) || '<span class="muted">empty</span>'}</div>
    </div>
    ${changed ? `
      <div>
        <span class="meta-label">Before cleanup</span>
        <div class="text-block dim selectable">${esc(d.raw_text)}</div>
      </div>`
      : '<p class="small muted">Cleanup made no changes to this transcript.</p>'}
    ${seq.length ? `
      <div>
        <div class="inline between"><span class="meta-label">Where the time went</span><span class="small muted">${fmt.ms(total)} after release</span></div>
        <div class="stacked">${seq.map((s, i) =>
          `<i class="${i % 3 === 1 ? 'alt2' : i % 3 === 2 ? 'alt3' : ''}" style="width:${((s.duration_ms / total) * 100).toFixed(2)}%" data-tip="${esc(`${s.stage} · ${fmt.ms(s.duration_ms)}`)}"></i>`).join('')}</div>
        <div class="legend-rows mt-3">${seq.map((s, i) => `
          <div class="legend-row">
            <span class="swatch ${i % 3 === 1 ? 'alt2' : i % 3 === 2 ? 'alt3' : ''}"></span>
            <span class="name">${esc(s.stage)}</span>
            <span class="val">${fmt.ms(s.duration_ms)}</span>
          </div>`).join('')}
          ${extra.map((s) => `
          <div class="legend-row">
            <span class="name muted">${esc(s.stage)} <span class="small">· ${esc(PART_NOTE[s.part] || PART_NOTE.other)}</span></span>
            <span class="val muted">${fmt.ms(s.duration_ms)}</span>
          </div>`).join('')}</div>
      </div>` : ''}
    <div>
      <span class="meta-label">Details</span>
      <div class="kv">${meta.map(([k, v]) =>
        `<div class="kv-row"><span class="kv-key">${esc(k)}</span><span class="kv-val">${esc(v)}</span></div>`).join('')}</div>
    </div>`;
}

/* -------------------------------------------------------------- actions */

async function copyEntry(id, button) {
  const entry = await api('/api/session', { id });
  try {
    await navigator.clipboard.writeText(entry.text || '');
    copiedFeedback(button);
  } catch {
    toast("Couldn't reach the clipboard");
  }
}

async function deleteEntry(id) {
  // Optimistic with no confirm (§9.3): the row collapses at once. There is
  // no restore path in the store, and keeping deleted text on disk would be
  // the opposite of what delete means -- so the toast offers the honest
  // version: copy the text back to the clipboard, not "Undo".
  const row = document.querySelector(`.row[data-id="${CSS.escape(id)}"]`);
  const entry = await api('/api/session', { id });
  if (row) {
    row.style.height = `${row.offsetHeight}px`;
    row.classList.add('collapsing');
    requestAnimationFrame(() => { row.style.height = '0px'; row.style.opacity = '0'; row.style.paddingTop = '0'; row.style.paddingBottom = '0'; });
  }
  try {
    await post('/api/history/delete', { id });
  } catch (err) {
    if (row) { row.style.height = ''; row.style.opacity = ''; row.style.paddingTop = ''; row.style.paddingBottom = ''; row.classList.remove('collapsing'); }
    throw err;
  }
  state.lastDeleted = entry;
  setTimeout(() => refresh({ keepScroll: true }), 200);
  toast('Dictation deleted', {
    label: 'Copy its text',
    run: async () => {
      try {
        await navigator.clipboard.writeText(state.lastDeleted.text || '');
        toast('Text copied to the clipboard');
      } catch { toast("Couldn't reach the clipboard"); }
    },
  });
}

function confirmDeleteAll() {
  openModal({
    title: 'Delete every dictation?',
    body: '<p class="muted">This removes every transcript and every timing stored on this machine. You can’t undo this.</p>',
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn danger" data-act="confirm-delete-all">Delete everything</button>`,
    focus: '[data-act="close-modal"]',
  });
}

/**
 * Settings apply immediately (§7 Toggle) and answer through the control
 * itself, not a toast (§11.1). A restart requirement is an inline note
 * under the row; a failed save reverts and says so next to the control.
 */
async function saveSetting(fieldName, value, control) {
  try {
    state.settings = await post('/api/settings', { [fieldName]: value });
    const applied = state.settings.applied || {};
    await refresh({ keepScroll: true, quiet: true });
    if (applied.restart_required) rowNote(fieldName, 'Restart Sway to apply', false);
  } catch (err) {
    await refresh({ keepScroll: true, quiet: true });
    rowNote(fieldName, `Couldn't save. ${err.message}`, true);
  }
  if (control && control.isConnected) control.focus();
}

function rowNote(fieldName, message, isError) {
  const control = document.querySelector(`[data-field="${CSS.escape(fieldName)}"]`);
  const slot = control && control.closest('.control');
  if (!slot) { toast(message); return; }
  slot.querySelectorAll('.row-note').forEach((n) => n.remove());
  slot.insertAdjacentHTML('beforeend',
    `<div class="helper row-note ${isError ? 'error' : ''}">${isError ? icon('alert') : ''}<span>${esc(message)}</span></div>`);
}

function keyDialog() {
  openModal({
    title: 'Groq API key',
    body: `
      ${field('key-input', 'Paste your key', `
        <div class="reveal-wrap">
          <input class="input mono" id="key-input" type="password" placeholder="gsk_…" autocomplete="off" spellcheck="false">
          ${iconBtn('eye', 'Show key', 'data-act="toggle-key-visibility"')}
        </div>`,
        'Free keys come from console.groq.com. It is stored in Windows Credential Manager, never in a file Sway writes.')}`,
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-key">Save key</button>`,
  });
}

async function saveKey(button) {
  const input = el('key-input');
  const value = input.value.trim();
  if (!value) { fieldError('key-input', 'Paste a key first.'); input.focus(); return; }
  fieldError('key-input', '');
  try {
    const result = await busy(button, { doing: 'Checking…' }, () => post('/api/key', { key: value }));
    closeModal();
    state.settings = null;
    await refresh({ keepScroll: true, quiet: true });
    toast(result.warning || 'Key saved');
  } catch (err) {
    fieldError('key-input', `Groq rejected that key. ${err.message}`);
  }
}

async function testKey(button) {
  const target = el('key-result');
  try {
    const result = await busy(button, { doing: 'Testing…' }, () => post('/api/key/test', {}));
    if (!target) return;
    target.innerHTML = result.ok
      ? `<div class="helper inline"><span class="dot ok"></span> Connected · ${esc(result.model || '')} · ${fmt.ms(result.latency_ms)}</div>`
      : `<div class="helper error">${icon('alert')}<span>${esc(result.detail || 'Groq rejected the key.')}</span></div>`;
  } catch (err) {
    if (target) target.innerHTML = `<div class="helper error">${icon('alert')}<span>${esc(err.message)}</span></div>`;
  }
}

function triggerDialog(existing) {
  const t = existing || { id: '', name: '', phrase: '', payload: '', type: 'macro' };
  openModal({
    title: existing ? 'Edit snippet' : 'New snippet',
    body: `
      ${field('t-name', 'Name <span class="muted">(optional)</span>',
        `<input class="input" id="t-name" value="${esc(t.name || '')}" placeholder="Intro email" maxlength="80">`)}
      ${field('t-phrase', 'When I say',
        `<input class="input" id="t-phrase" value="${esc(t.phrase)}" placeholder="intro email" maxlength="80">`,
        'Say it, or “use my …”, while dictating. Matched on this machine: no delay, no tokens.')}
      ${field('t-type', 'Insert',
        `<select class="input" id="t-type">
          <option value="macro" ${t.type === 'macro' ? 'selected' : ''}>A fixed piece of text</option>
          <option value="date" ${t.type === 'date' ? 'selected' : ''}>Today's date</option>
        </select>`)}
      <div id="t-payload-wrap" class="${t.type === 'date' ? 'hidden' : ''}">
        ${field('t-payload', 'Text to insert',
          `<textarea class="input" id="t-payload" maxlength="4000" placeholder="you@example.com">${esc(t.payload)}</textarea>`)}
      </div>`,
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-trigger" data-id="${esc(t.id)}">Save snippet</button>`,
  });
  el('t-type').addEventListener('change', (event) => {
    el('t-payload-wrap').classList.toggle('hidden', event.target.value === 'date');
  });
  el('t-phrase').addEventListener('blur', () => {
    if (el('t-phrase').value.trim()) fieldError('t-phrase', '');
  });
}

async function saveTrigger(id, button) {
  const body = {
    id: id || undefined,
    name: el('t-name').value,
    phrase: el('t-phrase').value,
    type: el('t-type').value,
    payload: el('t-payload').value,
  };
  if (!body.phrase.trim()) { fieldError('t-phrase', 'Add the phrase you will say.'); el('t-phrase').focus(); return; }
  try {
    state.settings = await busy(button, { doing: 'Saving…' }, () => post('/api/triggers/save', body));
    closeModal();
    await refresh({ keepScroll: true, quiet: true });
    toast(id ? 'Snippet saved' : 'Snippet added');
  } catch (err) {
    fieldError(body.type === 'macro' && /text|payload/i.test(err.message) ? 't-payload' : 't-phrase', err.message);
  }
}

function appRuleDialog() {
  const styles = (state.settings && state.settings.cleanup_styles) || [];
  openModal({
    title: 'Add app rule',
    body: `
      ${field('r-process', 'Application',
        '<input class="input mono" id="r-process" placeholder="slack.exe" maxlength="120" spellcheck="false">',
        'The executable name, as Windows reports it.')}
      ${field('r-style', 'Write like',
        `<select class="input" id="r-style">
          ${styles.map((s) => `<option value="${esc(s)}">${esc(styleLabel(s))}</option>`).join('')}
        </select>`)}`,
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-rule">Save rule</button>`,
  });
}

function termDialog() {
  openModal({
    title: 'Add to dictionary',
    body: field('d-terms', 'Words Sway keeps getting wrong',
      '<textarea class="input" id="d-terms" placeholder="Ashwini&#10;Kubernetes"></textarea>',
      'One per line. Sent to the transcriber as a hint so it knows to expect them.'),
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-terms">Add words</button>`,
  });
}

/* ------------------------------------------------------- event delegation */

document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-act]');
  if (!target) {
    // A click on a history row that missed an action button opens the detail.
    const row = event.target.closest('.row[data-id]');
    if (row && !window.getSelection().toString()) await openEntry(row.dataset.id);
    return;
  }
  const act = target.dataset.act;
  const id = target.dataset.id;

  try {
    // Onboarding owns its own verbs; everything else falls through below.
    if (act.startsWith('ob-')) { await onboardingAction(act, target); return; }

    switch (act) {
      case 'copy': event.stopPropagation(); await copyEntry(id, target); break;
      case 'delete': event.stopPropagation(); await deleteEntry(id); break;
      case 'delete-all': confirmDeleteAll(); break;
      case 'confirm-delete-all':
        await busy(target, { doing: 'Deleting…' }, () => post('/api/history/delete-all', { confirm: true }));
        closeModal(); await refresh({ quiet: true }); toast('Everything deleted');
        break;
      case 'close-modal': closeModal(); break;
      case 'go-history': await go('history'); el('search') && el('search').focus(); break;
      case 'go-key': state.settingsTab = 'general'; await go('settings'); break;
      case 'go-voice': state.settingsTab = 'voice'; await go('settings'); break;
      case 'go-voice-profile': await go('voice'); break;
      case 'clear-search': {
        const input = el(target.dataset.for);
        if (input) { input.value = ''; input.dispatchEvent(new Event('input')); }
        break;
      }
      case 'clear-history-search': state.query = ''; await refresh({ keepFocus: '#search' }); break;
      case 'clear-snippet-search': state.snippetQuery = ''; await refresh({ keepFocus: '#snippet-search' }); break;

      case 'toggle-agent': {
        const on = target.getAttribute('aria-checked') === 'true';
        target.setAttribute('aria-checked', String(!on)); // optimistic (§9.3)
        try {
          await post('/api/listening', { listening: !on });
        } catch (err) {
          target.setAttribute('aria-checked', String(on));
          throw err;
        }
        await refresh({ keepScroll: true, quiet: true });
        break;
      }

      case 'set-key': keyDialog(); break;
      case 'save-key': await saveKey(target); break;
      case 'test-key': await testKey(target); break;
      case 'toggle-key-visibility': {
        const input = el('key-input');
        input.type = input.type === 'password' ? 'text' : 'password';
        target.setAttribute('aria-label', input.type === 'password' ? 'Show key' : 'Hide key');
        target.setAttribute('data-tip', input.type === 'password' ? 'Show key' : 'Hide key');
        break;
      }

      case 'add-transform': openTransformSheet(null); break;
      case 'edit-transform': {
        const list = (state.settings && state.settings.transforms) || [];
        const existing = list.find((t) => t.id === target.dataset.id);
        if (existing) openTransformSheet(existing);
        break;
      }
      case 'close-transform-sheet':
        if (sheetState && sheetDirty) await saveSheetNow();
        closeTransformSheet();
        await refresh({ keepScroll: true, quiet: true });
        break;
      case 'add-transform-shortcut':
      case 'edit-transform-shortcut': {
        if (!sheetState) break;
        const index = target.dataset.index !== undefined ? Number(target.dataset.index) : -1;
        const snapshot = { ...sheetState };
        captureBindingDialog({
          current: index >= 0 ? (sheetState.shortcuts || [])[index] : undefined,
          title: index >= 0 ? 'Press the new key, combination, or mouse button' : 'Press a key, combination, or mouse button',
          hint: 'Hold modifiers together for a combo (like Ctrl+Win), add a key for a shortcut (like Ctrl+Alt+P), or click a mouse button (middle, back, or forward).',
          onCaptured: async (spec) => {
            sheetState = snapshot;
            const shortcuts = [...(sheetState.shortcuts || [])];
            if (index >= 0) shortcuts[index] = spec; else shortcuts.push(spec);
            sheetState.shortcuts = shortcuts;
            renderTransformSheet();
            await saveSheetNow();
            renderTransformSheet();
          },
          onCancel: () => { sheetState = snapshot; renderTransformSheet(); },
        });
        break;
      }
      case 'remove-transform-shortcut': {
        if (!sheetState) break;
        const index = Number(target.dataset.index);
        sheetState.shortcuts = (sheetState.shortcuts || []).filter((_, i) => i !== index);
        await saveSheetNow();
        renderTransformSheet();
        break;
      }
      case 'delete-transform': {
        event.stopPropagation();
        const list = (state.settings && state.settings.transforms) || [];
        const gone = list.find((t) => t.id === target.dataset.id);
        if (sheetState && gone && sheetState.id === gone.id) closeTransformSheet();
        state.settings = await post('/api/transforms/delete', { id: target.dataset.id });
        await refresh({ keepScroll: true, quiet: true });
        // Immediate, with an undo that really restores it (§9.3).
        toast(`Deleted “${gone ? gone.name : 'transform'}”`, gone && {
          label: 'Undo',
          run: async () => {
            await post('/api/transforms/save', gone);
            await refresh({ keepScroll: true, quiet: true });
            toast('Transform restored');
          },
        });
        break;
      }
      case 'reset-transforms':
        closeTransformSheet();
        state.settings = await post('/api/transforms/reset', {});
        await refresh({ keepScroll: true, quiet: true });
        toast('Built-in transforms restored');
        break;

      case 'add-trigger': triggerDialog(null); break;
      case 'copy-snippet': {
        const list = (state.settings && state.settings.triggers) || [];
        const snippet = list.find((t) => t.id === target.dataset.id);
        if (!snippet) break;
        try {
          await navigator.clipboard.writeText(snippet.payload);
          copiedFeedback(target);
        } catch {
          // A webview can refuse clipboard access outright. Say so rather
          // than claiming a copy that did not happen.
          toast("Couldn't reach the clipboard");
        }
        break;
      }
      case 'edit-trigger': {
        const triggers = (state.settings && state.settings.triggers) || [];
        triggerDialog(triggers.find((t) => t.id === id));
        break;
      }
      case 'save-trigger': await saveTrigger(target.dataset.id, target); break;
      case 'delete-trigger': {
        const gone = ((state.settings && state.settings.triggers) || []).find((t) => t.id === id);
        state.settings = await post('/api/triggers/delete', { id });
        await refresh({ keepScroll: true, quiet: true });
        // A real undo: the snippet is saved again with the same phrase,
        // text and enabled state (§9.3).
        toast('Snippet deleted', gone && {
          label: 'Undo',
          run: async () => {
            state.settings = await post('/api/triggers/save', {
              name: gone.name, phrase: gone.phrase, type: gone.type, payload: gone.payload, enabled: gone.enabled,
            });
            await refresh({ keepScroll: true, quiet: true });
            toast('Snippet restored');
          },
        });
        break;
      }
      case 'toggle-trigger': {
        const triggers = (state.settings && state.settings.triggers) || [];
        const trigger = triggers.find((t) => t.id === id);
        if (!trigger) break;
        target.setAttribute('aria-checked', String(!trigger.enabled)); // optimistic
        try {
          state.settings = await post('/api/triggers/save', { ...trigger, enabled: !trigger.enabled });
        } catch (err) {
          target.setAttribute('aria-checked', String(trigger.enabled));
          throw err;
        }
        await refresh({ keepScroll: true, quiet: true });
        break;
      }

      case 'add-rule': appRuleDialog(); break;
      case 'save-rule': {
        const process = el('r-process').value.trim();
        if (!process) { fieldError('r-process', 'Type the app’s executable name, like slack.exe.'); el('r-process').focus(); break; }
        try {
          state.settings = await busy(target, { doing: 'Saving…' }, () => post('/api/app-rules/save', {
            process, style: el('r-style').value,
          }));
          closeModal(); await refresh({ keepScroll: true, quiet: true }); toast('Rule added');
        } catch (err) { fieldError('r-process', err.message); }
        break;
      }
      case 'delete-rule':
        state.settings = await post('/api/app-rules/delete', { process: target.dataset.process });
        await refresh({ keepScroll: true, quiet: true });
        toast('Rule removed');
        break;

      case 'add-term': termDialog(); break;
      case 'save-terms': {
        const terms = el('d-terms').value.split(/[\n,]/).map((t) => t.trim()).filter(Boolean);
        if (!terms.length) { fieldError('d-terms', 'Type at least one word.'); el('d-terms').focus(); break; }
        try {
          await busy(target, { doing: 'Adding…' }, () => post('/api/dictionary/add', { terms }));
          closeModal(); state.dictionary = null; await refresh({ keepScroll: true, quiet: true });
          toast(terms.length === 1 ? 'Word added' : `${terms.length} words added`);
        } catch (err) { fieldError('d-terms', err.message); }
        break;
      }
      case 'remove-term': {
        const term = target.dataset.term;
        await post('/api/dictionary/remove', { terms: [term] });
        state.dictionary = null; await refresh({ keepScroll: true, quiet: true });
        toast(`Removed “${term}”`, {
          label: 'Undo',
          run: async () => {
            await post('/api/dictionary/add', { terms: [term] });
            state.dictionary = null; await refresh({ keepScroll: true, quiet: true });
          },
        });
        break;
      }

      case 'pin-term':
        await post('/api/dictionary/pin', {
          term: target.dataset.term,
          pinned: !target.dataset.pinned,
        });
        state.dictionary = null; await refresh({ keepScroll: true, quiet: true });
        break;

      case 'export-vocab': {
        const pack = await api('/api/dictionary/export');
        // Shown as text rather than pushed as a download: a webview can
        // refuse a file save outright.
        openModal({
          title: 'Share your vocabulary',
          body: field('v-pack', 'Vocabulary pack',
            `<textarea class="input mono selectable" id="v-pack" rows="9" readonly>${esc(JSON.stringify(pack, null, 2))}</textarea>`,
            'Send this to a teammate and have them paste it into Import. It carries only the words, not how often you use them or which you pinned.'),
          actions: `<div class="spacer"></div>
            <button class="btn ghost" data-act="close-modal">Done</button>
            <button class="btn primary" data-act="copy-pack">${icon('copy')} Copy pack</button>`,
          focus: '[data-act="copy-pack"]',
        });
        break;
      }
      case 'copy-pack':
        try {
          await navigator.clipboard.writeText(el('v-pack').value);
          target.innerHTML = `${icon('check')} Copied`;
        } catch {
          el('v-pack').select();
          toast('Select it and press Ctrl+C');
        }
        break;

      case 'import-vocab':
        openModal({
          title: 'Import a vocabulary pack',
          body: field('v-import', 'Pack',
            '<textarea class="input mono" id="v-import" rows="9" placeholder=\'{"version": 1, "terms": ["..."]}\'></textarea>',
            'Paste a pack someone exported. Its words are added alongside yours, labelled “shared”, and you can remove any of them.'),
          actions: `<div class="spacer"></div>
            <button class="btn ghost" data-act="close-modal">Cancel</button>
            <button class="btn primary" data-act="save-import">Import words</button>`,
        });
        break;
      case 'save-import': {
        let pack;
        try {
          pack = JSON.parse(el('v-import').value);
        } catch {
          fieldError('v-import', 'That isn’t valid JSON. Paste the whole pack, braces and all.');
          break;
        }
        try {
          const result = await busy(target, { doing: 'Importing…' }, () => post('/api/dictionary/import', { pack }));
          closeModal(); state.dictionary = null; await refresh({ keepScroll: true, quiet: true });
          toast(result.added
            ? `${fmt.plural(result.added, 'word')} added`
            : 'You already had every word in that pack');
        } catch (err) { fieldError('v-import', err.message); }
        break;
      }

      case 'pick-device': await saveSetting('audio_device', target.dataset.device || null); break;
      case 'edit-hotkey': hotkeyDialog(); break;
      case 'add-binding': {
        const bindingTarget = target.dataset.target;
        captureBindingDialog({
          title: 'Press a key, combination, or mouse button',
          hint: 'Hold a key on its own, hold modifiers together for a combo (like Ctrl+Win), add a key for a shortcut (like Ctrl+Alt+P), or click a mouse button (middle, back, or forward).',
          onCaptured: async (spec) => {
            const before = bindingValues(bindingTarget);
            try {
              state.settings = await post('/api/shortcuts/save', { target: bindingTarget, op: 'add', binding: spec });
              await refresh({ keepScroll: true, quiet: true });
              celebrateBindings(bindingTarget, before);
              toast('Shortcut added');
            } catch (err) { toast(err.message); }
          },
        });
        break;
      }
      case 'edit-binding': {
        const bindingTarget = target.dataset.target;
        const oldValue = target.dataset.value;
        captureBindingDialog({
          title: 'Press the new key, combination, or mouse button',
          hint: 'This replaces the binding you clicked edit on.',
          current: oldValue,
          onCaptured: async (spec) => {
            const before = bindingValues(bindingTarget);
            try {
              state.settings = await post('/api/shortcuts/save', {
                target: bindingTarget, op: 'replace', old_binding: oldValue, binding: spec,
              });
              await refresh({ keepScroll: true, quiet: true });
              celebrateBindings(bindingTarget, before);
              toast('Shortcut changed');
            } catch (err) { toast(err.message); }
          },
        });
        break;
      }
      case 'remove-binding':
        try {
          state.settings = await post('/api/shortcuts/save', {
            target: target.dataset.target, op: 'remove', binding: target.dataset.value,
          });
          await refresh({ keepScroll: true, quiet: true });
          toast('Shortcut removed');
        } catch (err) {
          toast(err.message);
        }
        break;
      case 'reset-binding':
        try {
          await post('/api/shortcuts/reset', { target: target.dataset.target });
          await refresh({ keepScroll: true, quiet: true });
          toast('Shortcuts reset to default');
        } catch (err) {
          toast(err.message);
        }
        break;
      case 'refresh-diagnostics':
        state.diagnostics = await busy(target, { doing: 'Refreshing…' }, () => api('/api/diagnostics'));
        await refresh({ keepScroll: true, quiet: true });
        break;

      case 'set': {
        // Toggles carry their state in aria-checked; selects in value.
        if (target.classList.contains('toggle')) {
          const next = target.getAttribute('aria-checked') !== 'true';
          target.setAttribute('aria-checked', String(next)); // optimistic (§9.3)
          await saveSetting(target.dataset.field, next, target);
        }
        break;
      }
      default: break;
    }
  } catch (err) {
    toast(err.message);
  }
});

// Selects fire change, not click.
document.addEventListener('change', async (event) => {
  const target = event.target.closest('[data-act="set"]');
  if (target && target.tagName === 'SELECT') {
    const raw = target.value;
    await saveSetting(target.dataset.field, target.dataset.numeric ? Number(raw) : (raw || null), target);
    return;
  }
  const rule = event.target.closest('[data-act="set-rule"]');
  if (rule) {
    try {
      state.settings = await post('/api/app-rules/save', {
        process: rule.dataset.process, style: rule.value,
      });
      await refresh({ keepScroll: true, quiet: true });
    } catch (err) { toast(`Couldn't save. ${err.message}`); }
  }
});

el('scrim').addEventListener('click', closeDrawer);
el('drawer-close').addEventListener('click', closeDrawer);
el('modal-scrim').addEventListener('click', (event) => {
  if (event.target !== el('modal-scrim')) return;
  // Not while a capture is running: a stray click must not lose the gesture.
  if (document.getElementById('ab-caps')) return;
  if (sheetState) { el('modal-scrim').querySelector('[data-act="close-transform-sheet"]').click(); return; }
  closeModal();
});

/* ============================================================= popovers */
/* One floating-surface vocabulary for the select list and the text menu:
   --field fill, --shadow-pop, 140ms enter / 100ms exit from the trigger's
   side (§10.3), keyboard first. Only one is open at a time.               */

let openPop = null;

function closePopover({ restoreFocus = true } = {}) {
  const pop = openPop;
  if (!pop) return;
  openPop = null;
  document.removeEventListener('keydown', pop.onKey, true);
  document.removeEventListener('mousedown', pop.onOutside, true);
  window.removeEventListener('resize', pop.onResize);
  el('main').removeEventListener('scroll', pop.onResize);
  pop.node.classList.remove('open');
  pop.node.classList.add('closing');
  setTimeout(() => pop.node.remove(), 100);
  if (pop.trigger) pop.trigger.setAttribute('aria-expanded', 'false');
  if (restoreFocus && pop.trigger && pop.trigger.isConnected) pop.trigger.focus();
}

/**
 * Show a popover of rows. `items`: [{label, hint, disabled, selected, run}]
 * or {separator: true}. `at`: an element to drop below, or {x, y} for a
 * context menu.
 */
function showPopover({ items, at, trigger, role = 'menu', minWidth = 0, label = '' }) {
  closePopover({ restoreFocus: false });
  const node = document.createElement('div');
  node.className = `popover ${role === 'listbox' ? 'select-pop' : 'context-pop'}`;
  node.setAttribute('role', role);
  if (label) node.setAttribute('aria-label', label);
  node.innerHTML = items.map((it, i) => (it.separator
    ? '<div class="pop-sep" role="separator"></div>'
    : `<div class="pop-item${it.disabled ? ' disabled' : ''}${it.selected ? ' selected' : ''}" data-i="${i}"
          role="${role === 'listbox' ? 'option' : 'menuitem'}" aria-selected="${it.selected ? 'true' : 'false'}"
          ${it.disabled ? 'aria-disabled="true"' : ''}>
         <span class="pop-check">${it.selected ? icon('check') : ''}</span>
         <span class="pop-label">${esc(it.label)}</span>
         ${it.hint ? `<span class="pop-hint">${esc(it.hint)}</span>` : ''}
       </div>`)).join('');
  document.body.appendChild(node);
  node.style.minWidth = `${Math.max(minWidth, 180)}px`;

  const place = () => {
    const box = node.getBoundingClientRect();
    let x; let y; let up = false;
    if (at instanceof Element) {
      const r = at.getBoundingClientRect();
      x = r.left;
      y = r.bottom + 4;
      if (y + box.height > window.innerHeight - 8) { y = r.top - box.height - 4; up = true; }
    } else {
      x = at.x; y = at.y;
      if (y + box.height > window.innerHeight - 8) { y = at.y - box.height; up = true; }
    }
    x = Math.max(8, Math.min(x, window.innerWidth - box.width - 8));
    y = Math.max(8, y);
    node.style.left = `${Math.round(x)}px`;
    node.style.top = `${Math.round(y)}px`;
    node.classList.toggle('from-below', up);
  };
  place();

  const rows = [...node.querySelectorAll('.pop-item')];
  const enabled = rows.filter((r) => !r.classList.contains('disabled'));
  let active = Math.max(0, enabled.findIndex((r) => r.classList.contains('selected')));
  const setActive = (i) => {
    if (!enabled.length) return;
    active = (i + enabled.length) % enabled.length;
    rows.forEach((r) => r.classList.remove('active'));
    enabled[active].classList.add('active');
    enabled[active].scrollIntoView({ block: 'nearest' });
  };
  if (role === 'listbox' || trigger) setActive(active);

  const choose = (row) => {
    const item = items[Number(row.dataset.i)];
    if (!item || item.disabled) return;
    closePopover();
    item.run();
  };

  let typed = '';
  let typedTimer = null;
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePopover(); return; }
    if (e.key === 'Tab') { closePopover({ restoreFocus: false }); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(active + 1); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); setActive(active - 1); return; }
    if (e.key === 'Home') { e.preventDefault(); setActive(0); return; }
    if (e.key === 'End') { e.preventDefault(); setActive(enabled.length - 1); return; }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); if (enabled[active]) choose(enabled[active]); return; }
    if (e.key.length === 1 && !e.ctrlKey && !e.altKey) {
      // Type-ahead (§7 Inputs).
      typed += e.key.toLowerCase();
      clearTimeout(typedTimer);
      typedTimer = setTimeout(() => { typed = ''; }, 600);
      const hit = enabled.findIndex((r) => r.textContent.trim().toLowerCase().startsWith(typed));
      if (hit >= 0) setActive(hit);
    }
  };
  const onOutside = (e) => {
    if (node.contains(e.target) || (trigger && trigger.contains(e.target))) return;
    closePopover({ restoreFocus: false });
  };
  const onResize = () => closePopover({ restoreFocus: false });
  node.addEventListener('mousemove', (e) => {
    const row = e.target.closest('.pop-item:not(.disabled)');
    if (row) setActive(enabled.indexOf(row));
  });
  node.addEventListener('click', (e) => {
    const row = e.target.closest('.pop-item');
    if (row) choose(row);
  });
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('mousedown', onOutside, true);
  window.addEventListener('resize', onResize);
  el('main').addEventListener('scroll', onResize, { passive: true });
  if (trigger) trigger.setAttribute('aria-expanded', 'true');
  openPop = { node, onKey, onOutside, onResize, trigger };
  requestAnimationFrame(() => node.classList.add('open'));
}

/* ---- Custom select (§7 Inputs) -------------------------------------------
   The native <select> stays in the DOM as the source of truth -- its value,
   its change event and every handler bound to it keep working -- and is
   visually replaced by a button that opens a keyboard-navigable list.     */

function enhanceSelects(root) {
  root.querySelectorAll('select.input:not([data-enhanced])').forEach((sel) => {
    sel.dataset.enhanced = '1';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `${sel.className} select-btn`;
    button.setAttribute('aria-haspopup', 'listbox');
    button.setAttribute('aria-expanded', 'false');
    if (sel.id) {
      button.id = `${sel.id}-btn`;
      const lbl = document.querySelector(`label[for="${CSS.escape(sel.id)}"]`);
      if (lbl) lbl.htmlFor = button.id;
    }
    const aria = sel.getAttribute('aria-label');
    if (aria) button.setAttribute('aria-label', aria);
    button.innerHTML = `<span class="select-value"></span>${icon('chevron', 'select-chev')}`;
    sel.classList.add('native-hidden');
    sel.tabIndex = -1;
    sel.setAttribute('aria-hidden', 'true');
    sel.after(button);
    const sync = () => {
      const opt = sel.selectedOptions[0];
      button.querySelector('.select-value').textContent = opt ? opt.textContent : '';
    };
    sync();
    sel.addEventListener('change', sync);
    const open = () => {
      if (openPop && openPop.trigger === button) { closePopover(); return; }
      showPopover({
        role: 'listbox',
        at: button,
        trigger: button,
        minWidth: button.offsetWidth,
        label: aria || '',
        items: [...sel.options].map((o) => ({
          label: o.textContent,
          selected: o.selected,
          disabled: o.disabled,
          run: () => {
            if (sel.value === o.value) return;
            sel.value = o.value;
            sync();
            sel.dispatchEvent(new Event('change', { bubbles: true }));
          },
        })),
      });
    };
    button.addEventListener('click', open);
    button.addEventListener('keydown', (e) => {
      if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key) && !openPop) { e.preventDefault(); open(); }
    });
  });
}

/* ---- Text menu (§13) ------------------------------------------------------
   WebView2's own menus are off outside debug builds, so text fields would
   otherwise have no menu at all. This is ours: Cut, Copy, Paste, Select
   all, with their shortcuts, and only the ones that can work enabled.     */

document.addEventListener('contextmenu', (event) => {
  event.preventDefault();
  const field = event.target.closest && event.target.closest('input:not([type=checkbox]), textarea');
  const selectable = !field && event.target.closest && event.target.closest('.selectable');
  if (!field && !selectable) { closePopover({ restoreFocus: false }); return; }

  const editable = field && !field.readOnly && !field.disabled;
  const hasSelection = field
    ? field.selectionStart !== field.selectionEnd
    : !!window.getSelection().toString();
  const masked = field && field.type === 'password';
  if (field) field.focus();

  const insert = (text) => {
    const start = field.selectionStart; const end = field.selectionEnd;
    field.setRangeText(text, start, end, 'end');
    field.dispatchEvent(new Event('input', { bubbles: true }));
  };

  showPopover({
    at: { x: event.clientX, y: event.clientY },
    label: 'Text',
    items: [
      { label: 'Cut', hint: 'Ctrl+X', disabled: !editable || !hasSelection || masked,
        run: () => { field.focus(); document.execCommand('cut'); field.dispatchEvent(new Event('input', { bubbles: true })); } },
      { label: 'Copy', hint: 'Ctrl+C', disabled: !hasSelection || masked,
        run: () => { if (field) field.focus(); document.execCommand('copy'); } },
      { label: 'Paste', hint: 'Ctrl+V', disabled: !editable,
        run: async () => {
          field.focus();
          try { insert(await navigator.clipboard.readText()); } catch {
            if (!document.execCommand('paste')) toast('Press Ctrl+V to paste');
          }
        } },
      { separator: true },
      { label: 'Select all', hint: 'Ctrl+A',
        run: () => {
          if (field) { field.focus(); field.select(); return; }
          const range = document.createRange();
          range.selectNodeContents(selectable);
          const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
        } },
    ],
  });
});

/* ---- Live-update polish ---------------------------------------------------
   On an in-place refresh, a number that changed cross-fades rather than
   counting up (§7 Cards), and a history row that was not there before
   flashes indigo once so the user sees where it landed (§7 Lists).        */

function snapshotLive(page) {
  const values = new Map();
  page.querySelectorAll('.stat, .stat-line').forEach((node, i) => {
    const label = (node.querySelector('.meta-label, .label') || {}).textContent || i;
    const value = (node.querySelector('.stat-value, .stat-none, .t-number') || {}).textContent;
    values.set(String(label).trim(), value);
  });
  const ids = new Set([...page.querySelectorAll('.row[data-id]')].map((r) => r.dataset.id));
  return { values, ids };
}

function animateLive(page, before) {
  page.querySelectorAll('.stat, .stat-line').forEach((node, i) => {
    const label = String((node.querySelector('.meta-label, .label') || {}).textContent || i).trim();
    const valueNode = node.querySelector('.stat-value, .stat-none, .t-number');
    if (valueNode && before.values.has(label) && before.values.get(label) !== valueNode.textContent) {
      valueNode.classList.add('changed');
    }
  });
  if (before.ids.size) {
    page.querySelectorAll('.row[data-id]').forEach((row) => {
      if (!before.ids.has(row.dataset.id)) row.classList.add('fresh');
    });
  }
}

/* ---- Keyboard (§6.2, §12, §13) ---------------------------------------- */

document.addEventListener('keydown', (event) => {
  const modalOpen = el('modal-scrim').classList.contains('open');
  const drawerOpen = el('drawer').classList.contains('open');

  // It's an app, not a page: no zoom from the keyboard (§13).
  if (event.ctrlKey && ['+', '-', '=', '0'].includes(event.key)) { event.preventDefault(); return; }

  if (event.key === 'Escape') {
    // Esc hides a tooltip; inside a dialog or drawer the same press also
    // closes it, so a focus tooltip never costs an extra keypress.
    const hadTip = !!tip.target;
    hideTip();
    if (hadTip && !modalOpen && !drawerOpen) return;
    const active = document.activeElement;
    if (active && active.matches && active.matches('[data-search]')) {
      // Esc clears, then blurs (§7 Inputs).
      if (active.value) { active.value = ''; active.dispatchEvent(new Event('input')); } else active.blur();
      return;
    }
    if (modalOpen) {
      if (document.getElementById('ab-caps')) return; // capture dialog handles its own keys
      if (sheetState) { el('modal-scrim').querySelector('[data-act="close-transform-sheet"]').click(); return; }
      closeModal();
      return;
    }
    if (drawerOpen) closeDrawer();
    return;
  }

  if (event.key === 'Tab') {
    if (modalOpen) trapFocus(event, el('modal-scrim').querySelector('.modal'));
    else if (drawerOpen) trapFocus(event, el('drawer'));
    return;
  }

  // Enter on a focused row or card opens it, like a click.
  if ((event.key === 'Enter' || event.key === ' ') && event.target.matches && event.target.matches('.row[data-id], .transform-card, [data-act][role="button"], .row[role="radio"]')) {
    event.preventDefault();
    event.target.click();
    return;
  }

  // Ctrl+Z triggers the toast's action while it is up (§11.2).
  if (event.ctrlKey && event.key.toLowerCase() === 'z' && toastState.action && toastState.action.label === 'Undo'
      && !(event.target.matches && event.target.matches('input, textarea'))) {
    event.preventDefault();
    runToastAction();
    return;
  }

  if (modalOpen || !el('onboard').classList.contains('hidden')) return;

  if (event.ctrlKey && !event.shiftKey && !event.altKey) {
    const nav = document.querySelector(`.nav-item[data-key="${event.key}"]`);
    if (nav) { event.preventDefault(); go(nav.dataset.view); return; }
    if (event.key === ',') { event.preventDefault(); go('settings'); return; }
    if (event.key.toLowerCase() === 'k') {
      const search = document.querySelector('[data-search]');
      event.preventDefault();
      if (search) search.focus();
      else go('history').then(() => el('search') && el('search').focus());
    }
  }
});

// No Ctrl+wheel zoom (§13). pywebview leaves WebView2's zoom control on.
document.addEventListener('wheel', (event) => { if (event.ctrlKey) event.preventDefault(); }, { passive: false });
document.addEventListener('dragstart', (event) => {
  if (!(event.target.closest && event.target.closest('input, textarea'))) event.preventDefault();
});

/* --------------------------------------------------------------- routing */

function renderNav() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    const active = item.dataset.view === state.view;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
    item.onclick = () => go(item.dataset.view);
    if (item.dataset.key) item.setAttribute('data-tip-keys', `Ctrl + ${item.dataset.key}`);
  });
  // Red counts only problems that screen can fix (§6.2).
  setBadge('settings-count', settingsProblemCount());
  setBadge('snippets-count', countConflicts());
  setBadge('transforms-count', countTransformConflicts());
}

function countTransformConflicts() {
  const transforms = (state.settings && state.settings.transforms) || [];
  return transforms.filter((t) => t.conflicts && t.conflicts.length).length;
}

function setBadge(id, count) {
  const badge = el(id);
  if (!badge) return;
  badge.textContent = count || '';
  badge.classList.toggle('show', count > 0);
}

function settingsProblemCount() {
  const s = state.settings;
  if (!s) return 0;
  return s.key && !s.key.configured ? 1 : 0;
}

/** Tab state lives in the URL hash so Back and reopening keep it (§6.2). */
function syncHash() {
  const tab = state.view === 'settings' ? state.settingsTab : state.view === 'insights' ? state.insightsTab : '';
  const hash = `#${state.view}${tab ? `/${tab}` : ''}`;
  if (location.hash !== hash) history.replaceState(null, '', hash);
}

/**
 * Split a location hash into a view and, for Settings and Insights, a tab.
 * The bar's microphone and language controls ask for "#settings/voice".
 * "#settings/apps" is where app rules used to live; they are on Style now.
 * Returns null when the hash names no view we have.
 */
function routeFor(hash) {
  const [view, tab] = (hash || '').replace('#', '').split('/');
  if (view === 'settings' && tab === 'apps') return 'style';
  if (!views[view]) return null;
  if (view === 'settings' && tab && views.settings.tabsFor().some(([id]) => id === tab)) state.settingsTab = tab;
  if (view === 'insights' && tab && views.insights.tabsFor().some(([id]) => id === tab)) state.insightsTab = tab;
  return view;
}

/** Slide the tab underline from where it was to the active tab (§6.2). */
const underlineMemory = {};
function placeUnderline(root, animate) {
  root.querySelectorAll('.tabs').forEach((tabs) => {
    const line = tabs.querySelector('.tab-underline');
    const active = tabs.querySelector('.tab.active');
    if (!line || !active) return;
    const key = state.view;
    const to = { x: active.offsetLeft, w: active.offsetWidth };
    const from = underlineMemory[key];
    if (animate && from) {
      line.style.transition = 'none';
      line.style.transform = `translateX(${from.x}px)`;
      line.style.width = `${from.w}px`;
      void line.offsetWidth; // commit the start position
      line.style.transition = '';
    } else {
      line.style.transition = 'none';
    }
    line.style.transform = `translateX(${to.x}px)`;
    line.style.width = `${to.w}px`;
    if (!animate || !from) { void line.offsetWidth; line.style.transition = ''; }
    underlineMemory[key] = to;
  });
}

/** Skeleton of the real layout while a page's data loads (§9.2). */
function skeletonFor(view) {
  const v = views[view];
  return `
    ${pageHead(esc(v.title))}
    ${v.tabsFor ? tabRow(v.tabsFor(), view === 'settings' ? state.settingsTab : state.insightsTab, 'data-tab-skeleton') : ''}
    <div class="page-body stack">
      <div class="grid cols-3"><div class="skeleton block"></div><div class="skeleton block"></div><div class="skeleton block"></div></div>
      <div class="skeleton tall"></div>
    </div>`;
}

let loadSeq = 0;

/**
 * Load and render the current view. Options:
 *  - keepFocus: selector to refocus (and keep the caret in) after render
 *  - keepScroll: keep the panel's scroll position (in-page updates)
 *  - quiet: no skeleton, no entrance animation (an in-place update)
 *  - entering: this is a page change (fade in + rise, §10.3)
 */
async function refresh(options = {}) {
  const view = views[state.view];
  const page = el('page');
  const main = el('main');
  const seq = ++loadSeq;
  const scroll = main.scrollTop;

  let skeletonShownAt = 0;
  const skeletonTimer = options.entering
    ? setTimeout(() => { if (seq === loadSeq) { page.innerHTML = skeletonFor(state.view); skeletonShownAt = Date.now(); placeUnderline(page, false); } }, 150)
    : null;

  try {
    await view.load();
  } catch (err) {
    clearTimeout(skeletonTimer);
    if (seq !== loadSeq) return;
    page.innerHTML = `${pageHead(esc(view.title))}<div class="page-body"><div class="banner">${icon('alert')}<span class="grow">${esc(err.message)}</span><button class="btn small" data-act="retry-load">Try again</button></div></div>`;
    return;
  }
  clearTimeout(skeletonTimer);
  if (seq !== loadSeq) return;
  // Once shown, a loading state stays at least 300ms so it doesn't flash.
  if (skeletonShownAt) {
    const wait = 300 - (Date.now() - skeletonShownAt);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    if (seq !== loadSeq) return;
  }

  const active = document.activeElement;
  const selectionStart = active && active.selectionStart;
  // Only an update of the same screen animates; switching tabs is not a change.
  const renderKey = `${state.view}/${state.view === 'insights' ? state.insightsTab + state.range : state.view === 'settings' ? state.settingsTab : ''}`;
  const before = options.quiet && page.dataset.renderKey === renderKey ? snapshotLive(page) : null;
  page.dataset.renderKey = renderKey;
  hideTip();
  closePopover({ restoreFocus: false });
  page.innerHTML = view.render();
  if (view.bind) view.bind(page);
  enhanceSelects(page);
  if (before) animateLive(page, before);
  renderNav();
  placeUnderline(page, !!options.quiet);
  page.classList.remove('entering');
  if (options.entering) { void page.offsetWidth; page.classList.add('entering'); }
  else if (skeletonShownAt) { page.classList.add('fade-in'); setTimeout(() => page.classList.remove('fade-in'), 160); }

  if (options.keepFocus) {
    const target = page.querySelector(options.keepFocus);
    if (target) {
      target.focus();
      if (selectionStart !== null && selectionStart !== undefined) {
        try { target.setSelectionRange(selectionStart, selectionStart); } catch { /* not a text input */ }
      }
    }
  }
  main.scrollTop = options.keepScroll || options.keepFocus ? scroll : 0;
  updateStuck();
}

async function go(view) {
  if (!views[view]) return;
  const changed = state.view !== view;
  state.view = view;
  closeDrawer();
  syncHash();
  await refresh({ entering: changed || !el('page').innerHTML });
}

document.addEventListener('click', (event) => {
  if (event.target.closest('[data-act="retry-load"]')) refresh({ entering: true });
});

/* Sticky tab row: the rule firms up once the panel scrolls (§5.1). */
function updateStuck() {
  const tabs = el('page').querySelector('.tabs');
  if (!tabs) return;
  tabs.classList.toggle('stuck', el('main').scrollTop > tabs.offsetTop - 1);
}
el('main').addEventListener('scroll', updateStuck, { passive: true });

/* ---- Sidebar: icon rail below 1180px, or when chosen (§5.6) ------------ */

function applySidebar() {
  const pref = prefs.get('sidebar', 'auto');
  const collapsed = pref === 'collapsed' || (pref === 'auto' && window.innerWidth < 1180);
  el('app').classList.toggle('collapsed', collapsed);
  const button = el('sidebar-toggle');
  const label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
  button.setAttribute('aria-label', label);
  button.setAttribute('data-tip', label);
  requestAnimationFrame(() => placeUnderline(el('page'), false));
}
el('sidebar-toggle').addEventListener('click', () => {
  const collapsed = el('app').classList.contains('collapsed');
  prefs.set('sidebar', collapsed ? 'open' : 'collapsed');
  applySidebar();
});
window.addEventListener('resize', applySidebar);
applySidebar();

/* --------------------------------------------------------------- hotkey */

/**
 * Hotkey capture. Only modifier keys are accepted for a hold-to-talk key: a
 * key that is also a character would type that character into whatever has
 * focus every time the user dictates.
 */
const MODIFIER_CODES = {
  ControlLeft: 'ctrl_l', ControlRight: 'ctrl_r',
  ShiftLeft: 'shift_l', ShiftRight: 'shift_r',
  AltLeft: 'alt_l', AltRight: 'alt_r',
  // Windows does not reliably distinguish left/right Win -- KNOWN_KEYS on
  // the Python side only has a single "cmd".
  MetaLeft: 'cmd', MetaRight: 'cmd',
  CapsLock: 'caps_lock', F13: 'f13',
};

/** Modifier identification for a *chord* capture: left and right count as
 * the one modifier, since "which side" is not part of what a chord means. */
const CHORD_MODIFIER_CODES = {
  ControlLeft: 'ctrl', ControlRight: 'ctrl',
  ShiftLeft: 'shift', ShiftRight: 'shift',
  AltLeft: 'alt', AltRight: 'alt',
  MetaLeft: 'cmd', MetaRight: 'cmd',
};

const CHORD_NAMED_KEYS = {
  ' ': 'space', Enter: 'enter', Tab: 'tab', Backspace: 'backspace',
  Delete: 'delete', Home: 'home', End: 'end', Insert: 'insert',
};

/** Mirrors flow.hotkey._RESERVED_CHORDS -- checked here too so a doomed
 * combination is refused before the round trip. */
const RESERVED_CHORDS = {
  'ctrl+c': 'used internally to read a selection. Pick something else',
  'ctrl+v': 'used internally to paste. Pick something else',
  'ctrl+x': 'used internally to read a selection. Pick something else',
  'ctrl+alt+delete': "reserved by Windows and can't be used here",
  'cmd+l': "reserved by Windows (locks the screen) and can't be used here",
};

const CHORD_MODIFIER_ORDER = ['ctrl', 'alt', 'shift', 'cmd'];

function chordPartLabel(part) {
  const labels = { ctrl: 'Ctrl', alt: 'Alt', shift: 'Shift', cmd: 'Win' };
  if (labels[part]) return labels[part];
  return part.length === 1 ? part.toUpperCase() : part.charAt(0).toUpperCase() + part.slice(1);
}

const chordLabel = (chord) => chord.split('+').map(chordPartLabel).join(' + ');

/** The terminal (non-modifier) key of a chord, from event.key -- which
 * reflects the active keyboard layout, matching pynput server-side. */
function chordTerminalKeyName(event) {
  const key = event.key;
  if (key.length === 1 && /[a-zA-Z0-9]/.test(key)) return key.toLowerCase();
  if (CHORD_NAMED_KEYS[key]) return CHORD_NAMED_KEYS[key];
  const fMatch = /^F([1-9]|1[0-9]|20)$/.exec(key);
  return fMatch ? `f${fMatch[1]}` : null;
}

/** Mouse buttons that may be bound. Left (0) and right (2) are deliberately
 * absent -- see sway.hotkey.MOUSE_BUTTONS. */
const MOUSE_BUTTON_NAMES = { 1: 'middle', 3: 'back', 4: 'forward' };
const MOUSE_LABELS = { middle: 'Middle Click', back: 'Back Button', forward: 'Forward Button' };

/** A raw binding spec as key-cap labels, for a capture that has not
 * round-tripped through the server yet. */
function specLabels(spec) {
  const SIDED = ['ctrl_r', 'ctrl_l', 'alt_r', 'alt_l', 'shift_r', 'shift_l', 'cmd', 'caps_lock', 'f13'];
  return String(spec).split('+').map((p) => {
    if (p.startsWith('mouse:')) return MOUSE_LABELS[p.slice(6)] || p;
    if (SIDED.includes(p)) return prettyKey(p);
    return chordPartLabel(p);
  });
}

function specChips(spec, cls) {
  return specLabels(spec).map((label) => `<span class="cap big ${cls}">${esc(label)}</span>`).join('');
}

/** What kind of binding a spec is, in the words the capture dialog uses. */
function specShape(spec) {
  const parts = String(spec).split('+');
  if (parts.some((p) => p.startsWith('mouse:'))) return parts.length > 1 ? 'Mouse shortcut' : 'Mouse button';
  if (parts.length === 1) return 'Hold key';
  if (parts.every((p) => CHORD_MODIFIER_ORDER.includes(p))) return 'Combo · hold together';
  return 'Shortcut · press together';
}

/** Another binding already using this exact gesture, or null. Compared as
 * part sets so "alt+ctrl" and "ctrl+alt" are the same; the server stays
 * the authority (it reports conflicts after saving too). */
function findBindingConflict(spec, exclude) {
  const key = (v) => String(v || '').toLowerCase().split('+').sort().join('+');
  const wanted = key(spec);
  const engine = (state.settings && state.settings.engine) || {};
  const live = (engine.bindings || []).map((b) => ({ value: b.value, label: b.label }));
  const activation = (state.settings && state.settings.activation) || {};
  const NAMES = { dictation: 'Dictation hotkey', command: 'Voice command hotkey', hands_free: 'Hands-free dictation' };
  for (const [target, a] of Object.entries(activation)) {
    for (const b of (a && a.bindings) || []) live.push({ value: b.value, label: NAMES[target] || target });
  }
  for (const t of (state.settings && state.settings.transforms) || []) {
    for (const v of t.shortcuts || []) live.push({ value: v, label: `“${t.name}”` });
  }
  const hit = live.find((b) => key(b.value) === wanted && key(b.value) !== key(exclude));
  return hit ? hit.label : null;
}

/**
 * One capture dialog for every binding, activation or transform: a bare
 * held key, two or more modifiers held together (a combo), a modifier
 * chord ending in a key, a bare mouse button, or a chord ending in a mouse
 * button. The server validates and canonicalises the spec
 * (sway.hotkey.parse_binding); this reports the gesture and makes pressing
 * it feel physical:
 *
 *   listening  a breathing ghost key and a pulsing status
 *   holding    the stage tints amber; each key pops in as it goes down
 *   captured   on release the caps settle with a small bounce, the shape is
 *              named, and an existing user of the same gesture is called out
 *   error      one shake, the reason in red ink
 *
 * Enter saves, Backspace starts over, Esc cancels.
 */
function captureBindingDialog({ title, hint, onCaptured, onCancel, current }) {
  let captured = null;
  const heldGeneric = new Set();
  let physicalCount = 0;
  let locked = false; // a terminal (key or mouse) has fired: the capture is final
  let saved = false;
  let shownLabels = [];

  openModal({
    title,
    body: `
      <div class="capture-stage" id="ab-stage" data-state="listening">
        <div id="ab-caps" class="capture-box"><span class="cap big ghost"></span></div>
        <div class="capture-status" id="ab-status"><span class="live-dot"></span><span>Listening for keys…</span></div>
      </div>
      <div class="capture-meta" id="ab-meta"></div>
      <div class="helper" id="ab-help">${esc(hint)}</div>
      <div class="kbd-hints">
        <span><span class="cap">Enter</span> Save</span>
        <span><span class="cap">Backspace</span> Start over</span>
        <span><span class="cap">Esc</span> Cancel</span>
      </div>`,
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="close-modal">Cancel</button>
      <button class="btn primary" id="ab-save" disabled data-tip="Press a key first">Save shortcut</button>`,
    focus: '#ab-save',
  });

  const stage = document.getElementById('ab-stage');
  const capsBox = document.getElementById('ab-caps');
  const status = document.getElementById('ab-status');
  const meta = document.getElementById('ab-meta');
  const save = document.getElementById('ab-save');

  const setStatus = (html) => { status.innerHTML = html; };
  const setError = (message) => {
    const help = document.getElementById('ab-help');
    help.className = message ? 'helper error' : 'helper';
    help.innerHTML = message ? `${icon('alert')}<span>${esc(message)}</span>` : esc(hint);
    if (message) {
      stage.dataset.state = 'error';
      capsBox.classList.remove('conflict'); void capsBox.offsetWidth; capsBox.classList.add('conflict');
    }
  };
  const setSave = (enabled, label = 'Save shortcut') => {
    save.disabled = !enabled;
    save.textContent = label;
    if (enabled) save.removeAttribute('data-tip'); else save.setAttribute('data-tip', 'Press a key first');
  };

  // A capture that never settles should not leave the dialog silently stuck.
  let timeout = setTimeout(() => {
    if (!captured) setError("Couldn't detect a steady combination. Try again, or pick a different key.");
  }, 6000);

  /** Update caps in place: keys already shown keep their element (so their
   * transitions run), new keys pop in, and "+" joins them. */
  const drawCaps = (labels, cls) => {
    const same = labels.length === shownLabels.length && labels.every((l, i) => l === shownLabels[i]);
    if (!same) {
      const prefix = shownLabels.findIndex((l, i) => l !== labels[i]);
      const kept = prefix === -1 ? Math.min(shownLabels.length, labels.length) : prefix;
      capsBox.innerHTML = labels.map((l, i) =>
        `${i ? '<span class="capture-plus">+</span>' : ''}<span class="cap big ${cls} ${i >= kept ? 'entering' : ''}">${esc(l)}</span>`).join('');
      shownLabels = labels;
    } else {
      capsBox.querySelectorAll('.cap').forEach((c) => { c.classList.toggle('held', cls === 'held'); c.classList.toggle('hot', cls !== 'held'); });
    }
  };

  const reset = () => {
    captured = null; locked = false; physicalCount = 0; heldGeneric.clear(); shownLabels = [];
    capsBox.innerHTML = '<span class="cap big ghost entering"></span>';
    stage.dataset.state = 'listening';
    stage.classList.remove('settle');
    meta.innerHTML = '';
    setError('');
    setStatus('<span class="live-dot"></span><span>Listening for keys…</span>');
    setSave(false);
    clearTimeout(timeout);
    timeout = setTimeout(() => { if (!captured) setError("Couldn't detect a steady combination. Try again, or pick a different key."); }, 6000);
  };

  const render = () => {
    if (!captured) return;
    const holding = physicalCount > 0;
    drawCaps(specLabels(captured), holding ? 'held' : 'hot');
    if (holding) {
      stage.dataset.state = 'holding';
      stage.classList.remove('settle');
      setStatus(`<span class="live-dot"></span><span>${locked ? 'Got it. Let go to set it' : 'Keep holding, or add a key'}</span>`);
      return;
    }
    // Released: the gesture is set.
    const conflict = findBindingConflict(captured, current);
    if (stage.dataset.state !== 'captured' && stage.dataset.state !== 'conflict') {
      stage.classList.remove('settle'); void stage.offsetWidth; stage.classList.add('settle');
    }
    stage.dataset.state = conflict ? 'conflict' : 'captured';
    const win = captured.split('+').includes('cmd');
    meta.innerHTML = `
      <span class="badge brand">${esc(specShape(captured))}</span>
      ${conflict ? `<span class="helper error">${icon('alert')}<span>Already used by ${esc(conflict)}</span></span>` : ''}
      ${win && !conflict ? `<span class="helper" data-tip="Windows sometimes intercepts Win-key shortcuts before they reach the app you're using.">${icon('info')}<span>Win-key shortcuts can be less reliable</span></span>` : ''}`;
    if (conflict) {
      capsBox.classList.remove('conflict'); void capsBox.offsetWidth; capsBox.classList.add('conflict');
      setStatus(`${icon('alert')}<span>Pick another, or save it anyway</span>`);
      setSave(true, 'Save anyway');
    } else {
      setStatus(`${icon('check')}<span>Ready to save</span>`);
      setSave(true);
    }
  };

  const onKeyDown = (event) => {
    if (event.key === 'Escape' && !heldGeneric.size) { cleanupAll(); closeModal(); if (onCancel) onCancel(); return; }
    if (event.key === 'Tab') return;
    if (!heldGeneric.size && captured && physicalCount === 0) {
      if (event.key === 'Enter') { event.preventDefault(); save.click(); return; }
      if (event.key === 'Backspace') { event.preventDefault(); reset(); return; }
    }
    if (locked) { event.preventDefault(); return; }
    const mod = CHORD_MODIFIER_CODES[event.code];
    if (mod) {
      event.preventDefault();
      if (event.repeat) return;
      const wasHeld = heldGeneric.has(mod);
      heldGeneric.add(mod);
      if (!wasHeld) physicalCount += 1;
      // One modifier alone captures as that sided key; two or more as a
      // modifier-only combo. Recomputed on every press.
      captured = heldGeneric.size === 1 ? MODIFIER_CODES[event.code] : CHORD_MODIFIER_ORDER.filter((m) => heldGeneric.has(m)).join('+');
      setError('');
      render();
      return;
    }
    const key = chordTerminalKeyName(event);
    if (!key) return;
    event.preventDefault();
    if (heldGeneric.size === 0) {
      setError('A shortcut needs at least one modifier, like Ctrl+Alt+P. Or hold a single key on its own.');
      return;
    }
    const order = CHORD_MODIFIER_ORDER.filter((m) => heldGeneric.has(m));
    const spec = [...order, key].join('+');
    if (RESERVED_CHORDS[spec]) {
      setError(`${chordLabel(spec)} is ${RESERVED_CHORDS[spec]}.`);
      return;
    }
    setError('');
    captured = spec;
    locked = true;
    physicalCount += 1;
    render();
  };

  const onKeyUp = (event) => {
    const mod = CHORD_MODIFIER_CODES[event.code];
    if (mod) {
      if (heldGeneric.delete(mod)) physicalCount = Math.max(0, physicalCount - 1);
    } else if (locked && chordTerminalKeyName(event)) {
      physicalCount = Math.max(0, physicalCount - 1);
    } else {
      return;
    }
    // Every key up while locked releases the terminal too, so the stage
    // settles once the user has let go of everything.
    if (!heldGeneric.size) physicalCount = 0;
    render();
  };

  const onMouseDown = (event) => {
    const name = MOUSE_BUTTON_NAMES[event.button];
    if (!name || locked) return;
    event.preventDefault(); // stops the browser's own back/forward/autoscroll
    const order = CHORD_MODIFIER_ORDER.filter((m) => heldGeneric.has(m));
    captured = order.length ? [...order, `mouse:${name}`].join('+') : `mouse:${name}`;
    locked = true;
    physicalCount += 1;
    setError('');
    render();
  };

  const onMouseUp = (event) => {
    const name = MOUSE_BUTTON_NAMES[event.button];
    if (!name) return;
    physicalCount = Math.max(0, physicalCount - 1);
    if (!heldGeneric.size) physicalCount = 0;
    render();
  };

  document.addEventListener('keydown', onKeyDown, true);
  document.addEventListener('keyup', onKeyUp, true);
  document.addEventListener('mousedown', onMouseDown, true);
  document.addEventListener('mouseup', onMouseUp, true);

  const cleanup = () => {
    document.removeEventListener('keydown', onKeyDown, true);
    document.removeEventListener('keyup', onKeyUp, true);
    document.removeEventListener('mousedown', onMouseDown, true);
    document.removeEventListener('mouseup', onMouseUp, true);
    clearTimeout(timeout);
  };
  // Torn down whichever way the dialog closes, so the capture handlers can
  // never outlive it and swallow keystrokes on the page behind.
  const scrim = document.getElementById('modal-scrim');
  const observer = new MutationObserver(() => {
    if (!scrim.classList.contains('open') || !document.getElementById('ab-caps')) {
      cleanup(); observer.disconnect();
      if (!saved && onCancel && !scrim.classList.contains('open')) onCancel();
    }
  });
  const cleanupAll = () => { cleanup(); observer.disconnect(); };
  observer.observe(scrim, { attributes: true, attributeFilter: ['class'], childList: true, subtree: true });

  save.onclick = () => {
    if (!captured) return;
    saved = true;
    cleanupAll();
    closeModal();
    onCaptured(captured);
  };
}

/** After a binding is saved, its new chip pops in and its row flashes, so
 * the user sees exactly where it went (§7 Lists). */
function celebrateBindings(target, before) {
  const a = state.settings && state.settings.activation && state.settings.activation[target];
  const now = ((a && a.bindings) || []).map((b) => b.value);
  const fresh = now.filter((v) => !before.includes(v));
  for (const value of fresh) {
    const chip = document.querySelector(`[data-live-binding="${CSS.escape(target)}"][data-binding-value="${CSS.escape(value)}"]`);
    if (!chip) continue;
    chip.querySelectorAll('.cap').forEach((c) => c.classList.add('entering'));
    const row = chip.closest('.setting');
    if (row) { row.classList.remove('fresh'); void row.offsetWidth; row.classList.add('fresh'); }
  }
}

const bindingValues = (target) => {
  const a = state.settings && state.settings.activation && state.settings.activation[target];
  return ((a && a.bindings) || []).map((b) => b.value);
};

/** Onboarding still only ever rebinds the dictation hold key. */
function hotkeyDialog() {
  const current = primaryBinding('dictation');
  captureBindingDialog({
    title: 'Press the key you want to hold',
    hint: 'Hold-to-talk needs a key you never type with: a Ctrl, Shift, Alt or Win key. '
      + 'Right-hand keys work best, since your left hand keeps using shortcuts.',
    current: current && current.value,
    onCaptured: async (spec) => {
      try {
        state.settings = current
          ? await post('/api/shortcuts/save', { target: 'dictation', op: 'replace', old_binding: current.value, binding: spec })
          : await post('/api/shortcuts/save', { target: 'dictation', op: 'add', binding: spec });
        if (!el('onboard').classList.contains('hidden')) await drawOnboarding();
        else await refresh({ keepScroll: true, quiet: true });
        toast('Hotkey saved');
      } catch (err) {
        toast(err.message);
      }
    },
  });
}

/* ----------------------------------------------------------- onboarding */

/*
 * Five phases (§6.3). The rail's continuous underline carries progress; the
 * phase labels do not light up individually. One primary action per step,
 * bottom-right; Back is ghost, bottom-left; Enter triggers the primary.
 */
const PHASES = ['Welcome', 'Permissions', 'API key', 'Set up', 'Try it'];

const onboarding = { step: 0, deviceOk: false, keyOk: false, tried: false };

function renderRail() {
  const parts = [];
  PHASES.forEach((phase, index) => {
    if (index) parts.push('<span class="rail-sep">›</span>');
    parts.push(`<span class="rail-step ${index <= onboarding.step ? 'done' : ''}">${esc(phase)}</span>`);
  });
  const pct = ((onboarding.step + 1) / PHASES.length) * 100;
  document.getElementById('rail').innerHTML =
    parts.join('') + `<div class="rail-progress" style="width:${pct.toFixed(1)}%"></div>`;
}

function onboardScreen({ title, sub, card: inner, primary, secondary = '', back = true }) {
  return `
    <div class="onboard-inner fade-in">
      <h1>${title}</h1>
      ${sub ? `<p class="sub">${sub}</p>` : ''}
      ${inner ? `<div class="onboard-card">${inner}</div>` : ''}
      <div class="onboard-foot">
        ${back && onboarding.step > 0
          ? `<button class="btn ghost large" data-act="ob-back">${icon('back')} Back</button>` : ''}
        <div class="spacer"></div>
        ${secondary}
        ${primary}
      </div>
    </div>`;
}

async function drawOnboarding() {
  renderRail();
  const body = document.getElementById('onboard-body');
  const step = onboarding.step;

  if (step === 0) {
    body.innerHTML = onboardScreen({
      title: 'Talk. It types.',
      sub: 'Hold one key, say what you mean, let go. Cleaned-up text appears wherever your cursor is, in any application.',
      card: `
        <div class="stack">
          <div class="t-card">Three things to set up</div>
          <div class="feature">${'<span class="feature-icon">' + icon('voice') + '</span>'}<div><div class="medium">Your microphone</div><div class="small muted">Sway listens only while you hold your key.</div></div></div>
          <div class="feature">${'<span class="feature-icon">' + icon('globe') + '</span>'}<div><div class="medium">A Groq API key</div><div class="small muted">Free, and stored in Windows Credential Manager.</div></div></div>
          <div class="feature">${'<span class="feature-icon">' + icon('edit') + '</span>'}<div><div class="medium">The key you hold</div><div class="small muted">About a minute in all. Sway won’t ask again.</div></div></div>
        </div>`,
      primary: '<button class="btn primary large" data-act="ob-next">Get started</button>',
      back: false,
    });
    return;
  }

  if (step === 1) {
    await drawPermissions();
    return;
  }

  if (step === 2) {
    const key = (state.settings && state.settings.key) || {};
    onboarding.keyOk = !!key.configured;
    body.innerHTML = onboardScreen({
      title: 'Connect Sway to Groq',
      sub: 'Sway uses Groq to transcribe. Keys are free, and yours is stored in Windows Credential Manager, never in a file Sway writes.',
      card: key.configured
        ? `<div class="status-card">
             <span class="dot ok"></span>
             <div class="grow">
               <div class="status-head">Key saved</div>
               <div class="small muted mono">${esc(key.masked)}</div>
             </div>
             <button class="btn small" data-act="ob-test">Test connection</button>
             <button class="btn small" data-act="ob-key">Change…</button>
           </div>
           <div id="key-result" class="mt-3"></div>`
        : field('ob-key-input', 'Paste your key',
          '<input class="input mono" id="ob-key-input" type="password" placeholder="gsk_…" autocomplete="off" spellcheck="false">',
          'Get one at console.groq.com. It takes about 30 seconds.'),
      primary: key.configured
        ? '<button class="btn primary large" data-act="ob-next">Continue</button>'
        : '<button class="btn primary large" data-act="ob-save-key">Save and continue</button>',
    });
    const input = document.getElementById('ob-key-input');
    if (input) input.focus();
    return;
  }

  if (step === 3) {
    const binding = primaryBinding('dictation');
    const value = binding ? binding.value : 'ctrl_r';
    body.innerHTML = onboardScreen({
      title: 'Choose your key',
      sub: 'Hold it while you speak. It should be a key you never type with.',
      card: `
        <div class="t-card">Does it light up when you hold it?</div>
        <div class="well centered mt-4">
          <div id="ob-caps" class="capture-box">${specChips(value, 'hot')}</div>
        </div>
        <p class="small muted mt-4">Press and hold it now. The key turns solid amber while Sway is receiving it.</p>`,
      secondary: `<button class="btn large" data-act="ob-rebind">${icon('edit')} Use a different key…</button>`,
      primary: '<button class="btn primary large" data-act="ob-next">It works</button>',
    });
    watchHotkeyPreview(value);
    return;
  }

  // Step 4: the live try-it-now box. A neutral card rather than another
  // product's chrome: rendering someone else's UI here would be impersonation.
  const binding = primaryBinding('dictation');
  body.innerHTML = onboardScreen({
    title: 'Try it right here',
    sub: 'Click the box, then hold your key and say a sentence. This is the real thing: the same pipeline you will use everywhere else.',
    card: `
      <div class="inline wrap small muted">Hold ${binding ? capsFromLabel(binding.label) : 'your key'} · say something · let go</div>
      <textarea class="input mt-4" id="ob-try" rows="5" placeholder="Your words will appear here…"></textarea>
      <div id="ob-try-state" class="small muted mt-3">Waiting for you…</div>`,
    secondary: '<button class="btn ghost large" data-act="ob-finish">Skip</button>',
    primary: '<button class="btn primary large" data-act="ob-finish">Finish</button>',
  });
  const box = document.getElementById('ob-try');
  if (box) {
    box.focus();
    box.addEventListener('input', () => {
      onboarding.tried = true;
      const words = box.value.trim().split(/\s+/).filter(Boolean).length;
      document.getElementById('ob-try-state').textContent =
        words ? `${fmt.plural(words, 'word')}. That is Sway working.` : 'Waiting for you…';
    });
  }
}

/**
 * The permissions screen. Windows has no permission to grant, so this does
 * not claim to grant one: it opens the stream, shows the level, and asks
 * the user to observe.
 */
async function drawPermissions() {
  const body = document.getElementById('onboard-body');
  let devices = [];
  try {
    const result = await api('/api/devices');
    devices = result.devices || [];
  } catch { /* reported as zero below */ }

  onboarding.deviceOk = devices.length > 0;

  if (!devices.length) {
    // Genuinely zero, so the zero is printed (§9.1).
    body.innerHTML = onboardScreen({
      title: 'Sway can’t hear anything yet',
      card: `
        ${stat({ label: 'Microphones found', value: '0', foot: 'Windows is not reporting any input device.' })}
        <p class="small muted mt-4">
          Plug a microphone in, or turn on microphone access for desktop apps in
          Windows privacy settings, then check again.
        </p>`,
      secondary: '<button class="btn ghost large" data-act="ob-next">Skip for now</button>',
      primary: '<button class="btn primary large" data-act="ob-recheck">Check again</button>',
    });
    return;
  }

  const defaultDevice = devices.find((d) => d.default) || devices[0];
  body.innerHTML = onboardScreen({
    title: 'Let Sway hear you',
    sub: 'Sway listens only while you hold your key. Nothing is recorded before that, and audio is never stored.',
    card: `
      <div class="t-card">Say something. Do the bars move?</div>
      <div class="well centered mt-4 stack-3">
        <div id="ob-level">${levelMeter(0)}</div>
        <div class="small muted">${esc(defaultDevice.name)}</div>
      </div>
      <p class="small muted mt-4">
        ${fmt.plural(devices.length, 'microphone')} available.
        You can change which one Sway uses in Settings.
      </p>`,
    secondary: '<button class="btn large" data-act="ob-next">No, skip this</button>',
    primary: '<button class="btn primary large" data-act="ob-next">Yes, they move</button>',
  });
  watchLevel();
}

/**
 * Poll the engine's live mic level while a screen is showing a meter.
 * Cancelled whenever the onboarding step changes.
 */
let levelTimer = null;
function stopWatchers() {
  if (levelTimer) { clearInterval(levelTimer); levelTimer = null; }
}

function watchLevel() {
  stopWatchers();
  levelTimer = setInterval(async () => {
    const target = document.getElementById('ob-level');
    if (!target) { stopWatchers(); return; }
    try {
      const status = await api('/api/status');
      target.innerHTML = levelMeter(status.level || 0);
    } catch { /* transient; the next tick retries */ }
  }, 220);
}

function watchHotkeyPreview(bindingValue) {
  stopWatchers();
  levelTimer = setInterval(async () => {
    const target = document.getElementById('ob-caps');
    if (!target) { stopWatchers(); return; }
    try {
      const status = await api('/api/status');
      const held = status.hotkey_down === true;
      target.innerHTML = specChips(bindingValue, held ? 'held' : 'hot');
    } catch { /* transient */ }
  }, 140);
}

/**
 * Settings → Voice's live "is this binding physically active right now"
 * preview, for every configurable activation binding at once.
 * Self-terminating, checked each tick.
 */
let activationTimer = null;
function watchActivationPreview() {
  if (activationTimer) return;
  activationTimer = setInterval(async () => {
    if (state.view !== 'settings' || state.settingsTab !== 'voice') {
      clearInterval(activationTimer);
      activationTimer = null;
      return;
    }
    try {
      const status = await api('/api/status');
      const bindings = status.bindings_down || {};
      document.querySelectorAll('[data-live-binding]').forEach((node) => {
        const key = node.dataset.liveBinding;
        // Hands-free is tap-based: nothing is "held" to show on its chip.
        if (key === 'hands_free' || key === 'hands_free_chord') return;
        const down = key === 'dictation' ? status.hotkey_down === true : !!bindings[key];
        node.querySelectorAll('.cap').forEach((c) => {
          c.classList.toggle('held', down);
          c.classList.toggle('hot', !down);
        });
      });
      const dot = document.querySelector('[data-live-hands-free]');
      if (dot) dot.classList.toggle('hands-free-dot', status.hands_free_active === true);
    } catch { /* transient; the next tick retries */ }
  }, 200);
}

async function onboardingAction(act, target) {
  switch (act) {
    case 'ob-next':
      stopWatchers();
      onboarding.step = Math.min(PHASES.length - 1, onboarding.step + 1);
      await drawOnboarding();
      break;
    case 'ob-back':
      stopWatchers();
      onboarding.step = Math.max(0, onboarding.step - 1);
      await drawOnboarding();
      break;
    case 'ob-recheck':
      await drawOnboarding();
      break;
    case 'ob-save-key': {
      const input = document.getElementById('ob-key-input');
      const value = (input.value || '').trim();
      if (!value) { fieldError('ob-key-input', 'Paste a key first.'); input.focus(); return; }
      try {
        await busy(target, { doing: 'Checking…' }, () => post('/api/key', { key: value }));
        state.settings = await api('/api/settings');
        onboarding.step += 1;
        await drawOnboarding();
      } catch (err) { fieldError('ob-key-input', `Groq rejected that key. ${err.message}`); }
      break;
    }
    case 'ob-key': keyDialog(); break;
    case 'ob-test': await testKey(target); break;
    case 'ob-rebind': hotkeyDialog(); break;
    case 'ob-finish':
      stopWatchers();
      try {
        await post('/api/settings', { onboarding_complete: true });
      } catch { /* a failed write must not trap the user in onboarding */ }
      document.getElementById('onboard').classList.add('hidden');
      state.settings = null;
      await go('home');
      break;
    default:
      return false;
  }
  return true;
}

// Enter triggers the step's primary action when it is enabled (§6.3).
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || el('onboard').classList.contains('hidden')) return;
  if (el('modal-scrim').classList.contains('open')) return;
  if (event.target.matches && event.target.matches('textarea, button')) return;
  const primary = el('onboard-body').querySelector('.btn.primary:not([disabled])');
  if (primary) { event.preventDefault(); primary.click(); }
});

function startOnboarding() {
  document.getElementById('onboard').classList.remove('hidden');
  onboarding.step = 0;
  drawOnboarding();
}

/* ---------------------------------------------------------------- start */

(async function start() {
  let settings = null;
  try {
    settings = await api('/api/settings');
    state.settings = settings;
  } catch { /* the engine may still be coming up; Home will report it */ }

  const done = settings && settings.settings && settings.settings.onboarding_complete;
  if (settings && settings.editable && !done) {
    startOnboarding();
    return;
  }
  // The tray's Settings item opens the window on #settings rather than as a
  // second surface, so configuration lives in exactly one place.
  await go(routeFor(location.hash) || 'home');

  // A later navigation (the tray item, or the bar's mic button, on an
  // already-open window) has to be honoured too.
  window.addEventListener('hashchange', () => {
    const next = routeFor(location.hash);
    if (next) go(next);
  });

  // Live updates without a manual refresh. Quiet: nothing moves unless the
  // data changed, and the user's scroll and hover are kept.
  setInterval(() => {
    if (state.view === 'home' && !el('modal-scrim').classList.contains('open')
        && !el('drawer').classList.contains('open') && !document.querySelector('.row:hover')) {
      refresh({ keepScroll: true, quiet: true });
    }
  }, 5000);
}());
