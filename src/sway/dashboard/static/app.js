/*
 * Sway — the desktop UI.
 *
 * Structure mirrors docs/ui-reference-analysis.md: a flat sidebar (§2), one
 * page per destination, tabs for sub-navigation, and the component vocabulary
 * from §4 built once at the top and reused everywhere.
 *
 * Two rules that matter more than style here:
 *
 *  - Every string that came from the user, from a transcript, or from Groq is
 *    escaped before it reaches innerHTML. Transcripts are arbitrary text the
 *    app pastes into other applications; treating them as markup would be a
 *    self-inflicted XSS in a window that can also read the user's history.
 *  - The token never leaves this file's fetch calls. It is not put in the
 *    address bar, not logged, and not rendered.
 */

'use strict';

/* ------------------------------------------------------------------ api */

// Taken from the URL once, then scrubbed from the address bar so a screenshot
// or a shoulder-surfer never captures it (§ "nothing mentions tokens").
const TOKEN = new URLSearchParams(location.search).get('token') || '';
if (location.search) history.replaceState(null, '', location.pathname);

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

const DASH = '—';

const fmt = {
  int: (n) => (n === null || n === undefined ? DASH : Number(n).toLocaleString()),
  dec: (n, d = 0) => (n === null || n === undefined ? DASH : Number(n).toFixed(d)),
  ms: (n) => (n === null || n === undefined ? DASH : `${Math.round(n)} ms`),
  pct: (n, d = 0) => (n === null || n === undefined ? DASH : `${(n * 100).toFixed(d)}%`),
  clock: (epoch) =>
    new Date(epoch * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }),
  date: (epoch) =>
    new Date(epoch * 1000).toLocaleDateString([], { day: 'numeric', month: 'short', year: 'numeric' }),
  app: (name) => (name ? name.replace(/\.exe$/i, '') : 'Unknown app'),
  duration(seconds) {
    if (seconds === null || seconds === undefined) return DASH;
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  },
  countdown(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
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
  `<svg viewBox="0 0 20 20" class="${cls}"><use href="#i-${name}"/></svg>`;

const iconBtn = (name, label, attrs = '') =>
  `<button class="icon-btn" aria-label="${esc(label)}" title="${esc(label)}" ${attrs}>${icon(name)}</button>`;

/**
 * A statistic. `value === null` renders an em dash, never a zero: an
 * unmeasured value and a measured zero are different claims (§6). A real zero
 * is passed as 0 and prints as "0".
 */
function stat({ label, value, foot, estimate, serif }) {
  const shown = value === null || value === undefined
    ? '<div class="stat-none">—</div>'
    : `<div class="stat-value${serif ? ' serif' : ''}">${esc(value)}</div>`;
  return `
    <div>
      ${shown}
      <div class="meta-label" style="margin-top:6px">${esc(label)}</div>
      ${foot ? `<div class="small muted" style="margin-top:7px">${esc(foot)}</div>` : ''}
      ${estimate ? '<div class="small muted" style="margin-top:4px">Estimate</div>' : ''}
    </div>`;
}

/**
 * The threshold state (§6 strategy 3): show the shape and the exact remaining
 * cost. Deliberately without the reference's blur — blurring invented content
 * is still inventing it.
 */
function locked({ label, need, have }) {
  const left = Math.max(0, need - have);
  const pct = need > 0 ? Math.min(100, (have / need) * 100) : 0;
  return `
    <div class="locked">
      <div class="stat-none">—</div>
      <div class="meta-label" style="margin-top:6px">${esc(label)}</div>
      <div class="bar" style="margin-top:12px"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="small muted" style="margin-top:8px">Needs ${need} · ${left} to go</div>
    </div>`;
}

const card = (inner, style = '') => `<div class="card" ${style ? `style="${style}"` : ''}>${inner}</div>`;

/** A teaching empty state (§6 strategy 1): worked examples, not placeholders. */
function hero({ title, body, chips = [], action }) {
  return `
    <div class="hero">
      <h2>${esc(title)}</h2>
      <p>${esc(body)}</p>
      <div class="chips">
        ${action ? `<button class="chip action" data-act="${esc(action.act)}">${esc(action.label)}</button>` : ''}
        ${chips.join('')}
      </div>
    </div>`;
}

/** Plain centred text — for a filter that matched nothing (§6 strategy 4). */
const nothingFound = (text) => `<div class="empty">${esc(text)}</div>`;

const toggle = (checked, attrs = '') =>
  `<button class="toggle" role="switch" aria-checked="${checked ? 'true' : 'false'}" ${attrs}></button>`;

/** Key caps (§4). `hot` = "this is your hotkey" (amber, §5). */
function caps(spec, cls = 'hot') {
  const parts = String(spec || '').split('+').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return '';
  return `<span class="cap-row">${parts.map((p) => `<span class="cap ${cls}">${esc(prettyKey(p))}</span>`).join('')}</span>`;
}

/**
 * Key caps from a binding's already-formatted label (e.g. "Ctrl + Alt + P",
 * "Middle Click") -- what every activation and transform binding reaches
 * the dashboard as now (see sway.hotkey.Binding.pretty). Splits on " + "
 * rather than caps()'s "+", since the server has already done the parsing
 * and naming (mouse buttons, modifier combos, sided key names) that caps()
 * cannot.
 */
function capsFromLabel(label, cls = 'hot') {
  const parts = String(label || '').split(' + ').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return '';
  return `<span class="cap-row">${parts.map((p) => `<span class="cap ${cls}">${esc(p)}</span>`).join('')}</span>`;
}

/** The first (primary) effective dictation binding, or null if none is set
 * yet -- {value, label, shape}. Used wherever the UI needs to say "your
 * hotkey" outside the Shortcuts settings page itself (Home, onboarding). */
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

/* ------------------------------------------------------------- app state */

const state = {
  view: 'home',
  home: null,
  activity: null,
  insights: null,
  performance: null,
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

/* ------------------------------------------------------------------ chrome */

let toastTimer;
function toast(message, undo) {
  const node = el('toast');
  node.innerHTML = `<span>${esc(message)}</span>` + (undo ? '<button id="toast-undo">Undo</button>' : '');
  node.classList.add('show');
  clearTimeout(toastTimer);
  // A destructive action gets longer to be undone than a "Copied" gets to be
  // read; both eventually go away on their own.
  toastTimer = setTimeout(() => node.classList.remove('show'), undo ? 7000 : 2200);
  if (undo) {
    el('toast-undo').onclick = async () => {
      node.classList.remove('show');
      clearTimeout(toastTimer);
      await undo();
    };
  }
}

function openModal({ title, body, actions, wide = false }) {
  el('modal-title').textContent = title;
  el('modal-body').innerHTML = body;
  el('modal-foot').innerHTML = actions;
  document.querySelector('.modal').classList.toggle('wide', wide);
  el('modal-scrim').classList.add('open');
  const focusTarget = el('modal-body').querySelector('input, textarea, select, button');
  if (focusTarget) focusTarget.focus();
}
const closeModal = () => el('modal-scrim').classList.remove('open');

function closeDrawer() {
  el('drawer').classList.remove('open');
  el('scrim').classList.remove('open');
}

/* -------------------------------------------------------------- the views */

const views = {};

/* ---- Home (§1 7.png) ---------------------------------------------------- */

views.home = {
  async load() {
    state.home = await api('/api/home');
    if (!state.settings) state.settings = await api('/api/settings');
  },
  render() {
    const d = state.home;
    const binding = primaryBinding('dictation');
    const bindingCaps = binding ? capsFromLabel(binding.label) : '';

    const greeting = `
      <div class="page-title" style="gap:10px;flex-wrap:wrap">
        <span>Get back into the flow with</span> ${bindingCaps || '<span class="muted">your hotkey</span>'}
      </div>`;

    const rail = card(`
      <div class="stack" style="gap:20px">
        ${stat({ label: 'words today', value: fmt.int(d.today.words), serif: true })}
        ${d.today.avg_wpm === null
          ? locked({ label: 'words per minute', need: 3, have: d.today.measured_sessions || 0 })
          : stat({ label: 'words per minute', value: fmt.dec(d.today.avg_wpm, 0), serif: true })}
        ${stat({ label: d.streaks.current === 1 ? 'day streak' : 'day streak', value: fmt.int(d.streaks.current), serif: true, foot: `longest ${d.streaks.longest}` })}
      </div>`);

    const recent = d.recent.length
      ? `<div class="rows">${d.recent.map(historyRow).join('')}</div>`
      : `<div class="empty">Hold ${bindingCaps || 'your hotkey'} and say something. It will appear here.</div>`;

    return `
      ${greeting}
      <div style="margin-top:32px">${agentCard(d)}</div>
      <div class="grid" style="grid-template-columns:minmax(0,1fr) 310px;margin-top:32px;align-items:start">
        <div>
          <div class="sec-head" style="margin-top:4px">
            <span class="meta-label">Today</span>
            ${iconBtn('search', 'Search history', 'data-act="go-history"')}
          </div>
          ${recent}
        </div>
        ${rail}
      </div>`;
  },
};

/**
 * Split a location hash into a view and, for Settings, a tab.
 *
 * The bar's microphone and language controls ask for "#settings/voice"
 * specifically. Before this, the tab was parsed off and thrown away by the
 * engine, so every control that promised a particular settings page opened
 * the general one.
 *
 * Returns null when the hash names no view we have.
 */
function routeFor(hash) {
  const [view, tab] = (hash || '').replace('#', '').split('/');
  if (!views[view]) return null;
  if (view === 'settings' && tab) state.settingsTab = tab;
  return view;
}

/**
 * Agent status — the merged health banner and on/off control. Red is used
 * only where the user must act (§5); a transient condition gets no colour.
 */
function agentCard(d) {
  const status = d.status || {};
  const health = d.health || {};
  const listening = status.listening;
  const keyMissing = state.settings && state.settings.key && !state.settings.key.configured;
  // The engine's richest error copy (engine._describe_failure) has always
  // been served here and has never been read by anything. It is the only
  // place that says *what* broke and whether the user can do something.
  const failure = status.last_error;

  let dot = 'ok';
  let headline = 'Listening for your hotkey';
  let detail = health.detail || '';
  let action = '';

  if (keyMissing) {
    dot = 'act';
    headline = 'Paused — no API key';
    detail = 'Sway cannot transcribe until a Groq key is set.';
    action = '<button class="btn primary" data-act="go-key">Fix this</button>';
  } else if (listening === false) {
    dot = 'warn';
    headline = 'Paused';
    detail = 'Your hotkey is disarmed. Nothing is being recorded.';
  } else if (failure) {
    // Actionability decides the colour, not severity (§2.1): red is reserved
    // for a condition only the user can clear.
    dot = failure.actionable ? 'act' : 'warn';
    headline = failure.headline || 'That dictation failed';
    detail = failure.detail || detail;
    if (failure.kind === 'auth' || failure.kind === 'no_key') {
      action = '<button class="btn primary" data-act="go-key">Fix this</button>';
    } else if (failure.kind === 'audio') {
      action = '<button class="btn" data-act="go-voice">Check microphone</button>';
    }
  } else if (health.status === 'problem') {
    // These two used to test for 'error' and 'warn', which queries.health()
    // never returns, so neither branch could ever run.
    dot = 'act';
    headline = health.headline || 'Recent dictations are failing';
  } else if (health.status === 'degraded') {
    dot = 'warn';
    headline = health.headline || 'Degraded';
  }

  const toggleControl = status.can_toggle
    ? `<div style="display:flex;align-items:center;gap:10px">
         <span class="small muted">${listening === false ? 'Off' : 'On'}</span>
         ${toggle(listening !== false, 'data-act="toggle-agent"')}
       </div>`
    : '';

  return card(`
    <div class="status-card">
      <span class="dot ${dot}" style="margin-top:7px"></span>
      <div class="grow">
        <div class="status-head">${esc(headline)}</div>
        ${detail ? `<div class="small muted" style="margin-top:4px">${esc(detail)}</div>` : ''}
      </div>
      ${action}
      ${toggleControl}
    </div>`);
}

function historyRow(entry) {
  const preview = entry.preview || '';
  return `
    <div class="row" data-id="${esc(entry.id)}">
      <span class="when">${esc(fmt.clock(entry.created_at))}</span>
      <div class="body">
        <div class="text">${esc(preview) || '<span class="muted">empty</span>'}</div>
      </div>
      <div class="actions">
        ${iconBtn('copy', 'Copy', `data-act="copy" data-id="${esc(entry.id)}"`)}
        ${iconBtn('trash', 'Delete', `data-act="delete" data-id="${esc(entry.id)}"`)}
      </div>
    </div>`;
}

/* ---- History ------------------------------------------------------------ */

views.history = {
  async load() {
    state.activity = await api('/api/activity', { q: state.query, limit: 200 });
  },
  render() {
    const rows = state.activity.sessions;
    const body = rows.length
      ? `<div class="rows">${rows.map(historyRow).join('')}</div>`
      : state.query
        ? nothingFound(`No matches for “${state.query}”`)
        : hero({
            title: 'Everything you dictate lands here.',
            body: 'Each dictation is stored on this machine with its text, its timing, and the app it went to. Nothing is uploaded.',
          });

    return `
      <div style="display:flex;align-items:center;gap:16px">
        <h1 class="page-title">History</h1>
        <div class="spacer" style="flex:1"></div>
        ${rows.length || state.query ? `<button class="btn danger small" data-act="delete-all">Delete all</button>` : ''}
      </div>
      <div class="tabs">
        <div class="tab-actions" style="margin-left:0;flex:1;padding-bottom:8px">
          <div class="search-wrap" style="width:300px">
            ${icon('search')}
            <input class="input" id="search" placeholder="Search your dictations" value="${esc(state.query)}">
          </div>
        </div>
        <span class="small muted" style="padding-bottom:14px">${fmt.int(state.activity.total)} total</span>
      </div>
      <div style="margin-top:26px">${body}</div>`;
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

/* ---- Dictionary (§1 10.png) --------------------------------------------- */

views.dictionary = {
  async load() { state.dictionary = await api('/api/dictionary'); },
  render() {
    const entries = state.dictionary.entries || [];
    if (!state.dictionary.available) {
      return `<h1 class="page-title">Dictionary</h1>
        <div style="margin-top:26px">${nothingFound('The dictionary is not available in this window.')}</div>`;
    }

    if (!entries.length) {
      return `<h1 class="page-title">Dictionary</h1>
        <div style="margin-top:26px">${hero({
          title: 'Sway spells the way you do.',
          body: 'Add names, jargon, and product words Sway keeps getting wrong. They are sent '
              + 'to the transcriber as a hint, so it knows to expect them. Sway also picks up '
              + 'names you dictate often, and shows you what it learned.',
          chips: ['Ashwini', 'Groq', 'Kubernetes', 'Mayfair'].map(
            (w) => `<span class="chip on-dark">${esc(w)}</span>`),
          action: { act: 'add-term', label: 'Add a word' },
        })}</div>`;
    }

    const scope = state.dictScope || 'all';
    const counts = {
      all: entries.length,
      manual: entries.filter((e) => e.source === 'manual').length,
      learned: entries.filter((e) => e.source === 'learned').length,
      shared: entries.filter((e) => e.source === 'shared').length,
    };
    // Only offer a filter that would actually show something. An empty
    // "Learned" tab teaches the user nothing except that it is empty.
    const tabs = [['all', 'All'], ['manual', 'Added by me'],
                  ['learned', 'Learned'], ['shared', 'Shared']]
      .filter(([id]) => id === 'all' || counts[id] > 0);

    const shown = scope === 'all' ? entries : entries.filter((e) => e.source === scope);

    return `
      <div style="display:flex;align-items:center;gap:16px">
        <h1 class="page-title">Dictionary</h1>
        <div style="flex:1"></div>
        <button class="btn" data-act="import-vocab">Import</button>
        <button class="btn" data-act="export-vocab">Export</button>
        <button class="btn primary" data-act="add-term">${icon('plus')} Add new</button>
      </div>
      ${tabs.length > 1 ? `<div class="tabs">${tabs.map(([id, label]) =>
        `<button class="tab ${scope === id ? 'active' : ''}" data-scope="${id}">${label} <span class="muted">${counts[id]}</span></button>`).join('')}</div>` : ''}
      <div class="small muted" style="margin-top:14px">
        ${entries.length} term${entries.length === 1 ? '' : 's'} biasing transcription.
        Pin the ones that matter most — the transcriber only has room for a couple of hundred characters.
      </div>
      <div style="margin-top:24px">${shown.length ? termChips(shown) : nothingFound('Nothing here yet.')}</div>`;
  },
  bind(root) {
    root.querySelectorAll('[data-scope]').forEach((tab) => {
      tab.addEventListener('click', async () => {
        state.dictScope = tab.dataset.scope;
        await refresh();
      });
    });
  },
};

const SOURCE_LABEL = { manual: 'added by you', learned: 'learned from your dictation', shared: 'from a shared pack' };

function termChips(entries) {
  return `<div class="chips" style="display:flex;flex-wrap:wrap;gap:9px">
    ${entries.map((e) => {
      const used = e.hits ? `, said ${e.hits} time${e.hits === 1 ? '' : 's'}` : '';
      const title = `${SOURCE_LABEL[e.source] || e.source}${used}`;
      return `<span class="chip ${e.pinned ? 'selected' : ''}" title="${esc(title)}">
        <span class="txt" style="cursor:pointer" data-act="pin-term"
              data-term="${esc(e.term)}" data-pinned="${e.pinned ? '1' : ''}"
          >${e.pinned ? '★ ' : ''}${esc(e.term)}</span>
        <span class="x" data-act="remove-term" data-term="${esc(e.term)}">×</span>
      </span>`;
    }).join('')}
  </div>`;
}

/* ---- Snippets ------------------------------------------------------------

   Promoted from a Settings tab to a page of its own. Snippets are something
   people curate, not something they configure once -- and there is only one
   manager for them, so a phrase can never be edited in two places that
   disagree.                                                                */

views.snippets = {
  async load() {
    // Snippets live in settings.json alongside everything else the user edits,
    // so the settings payload already carries them and their conflicts.
    state.settings = await api('/api/settings');
  },
  render() {
    const all = (state.settings && state.settings.triggers) || [];
    const query = (state.snippetQuery || '').trim().toLowerCase();
    const shown = query
      ? all.filter((t) => [t.label, t.phrase, t.payload].some(
          (field) => (field || '').toLowerCase().includes(query)))
      : all;

    if (!all.length) {
      return `
        <h1 class="page-title">Snippets</h1>
        <div style="margin-top:26px">${hero({
          title: "The stuff you shouldn't have to say twice.",
          body: 'Save the text you retype — an intro email, a signature, an address — '
              + 'and say its phrase to insert it. Matched on this machine before anything '
              + 'is sent anywhere, so it costs nothing and adds no delay.',
          chips: [
            '<span class="chip on-dark">“intro email” → Hey, I’d love to find some time…</span>',
            '<span class="chip on-dark">“my email” → you@example.com</span>',
          ],
          action: { act: 'add-trigger', label: 'Add a snippet' },
        })}</div>`;
    }

    return `
      <div style="display:flex;align-items:center;gap:16px">
        <h1 class="page-title">Snippets</h1>
        <div style="flex:1"></div>
        <button class="btn primary" data-act="add-trigger">${icon('plus')} Add new</button>
      </div>
      <div class="search-wrap" style="margin-top:18px">
        ${icon('search')}
        <input class="input" id="snippet-search" placeholder="Search snippets"
               value="${esc(state.snippetQuery || '')}" autocomplete="off">
      </div>
      <div class="small muted" style="margin-top:14px">
        ${all.length} snippet${all.length === 1 ? '' : 's'}. Say the phrase, or “use my …”, to insert one.
      </div>
      <div style="margin-top:22px">
        ${shown.length ? snippetRows(shown) : nothingFound(`No snippet matches “${query}”.`)}
      </div>`;
  },
  bind(root) {
    const search = root.querySelector('#snippet-search');
    if (!search) return;
    search.addEventListener('input', async (event) => {
      state.snippetQuery = event.target.value;
      // keepFocus so the caret survives the re-render, as History's search does.
      await refresh({ keepFocus: '#snippet-search' });
    });
  },
};

function snippetRows(triggers) {
  return `<div class="rows">${triggers.map((t) => `
    <div class="row">
      ${toggle(t.enabled, `data-act="toggle-trigger" data-id="${esc(t.id)}"`)}
      <div class="body">
        ${t.name ? `<div class="text">${esc(t.name)}</div>` : ''}
        <div class="pair" ${t.name ? 'style="margin-top:4px"' : ''}>
          <span class="chip"><span class="txt">“${esc(t.phrase)}”</span></span>
          <span class="arrow">&rarr;</span>
          <span class="chip"><span class="txt">${esc(t.type === 'date' ? "today's date" : t.payload)}</span></span>
        </div>
        ${t.conflicts && t.conflicts.length
          ? `<div class="small" style="color:var(--red);margin-top:6px">
               Overlaps “${esc(t.conflicts[0])}” — the longer phrase wins.
             </div>` : ''}
      </div>
      <div class="actions">
        ${t.type === 'date' ? '' : iconBtn('copy', 'Copy text', `data-act="copy-snippet" data-id="${esc(t.id)}"`)}
        ${iconBtn('edit', 'Edit', `data-act="edit-trigger" data-id="${esc(t.id)}"`)}
        ${iconBtn('trash', 'Delete', `data-act="delete-trigger" data-id="${esc(t.id)}"`)}
      </div>
    </div>`).join('')}</div>`;
}

/* ---- Transforms ----------------------------------------------------------

   A transform is a saved instruction. It reaches the text two ways, and the
   card says which are set up, because the two cost different things: a
   shortcut runs one model call over a selection, a phrase folds its
   instruction into the cleanup call that was happening anyway.            */

// Mirrors sway.transforms.MAX_INSTRUCTION_CHARS. A textarea maxlength
// silently truncates a pasted value that exceeds it -- no error, no
// warning -- so this has to actually match the server's ceiling, not just
// be "generously large": a mismatch here is exactly the bug that silently
// clipped a user's own long, deliberate prompt.
const TRANSFORM_INSTRUCTION_MAX_CHARS = 8000;

views.transforms = {
  async load() { state.settings = await api('/api/settings'); },
  render() {
    const all = (state.settings && state.settings.transforms) || [];

    if (!all.length) {
      return `
        <h1 class="page-title">Transforms</h1>
        <div style="margin-top:26px">${hero({
          title: 'Say it once. Shape it after.',
          body: 'A transform is a saved instruction that rewrites text — polish it, '
              + 'make it concise, turn it into an email. Run one over any selection with '
              + 'its shortcut, or say its phrase while dictating.',
          chips: ['Polish', 'Make concise', 'Rewrite professionally'].map(
            (w) => `<span class="chip on-dark">${esc(w)}</span>`),
          action: { act: 'add-transform', label: 'Create a transform' },
        })}</div>`;
    }

    return `
      <div style="display:flex;align-items:center;gap:16px">
        <h1 class="page-title">Transforms</h1>
        <div style="flex:1"></div>
        <button class="btn" data-act="reset-transforms">${icon('back')} Reset to defaults</button>
        <button class="btn primary" data-act="add-transform">${icon('plus')} New transform</button>
      </div>
      <div class="small muted" style="margin-top:14px">
        Transform works anywhere you write. A transform only costs a request when you run it.
      </div>
      <div class="grid cols-3" style="margin-top:24px">
        ${all.map(transformCard).join('')}
        ${transformCreateTile()}
      </div>`;
  },
};

function transformCreateTile() {
  return `<button class="card transform-tile" data-act="add-transform">
    <span class="transform-tile-plus">${icon('plus')}</span>
    <div style="font-weight:600">Create your own</div>
    <div class="small muted">Write your own prompt</div>
  </button>`;
}

function transformCard(t) {
  const caps = (t.shortcut_labels || []).map((label) => capsFromLabel(label)).join('');
  const preview = t.instruction || t.description;
  return `<div class="card transform-card" data-act="edit-transform" data-id="${esc(t.id)}">
    <div class="transform-card-caps">
      ${caps || '<span class="small muted">No shortcut yet</span>'}
    </div>
    <div style="font-weight:600;margin-top:12px">${esc(t.name)}</div>
    <div class="small muted transform-card-preview">${esc(preview)}</div>
    ${t.phrase ? `<div class="chip" style="margin-top:10px"><span class="txt">“${esc(t.phrase)}”</span></div>` : ''}
    ${!t.runnable
      ? `<div class="small muted" style="margin-top:10px">No shortcut or phrase yet — it cannot be run.</div>` : ''}
    ${t.conflicts && t.conflicts.length
      ? `<div class="small" style="color:var(--red);margin-top:8px">Same shortcut as “${esc(t.conflicts[0])}” — only one of them will run.</div>`
      : ''}
    <div class="transform-card-actions">
      ${iconBtn('edit', 'Edit', `data-act="edit-transform" data-id="${esc(t.id)}"`)}
      ${iconBtn('trash', 'Delete', `data-act="delete-transform" data-id="${esc(t.id)}"`)}
    </div>
  </div>`;
}

/* ---- The transform editor sheet ------------------------------------------

   A two-pane sheet, not a form-in-a-modal: the left side is what the
   transform *is* (its name, how you run it, an example), the right side is
   what it *does* (the prompt, the one thing that actually matters). Autosaved
   on every field change (debounced 600ms for text, immediate for a shortcut
   or a checkbox), so there is no separate Save step to forget.                */

let sheetState = null;
let sheetSaveTimer = null;
let sheetSavePending = false;

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
  return `<span class="muted">No example yet — run it once from its shortcut or phrase, and what it produced will show up here.</span>`;
}

function renderTransformSheet() {
  const t = sheetState;
  const isNew = !t.id;
  const caps = (t.shortcut_labels || []).map((label, i) => {
    const advisory = (t.shortcut_advisories || [])[i];
    return `
    <span class="cap-row" style="gap:4px">
      ${capsFromLabel(label)}
      ${iconBtn('edit', 'Change', `data-act="edit-transform-shortcut" data-index="${i}"`)}
      ${iconBtn('trash', 'Remove', `data-act="remove-transform-shortcut" data-index="${i}"`)}
    </span>
    ${advisory ? `<div class="small muted" style="margin-top:2px">${esc(advisory)}</div>` : ''}`;
  }).join('');

  el('modal-title').textContent = '';
  el('modal-body').innerHTML = `
    <div class="sheet-grid">
      <div class="sheet-col">
        <input class="sheet-name-input" id="x-name" value="${esc(t.name)}"
          placeholder="Name this transform" maxlength="80">
        <div class="small muted" style="margin-top:2px">
          ${(t.shortcut_labels || []).length || t.phrase
            ? `${caps || ''} ${t.phrase ? `<span class="chip" style="margin-left:6px"><span class="txt">“${esc(t.phrase)}”</span></span>` : ''} to use`
            : 'No shortcut or phrase set yet'}
        </div>
        <div style="margin-top:22px">
          <label class="field-label" for="x-desc">Description <span class="muted">(optional)</span></label>
          <input class="input" id="x-desc" value="${esc(t.description)}" placeholder="What it is for" maxlength="200">
        </div>
        <div style="margin-top:22px">
          <div class="field-label">Example of transformed text</div>
          <div class="well transform-example">${transformExampleHtml(t)}</div>
        </div>
      </div>
      <div class="sheet-col">
        <div class="well">
          <div class="field-label">Shortcuts</div>
          <div class="small muted" style="margin:-3px 0 10px">Runs it over whatever you have selected.</div>
          <div class="stack" style="gap:8px">${caps}</div>
          <button class="btn small ghost" style="margin-top:8px" data-act="add-transform-shortcut">
            ${icon('plus')} Add a shortcut
          </button>
        </div>
        <div class="well" style="margin-top:16px">
          <label class="field-label" for="x-phrase">Spoken phrase <span class="muted">(optional)</span></label>
          <input class="input" id="x-phrase" value="${esc(t.phrase)}" placeholder="make that concise" maxlength="80">
          <div class="small muted" style="margin-top:6px">Say it at the end of a dictation. Folded into the cleanup request, so it costs no extra call.</div>
        </div>
        <div style="margin-top:22px;flex:1;display:flex;flex-direction:column">
          <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
            <label class="field-label" for="x-instruction">Prompt</label>
            <span style="display:flex;align-items:baseline;gap:10px">
              <span class="small muted" id="x-charcount">${t.instruction.length} / ${TRANSFORM_INSTRUCTION_MAX_CHARS}</span>
              <span class="small muted" id="x-autosave">Autosave on</span>
            </span>
          </div>
          <textarea class="input sheet-prompt" id="x-instruction" maxlength="${TRANSFORM_INSTRUCTION_MAX_CHARS}" rows="10"
            placeholder="Rewrite the text into a highly effective AI prompt while preserving the intent.">${esc(t.instruction)}</textarea>
          <div class="small muted" style="margin-top:6px">Written to the model as-is. Say what to preserve, not just what to change.</div>
        </div>
        <details style="margin-top:18px">
          <summary class="small" style="cursor:pointer;color:var(--muted)">What it may do to the text</summary>
          <div class="small muted" style="margin:8px 0">Tick what this prompt is <em>supposed</em> to do, so Sway knows what's worth flagging. Run from its shortcut, you'll always see the result and decide yourself whether to use it. Said as a phrase mid-dictation, there's no review step — an answer that looks off is dropped and your raw words are pasted instead, so these checkboxes are what keep a correct one from being dropped there.</div>
          <label class="check-row"><input type="checkbox" id="x-md"${t.allows_markdown ? ' checked' : ''}>
            <span>Produces a list or headings <span class="muted">— bullets, numbered points</span></span></label>
          <label class="check-row"><input type="checkbox" id="x-short"${t.may_shorten ? ' checked' : ''}>
            <span>Makes the text shorter <span class="muted">— summarize, condense</span></span></label>
          <label class="check-row"><input type="checkbox" id="x-long"${t.may_lengthen ? ' checked' : ''}>
            <span>Makes the text longer <span class="muted">— adds a greeting, a sign-off</span></span></label>
        </details>
        <div id="x-error" class="small" style="color:var(--red);min-height:18px;margin-top:10px"></div>
      </div>
    </div>`;
  el('modal-foot').innerHTML = `
    ${t.builtin ? `<button class="btn" data-act="reset-transforms">Reset to defaults</button>` : '<div class="spacer"></div>'}
    <div class="spacer"></div>
    <button class="btn primary" data-act="close-transform-sheet">Done</button>`;
  document.querySelector('.modal').classList.add('wide');
  el('modal-scrim').classList.add('open');

  for (const [id, field] of [
    ['x-name', 'name'], ['x-desc', 'description'], ['x-phrase', 'phrase'],
    ['x-instruction', 'instruction'],
  ]) {
    const node = el(id);
    if (node) node.oninput = () => {
      sheetState[field] = node.value;
      if (id === 'x-instruction' && el('x-charcount')) {
        el('x-charcount').textContent = `${node.value.length} / ${TRANSFORM_INSTRUCTION_MAX_CHARS}`;
      }
      scheduleSheetSave();
    };
  }
  for (const [id, field] of [['x-md', 'allows_markdown'], ['x-short', 'may_shorten'], ['x-long', 'may_lengthen']]) {
    const node = el(id);
    if (node) node.onchange = () => { sheetState[field] = node.checked; saveSheetNow(); };
  }
  const focusTarget = el('x-name');
  if (focusTarget && isNew) focusTarget.focus();
}

function scheduleSheetSave() {
  el('x-autosave').textContent = 'Saving…';
  clearTimeout(sheetSaveTimer);
  sheetSaveTimer = setTimeout(saveSheetNow, 600);
}

async function saveSheetNow() {
  if (!sheetState) return;
  if (sheetSavePending) { clearTimeout(sheetSaveTimer); sheetSaveTimer = setTimeout(saveSheetNow, 300); return; }
  const t = sheetState;
  const error = el('x-error');
  if (!t.name.trim() || !t.instruction.trim()) {
    if (el('x-autosave')) el('x-autosave').textContent = t.name.trim() ? 'Needs a prompt' : 'Needs a name';
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
    if (saved) { sheetState = { ...saved }; }
    if (el('x-autosave')) el('x-autosave').textContent = 'Saved';
    if (error) error.textContent = '';
    setBadge('transforms-count', countTransformConflicts());
  } catch (err) {
    if (error) error.textContent = err.message;
    if (el('x-autosave')) el('x-autosave').textContent = 'Autosave on';
  } finally {
    sheetSavePending = false;
  }
}

/* ---- Insights ----------------------------------------------------------- */

views.insights = {
  async load() {
    state.insights = await api('/api/overview', { range: state.range });
    state.performance = await api('/api/performance', { range: state.range });
  },
  render() {
    const tabs = [['usage', 'Your usage'], ['speed', 'Your speed'], ['performance', 'Performance']];
    return `
      <h1 class="page-title">Insights</h1>
      <div class="tabs">
        ${tabs.map(([id, label]) =>
          `<button class="tab ${state.insightsTab === id ? 'active' : ''}" data-tab="${id}">${label}</button>`).join('')}
        <div class="tab-actions">
          <select class="input small" id="range" style="width:132px;height:32px">
            ${[['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['all', 'All time']].map(
              ([v, l]) => `<option value="${v}" ${state.range === v ? 'selected' : ''}>${l}</option>`).join('')}
          </select>
        </div>
      </div>
      <div style="margin-top:30px">${this.panel()}</div>`;
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
        await refresh();
      });
    });
    const range = root.querySelector('#range');
    if (range) range.addEventListener('change', async (e) => { state.range = e.target.value; await refresh(); });
  },
};

function usagePanel() {
  const d = state.insights;
  if (!d.has_data) {
    return hero({
      title: 'Your numbers appear once you have dictated.',
      body: 'Words, speaking rate, which apps you use Sway in, and how consistent you have been — all measured from your own dictations, never estimated unless labelled.',
    });
  }
  const w = d.window;
  const apps = d.apps.length
    ? d.apps.map((a) => `
        <div class="wf">
          <div class="name">${esc(fmt.app(a.app))}</div>
          <div class="track"><div class="fill" style="width:${(a.share * 100).toFixed(1)}%"></div></div>
          <div class="val">${fmt.pct(a.share)}</div>
        </div>`).join('')
    : nothingFound('No application recorded yet.');

  return `
    <div class="stack">
      <div class="grid cols-3">
        ${card(stat({ label: 'words dictated', value: fmt.int(w.words) }))}
        ${card(stat({ label: 'dictations', value: fmt.int(w.sessions) }))}
        ${card(stat({
          label: 'time saved', value: fmt.duration(w.time_saved_s), estimate: true,
          foot: `Compared with typing at ${d.typing_wpm} WPM.`,
        }))}
      </div>
      <div class="grid cols-2">
        ${card(`<div class="sec-head"><span class="meta-label">Where you use Sway</span></div>
                <div class="waterfall">${apps}</div>`)}
        ${card(`<div class="sec-head">
                  <span class="meta-label">Consistency</span>
                  <span class="small muted">longest ${d.streaks.longest} d</span>
                </div>${heatmap(d.daily)}`)}
      </div>
      ${card(`<div class="sec-head"><span class="meta-label">Words per day</span></div>${barChart(d.daily, 'words')}`)}
    </div>`;
}

function speedPanel() {
  const d = state.insights;
  const measured = d.daily.filter((b) => b.avg_wpm !== null);
  const all = d.all_time;

  if (!measured.length) {
    return `<div class="grid cols-3">
      ${card(locked({ label: 'words per minute', need: 3, have: all.measured_sessions || 0 }))}
      ${card(stat({ label: 'words all time', value: fmt.int(all.words) }))}
      ${card(stat({ label: 'dictations all time', value: fmt.int(all.sessions) }))}
    </div>
    <div class="small muted" style="margin-top:18px">
      Speaking rate needs the measured speech duration of a dictation, which is recorded from this version onward.
    </div>`;
  }

  const recent = measured[measured.length - 1].avg_wpm;
  const best = Math.max(...measured.map((b) => b.avg_wpm));
  const delta = all.avg_wpm ? (recent - all.avg_wpm) / all.avg_wpm : null;

  return `
    <div class="stack">
      <div class="grid cols-3">
        ${card(stat({
          label: 'most recent day', value: fmt.dec(recent, 0),
          // Compared against the user's own history only. There is no
          // reliable external cohort to rank them against, so we do not
          // invent a percentile.
          foot: delta === null ? 'No baseline yet.'
            : `${Math.abs(delta * 100).toFixed(0)}% ${delta >= 0 ? 'faster' : 'slower'} than your all-time average.`,
        }))}
        ${card(stat({ label: 'personal best', value: fmt.dec(best, 0) }))}
        ${card(stat({ label: 'all-time average', value: fmt.dec(all.avg_wpm, 0), foot: `${all.measured_sessions} measured dictations` }))}
      </div>
      ${card(`<div class="sec-head"><span class="meta-label">Speaking rate by day</span></div>
              ${barChart(measured.map((b) => ({ day: b.day, wpm: Math.round(b.avg_wpm) })), 'wpm')}`)}
    </div>`;
}

function performancePanel() {
  const p = state.performance;
  if (!p.total) return nothingFound('No dictations recorded in this window.');

  const l = p.latency;
  const stages = p.stages.length
    ? p.stages.map((s) => `
        <div class="wf">
          <div class="name">${esc(s.stage)}</div>
          <div class="track"><div class="fill${s.share > 0.4 ? ' slow' : ''}" style="width:${(s.share * 100).toFixed(1)}%"></div></div>
          <div class="val">${fmt.ms(s.median_ms)}</div>
        </div>`).join('')
    : nothingFound('No stage timings recorded.');

  // Zero errors is printed as zero (§6): it is a fact, not an absence.
  const errors = p.errors.length
    ? `<div class="rows">${p.errors.map((e) => `
        <div class="row">
          <div class="body"><div class="text">${esc(errorLabel(e.error))}</div></div>
          <span class="small muted">${fmt.int(e.count)}</span>
        </div>`).join('')}</div>`
    : `<div style="padding:12px 0">${stat({ label: `errors in this window`, value: '0', foot: `across ${fmt.int(p.total)} dictations` })}</div>`;

  // Unlike Failures, no zero-state card: a guardrail tally with nothing in
  // it is not a fact worth a permanent card the way "0 errors" is -- most
  // windows will never trip one, and a page of empty cards is noise (§6
  // strategy: show a state only when there is a state to show).
  const guardrails = p.guardrails.length
    ? card(`<div class="sec-head"><span class="meta-label">Guardrails</span></div>
        <div class="small muted" style="margin-top:2px">
          Reviewed rewrites (transforms and voice commands) that a guardrail flagged before you saw them.
        </div>
        <div class="rows" style="margin-top:10px">${p.guardrails.map((g) => `
          <div class="row">
            <div class="body"><div class="text">${esc(guardrailLabel(g.reason))}</div></div>
            <span class="small muted">${fmt.int(g.count)}</span>
          </div>`).join('')}</div>`)
    : '';

  return `
    <div class="stack">
      <div class="grid cols-3">
        ${card(l.p50 === null
          ? locked({ label: 'p50 latency', need: 5, have: l.count || 0 })
          : stat({ label: 'p50 latency', value: fmt.ms(l.p50), foot: 'hotkey release to pasted' }))}
        ${card(l.count >= 20
          ? stat({ label: 'p95 latency', value: fmt.ms(l.p95) })
          : locked({ label: 'p95 latency', need: 20, have: l.count || 0 }))}
        ${card(stat({ label: 'dictations', value: fmt.int(p.total), foot: p.failed ? `${p.failed} failed` : 'all succeeded' }))}
      </div>
      <div class="grid cols-2">
        ${card(`<div class="sec-head"><span class="meta-label">Where the time goes</span></div>
                <div class="waterfall">${stages}</div>
                <div class="small muted" style="margin-top:12px">Median per stage. The widest bar is your latency budget.</div>`)}
        ${card(`<div class="sec-head"><span class="meta-label">Failures</span></div>${errors}`)}
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
  const pct = (v) => (v === null || v === undefined ? DASH : fmt.pct(v));
  return `
    <div class="grid cols-3">
      ${card(stat({
        label: 'corrected mid-sentence',
        value: fmt.int(u.corrections),
        foot: `${pct(u.correction_rate)} of ${fmt.int(u.dictations)} dictations`,
      }))}
      ${card(stat({
        label: 'resolved with no model call',
        value: pct(u.no_model_call_rate),
        foot: `${fmt.int(u.no_model_call)} needed nothing from the network`,
      }))}
      ${card(stat({
        label: 'snippets and transforms',
        value: fmt.int(u.snippets + u.transforms),
        foot: `${fmt.int(u.snippets)} snippet${u.snippets === 1 ? '' : 's'}, ${fmt.int(u.transforms)} transform${u.transforms === 1 ? '' : 's'}`,
      }))}
    </div>`;
}

/** Engine error codes are not user-facing text. */
function errorLabel(code) {
  const raw = String(code || '');
  if (raw.startsWith('rate_limited:')) {
    const which = raw.slice('rate_limited:'.length);
    return which && which !== 'unknown' ? `Rate limited — ${which.replace(/_/g, ' ')}` : 'Rate limited';
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
// tokens into ("lost_entity:200" -> "lost_entity") -- kept as a label map
// rather than reusing advisory_note's sentences, which are written to sit
// next to the specific result they describe, not to summarize a tally.
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

function barChart(buckets, key) {
  if (!buckets.length) return nothingFound('Nothing in this window.');
  const max = Math.max(...buckets.map((b) => b[key] || 0), 1);
  return `<div class="chart">${buckets.map((b) => {
    const value = b[key] || 0;
    return `<div class="col" title="${esc(b.day)}: ${value}"><i style="height:${((value / max) * 100).toFixed(1)}%"></i></div>`;
  }).join('')}</div>`;
}

function heatmap(buckets) {
  if (!buckets.length) return nothingFound('Nothing yet.');
  const max = Math.max(...buckets.map((b) => b.words || 0), 1);
  // Pad to a whole week so the columns line up on weekday rows.
  const first = new Date(buckets[0].day + 'T00:00:00');
  const pad = first.getDay();
  const cells = Array(pad).fill('<i></i>').concat(buckets.map((b) => {
    const value = b.words || 0;
    const level = value === 0 ? 0 : Math.min(4, Math.ceil((value / max) * 4));
    return `<i data-l="${level}" title="${esc(b.day)}: ${value} words"></i>`;
  }));
  return `<div class="heat">${cells.join('')}</div>
    <div class="small muted" style="margin-top:12px">${buckets.filter((b) => b.words > 0).length} active days in this window.</div>`;
}

/* ---- Settings ----------------------------------------------------------- */

views.settings = {
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
    // Snippets used to be a tab here. They are a page now: two managers for
    // one list is how a phrase ends up edited in two places that disagree.
    const tabs = [['general', 'General'], ['voice', 'Voice'], ['apps', 'Apps'],
                  ['data', 'Data']];
    return `
      <h1 class="page-title">Settings</h1>
      <div class="tabs">
        ${tabs.map(([id, label]) => `
          <button class="tab ${state.settingsTab === id ? 'active' : ''}" data-tab="${id}">
            ${label}
          </button>`).join('')}
      </div>
      ${state.settings.editable ? '' : '<div class="banner info" style="margin-top:24px">This window cannot change settings — no engine is attached.</div>'}
      <div style="margin-top:22px">${this.panel()}</div>`;
  },
  panel() {
    const tab = state.settingsTab;
    if (tab === 'general') return generalPanel();
    if (tab === 'voice') return voicePanel();
    if (tab === 'apps') return appsPanel();
    return dataPanel();
  },
  bind(root) {
    root.querySelectorAll('[data-tab]').forEach((tab) => {
      tab.addEventListener('click', async () => { state.settingsTab = tab.dataset.tab; await refresh(); });
    });
  },
};

function countConflicts() {
  const triggers = (state.settings && state.settings.triggers) || [];
  return triggers.filter((t) => t.conflicts && t.conflicts.length).length;
}

const settingRow = (name, hint, control) => `
  <div class="setting">
    <div class="grow"><div class="name">${esc(name)}</div>${hint ? `<div class="hint">${esc(hint)}</div>` : ''}</div>
    <div class="control">${control}</div>
  </div>`;

function generalPanel() {
  const s = state.settings;
  const key = s.key || {};
  const settings = s.settings || {};

  const keyControl = key.configured
    ? `<span class="mono muted">${esc(key.masked)}</span>
       <button class="btn small" data-act="set-key">Change</button>
       <button class="btn small" data-act="test-key">Test connection</button>`
    : `<button class="btn primary small" data-act="set-key">Add a key</button>`;

  const sourceHint = key.configured
    ? (key.source === 'keyring'
        ? 'Stored in Windows Credential Manager.'
        : 'Coming from your environment or .env file — not from Credential Manager.')
    : 'Sway cannot transcribe without one. Free keys come from console.groq.com.';

  return `
    <div id="key-result"></div>
    ${settingRow('Groq API key', sourceHint, keyControl)}
    ${settingRow('Start Sway when I sign in', 'Sway runs in the notification area.',
      toggle(settings.launch_at_login, 'data-act="set" data-field="launch_at_login"'))}
    ${settingRow('Overlay position', 'Where the status pill sits while you dictate.',
      `<select class="input" data-act="set" data-field="overlay_position">
        ${(s.overlay_positions || []).map((p) =>
          `<option value="${esc(p)}" ${settings.overlay_position === p ? 'selected' : ''}>${esc(positionLabel(p))}</option>`).join('')}
      </select>`)}`;
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
  // Served from sway.languages rather than hardcoded here: this list used to
  // be its own copy and had already drifted from the bar's -- different
  // names, a missing language, and no way to add one without editing both.
  const langs = s.languages || [];
  // Started here rather than in a dedicated bind hook -- this panel has
  // none -- and self-terminates the moment the tab is no longer showing
  // (see watchActivationPreview), so a stray render never leaves it running.
  watchActivationPreview();

  const deviceList = devices.length
    ? `<div class="rows" style="margin-top:14px">
        ${[{ name: '', label: 'System default', value: '' }].concat(
          devices.map((d) => ({ name: d.name, label: d.name + (d.default ? ' (default)' : ''), value: d.name }))
        ).map((d) => {
          const active = (settings.audio_device || '') === d.value;
          return `<div class="row" style="cursor:pointer" data-act="pick-device" data-device="${esc(d.value)}">
            <span class="dot ${active ? 'ok' : ''}" style="margin-left:6px"></span>
            <div class="body"><div class="text">${esc(d.label)}</div></div>
            ${active ? `<span class="small muted">In use</span>` : ''}
          </div>`;
        }).join('')}
      </div>`
    // Genuinely zero, so the zero is printed (§6 strategy 2).
    : `<div style="margin-top:18px">${stat({ label: 'microphones found', value: '0', foot: 'Windows is not reporting any input device.' })}</div>`;

  const activation = s.activation || {};

  return `
    <div style="margin-top:6px">
      <span class="meta-label">Shortcuts</span>
    </div>
    ${activationRow(activation, 'dictation', 'Dictation hotkey', 'Hold it, speak, release.')}
    ${activationRow(activation, 'command', 'Voice command hotkey', 'Hold it over selected text to transform it.')}
    ${handsFreeRow(activation)}
    ${registeredShortcutsTable(engine)}
    ${settingRow("I'm speaking", 'Leave on auto-detect unless Sway keeps guessing wrong.',
      `<select class="input" data-act="set" data-field="language">
        <option value="">Auto-detect</option>
        ${langs.map((l) =>
          `<option value="${l.code}" ${settings.language === l.code ? 'selected' : ''}>${l.label}</option>`).join('')}
      </select>`)}
    ${settingRow('Write it in',
      'Sway normally writes back whatever you spoke. Pick a language here to always get that one instead.',
      `<select class="input" data-act="set" data-field="target_language">
        <option value="">Same as spoken</option>
        ${langs.map((l) =>
          `<option value="${l.code}" ${settings.target_language === l.code ? 'selected' : ''}>${l.label}</option>`).join('')}
      </select>`)}
    <div style="margin-top:34px">
      <span class="meta-label">Understanding</span>
    </div>
    ${settingRow('Fix what I corrected mid-sentence',
      contextHint(settings.context_mode),
      `<select class="input" data-act="set" data-field="context_mode">
        ${(s.context_modes || []).map((m) =>
          `<option value="${esc(m)}" ${settings.context_mode === m ? 'selected' : ''}>${esc(contextLabel(m))}</option>`).join('')}
      </select>`)}
    ${settingRow('Remove filler words', 'Drops "um", "uh", and "you know" from the final text.',
      toggle(settings.remove_fillers !== false, 'data-act="set" data-field="remove_fillers"'))}
    ${settingRow('Learn names I use often',
      'Adds names and jargon you dictate repeatedly to your Dictionary. Everything it learns is listed there, and you can remove any of it.',
      toggle(settings.auto_learn_vocabulary !== false, 'data-act="set" data-field="auto_learn_vocabulary"'))}
    <div style="margin-top:34px">
      <span class="meta-label">Input device</span>
      ${deviceList}
    </div>`;
}

/** One binding chip, wrapped so the live-preview poller (watchActivationPreview)
 * can find it and paint .held while it is physically down right now. */
function bindingChip(target, b) {
  return `<span class="stack" style="gap:2px;align-items:flex-end">
    <span class="cap-row" data-live-binding="${esc(target)}" data-binding-value="${esc(b.value)}" style="gap:4px">
      ${capsFromLabel(b.label)}
      ${iconBtn('edit', 'Change', `data-act="edit-binding" data-target="${esc(target)}" data-value="${esc(b.value)}"`)}
      ${iconBtn('trash', 'Remove', `data-act="remove-binding" data-target="${esc(target)}" data-value="${esc(b.value)}"`)}
    </span>
    ${b.advisory ? `<div class="small muted">${esc(b.advisory)}</div>` : ''}
  </span>`;
}

/**
 * The Dictation/Voice-command rows: every current binding, each with its own
 * edit/remove, a reset icon once the row is no longer default, and a "+" to
 * add another -- any binding can now be a held key, a modifier combo, a
 * shortcut, or a mouse button, so there is one list rather than a fixed
 * "primary key, optional alternate chord" pair.
 */
function activationRow(activation, target, label, hint) {
  const a = activation[target] || {};
  const bindings = a.bindings || [];
  const hasConflict = a.conflicts && a.conflicts.length > 0;
  return settingRow(label, hint, `
    <div class="stack" style="gap:8px;align-items:flex-end">
      <div class="stack" style="gap:6px;align-items:flex-end">
        ${bindings.length
          ? bindings.map((b) => bindingChip(target, b)).join('')
          : '<span class="small muted">Not set</span>'}
      </div>
      <div style="display:flex;gap:8px">
        <button class="btn small ghost" data-act="add-binding" data-target="${target}">${icon('plus')} Add</button>
        ${!a.is_default ? `<button class="btn small ghost" data-act="reset-binding" data-target="${target}">Reset to default</button>` : ''}
      </div>
    </div>
    ${hasConflict ? `<div class="small" style="color:var(--red)">Same as ${esc(a.conflicts[0])} — only one will fire.</div>` : ''}
  `);
}

/**
 * Hands-free dictation: its own mode toggle (off by default -- this is an
 * additional way to dictate, not a replacement for hold-to-talk), the same
 * binding list as the two hold-based rows, and -- only meaningful once at
 * least one binding is a bare held key or combo, not a shortcut -- whether
 * it must be double-tapped.
 */
function handsFreeRow(activation) {
  const a = activation.hands_free || {};
  const bindings = a.bindings || [];
  const hasConflict = a.conflicts && a.conflicts.length > 0;
  const gesture = bindings.length
    ? (a.double_tap ? 'Double-tap a binding to start, double-tap again to stop.' : 'Tap a binding to start, tap again to stop.')
    : 'Tap once to start recording, tap again to stop — no holding.';

  return `
    ${settingRow('Hands-free dictation', gesture,
      `<span data-live-hands-free></span>${toggle(!!a.enabled, 'data-act="set" data-field="hands_free_enabled"')}`)}
    ${a.enabled ? settingRow('Hands-free binding', '', `
      <div class="stack" style="gap:8px;align-items:flex-end">
        <div class="stack" style="gap:6px;align-items:flex-end">
          ${bindings.length
            ? bindings.map((b) => bindingChip('hands_free', b)).join('')
            : '<span class="small muted">Not set</span>'}
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn small ghost" data-act="add-binding" data-target="hands_free">${icon('plus')} Add</button>
          ${!a.is_default ? `<button class="btn small ghost" data-act="reset-binding" data-target="hands_free">Reset to default</button>` : ''}
        </div>
      </div>
      ${bindings.length ? `<label class="small" style="display:flex;align-items:center;gap:8px;margin-top:10px;justify-content:flex-end">
        Double-tap instead of a single tap
        ${toggle(!!a.double_tap, 'data-act="set" data-field="hands_free_double_tap"')}
      </label>` : ''}
      ${hasConflict ? `<div class="small" style="color:var(--red)">Same as ${esc(a.conflicts[0])} — only one will fire.</div>` : ''}
    `) : ''}
  `;
}

/**
 * Read-only diagnostic table: every binding Sway is actually tracking right
 * now -- dictation, the voice command, hands-free, and every transform's
 * shortcuts, in one place -- with whether it registered cleanly, and
 * whether it has ever fired. Answers "is this my app, another app, or the
 * OS?" without needing to open the log file: a shortcut that never shows
 * "fired" here never reached Sway's own hook at all, so the cause is
 * upstream of Sway (another app or the OS claimed it first); one that shows
 * "fired" but visibly did nothing is downstream (selection capture,
 * injection, or the target app itself).
 */
function registeredShortcutsTable(engine) {
  const bindings = engine.bindings || [];
  if (!bindings.length) return '';

  const statusNote = (b) => {
    if (b.status === 'invalid') return `<span class="small" style="color:var(--red)">invalid${b.detail ? ` — ${esc(b.detail)}` : ''}</span>`;
    if (b.status === 'duplicate') return `<span class="small" style="color:var(--red)">duplicate — “${esc(b.detail)}” claimed it first</span>`;
    return `<span class="small muted">${b.fire_count ? `fired ${fmt.int(b.fire_count)}× — last ${fmt.clock(b.last_fired_at)}` : 'never fired'}</span>`;
  };

  return `
    <div style="margin-top:34px">
      <span class="meta-label">Registered shortcuts</span>
      <div class="rows" style="margin-top:8px">
        ${bindings.map((b) => `
          <div class="row">
            <span class="dot ${b.status === 'registered' ? 'ok' : ''}" style="margin-left:6px"></span>
            <div class="body">
              <div class="text">${esc(b.pretty || b.value)}</div>
              <div class="small muted">${esc(b.label)}</div>
            </div>
            ${statusNote(b)}
          </div>`).join('')}
      </div>
    </div>`;
}

// Named states, not a slider: "aggressiveness 0-10" tells a user nothing about
// what changes. Each label says what Sway will do to their words.
const contextLabel = (m) => ({
  off: 'Leave it as I said it',
  conservative: 'Only when I clearly corrected myself',
  balanced: 'Whenever it looks like a correction',
}[m] || m);

const contextHint = (m) => ({
  off: 'Say "meeting at 7, no, 8" and you get exactly that, word for word.',
  conservative: 'Say "meeting at 7, no, 8" and you get "meeting at 8". Sway only '
    + 'rewrites when you clearly replaced a name, time, date, or figure.',
  balanced: 'Catches more corrections, and will occasionally trim an aside you '
    + 'meant to keep.',
}[m] || '');

function appsPanel() {
  const rules = (state.settings && state.settings.app_rules) || [];
  const styles = (state.settings && state.settings.cleanup_styles) || [];

  const rows = rules.length
    ? `<div class="rows">${rules.map((r) => `
        <div class="row">
          <div class="body"><div class="text">${esc(fmt.app(r.process))}</div></div>
          <select class="input" style="width:170px" data-act="set-rule" data-process="${esc(r.process)}">
            ${styles.map((s) => `<option value="${esc(s)}" ${r.style === s ? 'selected' : ''}>${esc(styleLabel(s))}</option>`).join('')}
          </select>
          <div class="actions">${iconBtn('trash', 'Remove', `data-act="delete-rule" data-process="${esc(r.process)}"`)}</div>
        </div>`).join('')}</div>`
    : hero({
        title: 'Make Sway sound right everywhere.',
        body: 'Sway already writes casually in chat apps, formally in mail, and leaves terminals and editors completely untouched. Add a rule only where you want something different.',
        action: { act: 'add-rule', label: 'Add an app rule' },
      });

  return `
    <div class="sec-head">
      <span class="meta-label">Your rules</span>
      ${rules.length ? '<button class="btn small" data-act="add-rule">Add rule</button>' : ''}
    </div>
    ${rows}
    <div class="small muted" style="margin-top:20px">
      Anything without a rule uses Sway's built-in defaults for that kind of app.
    </div>`;
}

const styleLabel = (s) => ({ casual: 'Casual', formal: 'Formal', verbatim: 'No cleanup' }[s] || s);

function dataPanel() {
  const s = state.settings;
  const settings = s.settings || {};
  const engine = s.engine || {};
  const retention = (field, value) => `
    <select class="input" data-act="set" data-field="${field}" data-numeric="1">
      ${[[0, 'Keep forever'], [7, '7 days'], [30, '30 days'], [90, '90 days'], [365, '1 year']].map(
        ([v, l]) => `<option value="${v}" ${Number(value) === v ? 'selected' : ''}>${l}</option>`).join('')}
    </select>`;

  return `
    ${settingRow('Keep history for', 'Older dictations are removed automatically.',
      retention('history_retention_days', settings.history_retention_days))}
    ${settingRow('Keep timings for', 'Latency measurements behind the Insights charts.',
      retention('latency_retention_days', settings.latency_retention_days))}
    ${settingRow('Data folder', engine.data_directory || '', '<button class="btn small" data-act="open-data">Show me</button>')}
    <div style="margin-top:36px">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px">
        <span class="meta-label">Recent warnings &amp; errors</span>
        <button class="btn small ghost" data-act="refresh-diagnostics">Refresh</button>
      </div>
      <div class="small muted" style="margin-top:2px">
        Read straight from sway.log, so this shows up even when there is no console window to watch.
      </div>
      <div style="margin-top:10px">${diagnosticsPanel()}</div>
    </div>
    <div style="margin-top:36px">
      <div class="banner" style="align-items:flex-start">
        <div style="flex:1">
          <strong>Delete everything</strong>
          <div class="small" style="margin-top:4px">
            Removes every transcript and every timing from this machine. This cannot be undone.
          </div>
        </div>
        <button class="btn danger small" data-act="delete-all">Delete all</button>
      </div>
    </div>`;
}

// sway.logging_setup writes "YYYY-MM-DD HH:mm:ss,SSS" in the machine's own
// local time (Python's default asctime, no timezone in the string) -- parsed
// field-by-field into a local Date rather than handed to `new Date(string)`,
// which would read the comma-millisecond format inconsistently across
// browsers and some would read it as UTC.
function parseLogTimestamp(ts) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2}),(\d{3})$/.exec(ts);
  if (!m) return null;
  const [y, mo, day, h, mi, s, ms] = m.slice(1).map(Number);
  return new Date(y, mo - 1, day, h, mi, s, ms);
}

// A raw "2026-08-26 15:28:29,346" is precise but unreadable at a glance;
// this trades that precision for "how recent, on what day" -- the two
// things that actually matter when triaging a burst of warnings -- and
// keeps the exact value one hover away via `title`.
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
  if (!d) return '<div class="small muted">Loading…</div>';
  if (!d.available) {
    return `<div class="small muted">No log file yet${d.path ? ` (looked in ${esc(d.path)})` : ''}.</div>`;
  }
  if (!d.entries.length) {
    return `<div class="small muted">No warnings or errors recorded — that log file is at ${esc(d.path)}.</div>`;
  }
  // Entries arrive newest-first. The same warning firing repeatedly in a
  // burst (a guardrail tripping on every one of several transform runs) is
  // one event that happened N times, not N separate things to read and
  // compare -- so collapse a run of identical level+logger+message entries
  // into a single row with a count and a time span.
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
  return `<div class="rows">
    ${groups.map((g) => {
      const newest = formatDiagnosticTime(g.newest);
      const countBadge = g.count > 1
        ? ` <span class="small" style="background:var(--well);border-radius:10px;padding:1px 8px;font-weight:600">×${g.count} since ${esc(formatDiagnosticTime(g.oldest).label)}</span>`
        : '';
      return `
      <div class="row" style="align-items:flex-start">
        <span class="dot" style="margin-left:6px;margin-top:7px;background:${g.level === 'WARNING' ? 'var(--amber)' : 'var(--red)'}"></span>
        <div class="body">
          <div class="small muted" title="${esc(newest.title)}">${esc(newest.label)} · ${esc(g.level)} · ${esc(g.logger)}${countBadge}</div>
          <div class="mono small" style="white-space:pre-wrap;word-break:break-word;margin-top:2px">${esc(g.message)}</div>
        </div>
      </div>`;
    }).join('')}
  </div>`;
}

/* ---- About -------------------------------------------------------------- */

views.about = {
  async load() { if (!state.settings) state.settings = await api('/api/settings'); },
  render() {
    const engine = (state.settings && state.settings.engine) || {};
    return `
      <h1 class="page-title">About Sway</h1>
      <div class="stack" style="margin-top:30px;max-width:760px">
        ${card(`
          <div class="sec-head"><span class="meta-label">Where your words go</span></div>
          <div class="stack" style="gap:14px">
            <div><strong>Audio is never stored.</strong>
              <div class="small muted">It lives in memory while you hold the key and is discarded as soon as the clip is sent.</div></div>
            <div><strong>Text stays on this machine.</strong>
              <div class="small muted">Transcripts are written to SQLite files in your data folder. Nothing syncs anywhere.</div></div>
            <div><strong>What reaches Groq.</strong>
              <div class="small muted">The trimmed audio clip, and the transcript for cleanup. Nothing else leaves the machine.</div></div>
            <div><strong>No account, no server.</strong>
              <div class="small muted">Sway has no login and runs no public network listener.</div></div>
          </div>`)}
        ${card(`
          <div class="sec-head"><span class="meta-label">This build</span></div>
          <div class="kv">
            ${Object.entries(engine).map(([k, v]) =>
              `<div class="kv-row"><span class="kv-key">${esc(k.replace(/_/g, ' '))}</span><span class="kv-val mono">${esc(v ?? DASH)}</span></div>`).join('')
              || '<div class="small muted">No engine attached.</div>'}
          </div>`)}
      </div>`;
  },
};

/* ------------------------------------------------------------ the drawer */

async function openEntry(id) {
  el('drawer').classList.add('open');
  el('scrim').classList.add('open');
  const body = el('drawer-body');
  body.innerHTML = '<div class="empty">Loading…</div>';

  let d;
  try {
    d = await api('/api/session', { id });
  } catch (err) {
    body.innerHTML = nothingFound(err.message);
    return;
  }

  el('drawer-title').textContent = fmt.app(d.app);
  el('drawer-sub').textContent = `${fmt.date(d.created_at)} · ${fmt.clock(d.created_at)}`;

  const changed = d.raw_text && d.raw_text !== d.text;
  const total = d.stages.reduce((sum, s) => sum + s.duration_ms, 0) || 1;

  const meta = [
    ['Words', fmt.int(d.word_count)],
    ['Speaking rate', d.wpm === null ? 'Not measured' : `${fmt.dec(d.wpm, 0)} wpm`],
    ['Felt latency', d.run && d.run.release_to_paste_ms !== null ? fmt.ms(d.run.release_to_paste_ms) : 'Not recorded'],
    ['Language', d.language || 'Not recorded'],
    ['Result', d.status || 'Not recorded'],
  ];

  body.innerHTML = `
    <div>
      <div class="meta-label" style="margin-bottom:9px">What was pasted</div>
      <div class="text-block selectable">${esc(d.text) || '<span class="muted">empty</span>'}</div>
      <button class="btn small" style="margin-top:10px" data-act="copy" data-id="${esc(d.id)}">${icon('copy')} Copy</button>
    </div>
    ${changed ? `
      <div>
        <div class="meta-label" style="margin-bottom:9px">Before cleanup</div>
        <div class="text-block dim selectable">${esc(d.raw_text)}</div>
      </div>`
      : '<div class="small muted">Cleanup made no changes to this transcript.</div>'}
    ${d.stages.length ? `
      <div>
        <div class="meta-label" style="margin-bottom:11px">Where the time went</div>
        <div class="waterfall">${d.stages.map((s) => `
          <div class="wf">
            <div class="name">${esc(s.stage)}</div>
            <div class="track"><div class="fill${s.duration_ms / total > 0.4 ? ' slow' : ''}" style="width:${((s.duration_ms / total) * 100).toFixed(1)}%"></div></div>
            <div class="val">${fmt.ms(s.duration_ms)}</div>
          </div>`).join('')}</div>
      </div>` : ''}
    <div>
      <div class="meta-label" style="margin-bottom:9px">Details</div>
      <div class="kv">${meta.map(([k, v]) =>
        `<div class="kv-row"><span class="kv-key">${esc(k)}</span><span class="kv-val">${esc(v)}</span></div>`).join('')}</div>
    </div>`;
}

/* -------------------------------------------------------------- actions */

async function copyEntry(id) {
  const entry = await api('/api/session', { id });
  await navigator.clipboard.writeText(entry.text || '');
  toast('Copied');
}

async function deleteEntry(id) {
  // Optimistic with an undo, rather than a confirm dialog: the reference
  // shows no confirms (§7), and a single row is cheap to restore compared
  // with interrupting the user every time.
  const entry = await api('/api/session', { id });
  await post('/api/history/delete', { id });
  state.lastDeleted = entry;
  await refresh();
  toast('Dictation deleted', async () => {
    // Nothing can re-insert a deleted row: the store has no restore path and
    // inventing one would mean keeping deleted text on disk, which is the
    // opposite of what delete means. So undo restores it to the clipboard,
    // which is the honest version of the promise.
    await navigator.clipboard.writeText(state.lastDeleted.text || '');
    toast('Put back on your clipboard');
  });
}

async function confirmDeleteAll() {
  openModal({
    title: 'Delete every dictation?',
    body: `<p class="muted" style="margin:0">
        This removes every transcript and every timing stored on this machine.
        It cannot be undone.
      </p>`,
    actions: `<div class="spacer"></div>
      <button class="btn" data-act="close-modal">Cancel</button>
      <button class="btn danger" data-act="confirm-delete-all">Delete everything</button>`,
  });
}

async function saveSetting(field, value) {
  try {
    state.settings = await post('/api/settings', { [field]: value });
    const applied = state.settings.applied || {};
    toast(applied.restart_required ? 'Saved — restart Sway to apply' : 'Saved');
    await refresh();
  } catch (err) {
    toast(err.message);
  }
}

function keyDialog() {
  openModal({
    title: 'Groq API key',
    body: `
      <div>
        <label class="field-label" for="key-input">Paste your key</label>
        <input class="input mono" id="key-input" type="password" placeholder="gsk_…" autocomplete="off" spellcheck="false">
      </div>
      <div class="small muted">
        Free keys come from console.groq.com. It is stored in Windows Credential Manager,
        never in a file Sway writes.
      </div>
      <div id="key-error" class="small" style="color:var(--red)"></div>`,
    actions: `<div class="spacer"></div>
      <button class="btn" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-key">Save</button>`,
  });
}

async function saveKey() {
  const input = el('key-input');
  const error = el('key-error');
  const value = input.value.trim();
  if (!value) { error.textContent = 'Paste a key first.'; return; }
  try {
    const result = await post('/api/key', { key: value });
    closeModal();
    state.settings = null;
    await refresh();
    toast(result.warning || 'Key saved');
  } catch (err) {
    error.textContent = err.message;
  }
}

async function testKey() {
  const target = el('key-result');
  if (target) target.innerHTML = '<div class="banner info" style="margin-bottom:18px">Testing…</div>';
  try {
    const result = await post('/api/key/test', {});
    if (!target) return;
    target.innerHTML = result.ok
      ? `<div class="banner info" style="margin-bottom:18px">
           <span class="dot ok"></span> Connected · ${esc(result.model || '')} · ${fmt.ms(result.latency_ms)}
         </div>`
      : `<div class="banner" style="margin-bottom:18px">
           <span class="dot act"></span> ${esc(result.detail || 'Groq rejected the key.')}
         </div>`;
  } catch (err) {
    if (target) target.innerHTML = `<div class="banner" style="margin-bottom:18px">${esc(err.message)}</div>`;
  }
}

function triggerDialog(existing) {
  const t = existing || { id: '', name: '', phrase: '', payload: '', type: 'macro' };
  openModal({
    title: existing ? 'Edit snippet' : 'New snippet',
    body: `
      <div>
        <label class="field-label" for="t-name">Name <span class="muted">(optional)</span></label>
        <input class="input" id="t-name" value="${esc(t.name || '')}" placeholder="Intro email" maxlength="80">
      </div>
      <div>
        <label class="field-label" for="t-phrase">When I say</label>
        <input class="input" id="t-phrase" value="${esc(t.phrase)}" placeholder="intro email" maxlength="80">
      </div>
      <div>
        <label class="field-label" for="t-type">Insert</label>
        <select class="input" id="t-type">
          <option value="macro" ${t.type === 'macro' ? 'selected' : ''}>A fixed piece of text</option>
          <option value="date" ${t.type === 'date' ? 'selected' : ''}>Today's date</option>
        </select>
      </div>
      <div id="t-payload-wrap" class="${t.type === 'date' ? 'hidden' : ''}">
        <label class="field-label" for="t-payload">Text to insert</label>
        <textarea class="input" id="t-payload" maxlength="4000" placeholder="you@example.com">${esc(t.payload)}</textarea>
      </div>
      <div class="small muted">Say the phrase, or “use my …”, while dictating. Matched on this machine before anything is sent — no delay, no tokens.</div>
      <div id="t-error" class="small" style="color:var(--red)"></div>`,
    actions: `<div class="spacer"></div>
      <button class="btn" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-trigger" data-id="${esc(t.id)}">Save</button>`,
  });
  el('t-type').addEventListener('change', (event) => {
    el('t-payload-wrap').classList.toggle('hidden', event.target.value === 'date');
  });
}

async function saveTrigger(id) {
  const error = el('t-error');
  const body = {
    id: id || undefined,
    name: el('t-name').value,
    phrase: el('t-phrase').value,
    type: el('t-type').value,
    payload: el('t-payload').value,
  };
  try {
    state.settings = await post('/api/triggers/save', body);
    closeModal();
    await refresh();
    toast('Trigger saved');
  } catch (err) {
    error.textContent = err.message;
  }
}

function appRuleDialog() {
  const styles = (state.settings && state.settings.cleanup_styles) || [];
  openModal({
    title: 'App rule',
    body: `
      <div>
        <label class="field-label" for="r-process">Application</label>
        <input class="input" id="r-process" placeholder="slack.exe" maxlength="120">
        <div class="small muted" style="margin-top:7px">The executable name, as Windows reports it.</div>
      </div>
      <div>
        <label class="field-label" for="r-style">Write like</label>
        <select class="input" id="r-style">
          ${styles.map((s) => `<option value="${esc(s)}">${esc(styleLabel(s))}</option>`).join('')}
        </select>
      </div>
      <div id="r-error" class="small" style="color:var(--red)"></div>`,
    actions: `<div class="spacer"></div>
      <button class="btn" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-rule">Save</button>`,
  });
}

function termDialog() {
  openModal({
    title: 'Add to dictionary',
    body: `
      <div>
        <label class="field-label" for="d-terms">Words Sway keeps getting wrong</label>
        <textarea class="input" id="d-terms" placeholder="One per line"></textarea>
      </div>
      <div class="small muted">Sent to the transcriber as a hint so it knows to expect them.</div>
      <div id="d-error" class="small" style="color:var(--red)"></div>`,
    actions: `<div class="spacer"></div>
      <button class="btn" data-act="close-modal">Cancel</button>
      <button class="btn primary" data-act="save-terms">Add</button>`,
  });
}

/* ------------------------------------------------------- event delegation */

document.addEventListener('click', async (event) => {
  const target = event.target.closest('[data-act]');
  if (!target) {
    // A click on a history row that missed an action button opens the detail.
    const row = event.target.closest('.row[data-id]');
    if (row) await openEntry(row.dataset.id);
    return;
  }
  const act = target.dataset.act;
  const id = target.dataset.id;

  try {
    // Onboarding owns its own verbs; everything else falls through below.
    if (act.startsWith('ob-')) { await onboardingAction(act, target); return; }

    switch (act) {
      case 'copy': event.stopPropagation(); await copyEntry(id); break;
      case 'delete': event.stopPropagation(); await deleteEntry(id); break;
      case 'delete-all': await confirmDeleteAll(); break;
      case 'confirm-delete-all':
        await post('/api/history/delete-all', { confirm: true });
        closeModal(); await refresh(); toast('Everything deleted');
        break;
      case 'close-modal': closeModal(); break;
      case 'go-history': await go('history'); break;
      case 'go-key': state.settingsTab = 'general'; await go('settings'); break;
      case 'go-voice': state.settingsTab = 'voice'; await go('settings'); break;

      case 'toggle-agent': {
        const on = target.getAttribute('aria-checked') === 'true';
        await post('/api/listening', { listening: !on });
        await refresh();
        break;
      }

      case 'set-key': keyDialog(); break;
      case 'save-key': await saveKey(); break;
      case 'test-key': await testKey(); break;

      case 'add-transform': openTransformSheet(null); break;
      case 'edit-transform': {
        const list = (state.settings && state.settings.transforms) || [];
        const existing = list.find((t) => t.id === target.dataset.id);
        if (existing) openTransformSheet(existing);
        break;
      }
      case 'close-transform-sheet':
        closeTransformSheet();
        await refresh();
        break;
      case 'add-transform-shortcut':
      case 'edit-transform-shortcut': {
        if (!sheetState) break;
        const index = target.dataset.index !== undefined ? Number(target.dataset.index) : -1;
        captureBindingDialog({
          title: index >= 0 ? 'Press the new key, combination, or mouse button' : 'Press a key, combination, or mouse button',
          hint: 'Hold modifiers together for a combo (like Ctrl+Win), add a key for a shortcut (like Ctrl+Alt+P), or click a mouse button (middle, back, or forward).',
          onCaptured: async (spec) => {
            const shortcuts = [...(sheetState.shortcuts || [])];
            if (index >= 0) shortcuts[index] = spec; else shortcuts.push(spec);
            sheetState.shortcuts = shortcuts;
            await saveSheetNow();
            renderTransformSheet();
          },
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
        const list = (state.settings && state.settings.transforms) || [];
        const gone = list.find((t) => t.id === target.dataset.id);
        if (sheetState && gone && sheetState.id === gone.id) closeTransformSheet();
        state.settings = await post('/api/transforms/delete', { id: target.dataset.id });
        await refresh();
        // Deleting is immediate with an undo in the toast, matching how
        // snippets behave -- no confirm dialog anywhere in this app.
        toast(`Deleted “${gone ? gone.name : 'transform'}”`, gone && (async () => {
          await post('/api/transforms/save', gone);
          await refresh();
        }));
        break;
      }
      case 'reset-transforms':
        closeTransformSheet();
        state.settings = await post('/api/transforms/reset', {});
        await refresh();
        toast('Built-in transforms restored');
        break;

      case 'add-trigger': triggerDialog(null); break;
      case 'copy-snippet': {
        const list = (state.settings && state.settings.triggers) || [];
        const snippet = list.find((t) => t.id === target.dataset.id);
        if (!snippet) break;
        try {
          await navigator.clipboard.writeText(snippet.payload);
          toast('Copied to the clipboard');
        } catch {
          // A webview can refuse clipboard access outright. Say so rather
          // than showing a success toast for something that did not happen.
          toast('Could not reach the clipboard');
        }
        break;
      }
      case 'edit-trigger': {
        const triggers = (state.settings && state.settings.triggers) || [];
        triggerDialog(triggers.find((t) => t.id === id));
        break;
      }
      case 'save-trigger': await saveTrigger(target.dataset.id); break;
      case 'delete-trigger':
        state.settings = await post('/api/triggers/delete', { id });
        await refresh(); toast('Trigger deleted');
        break;
      case 'toggle-trigger': {
        const triggers = (state.settings && state.settings.triggers) || [];
        const trigger = triggers.find((t) => t.id === id);
        if (!trigger) break;
        state.settings = await post('/api/triggers/save', { ...trigger, enabled: !trigger.enabled });
        await refresh();
        break;
      }

      case 'add-rule': appRuleDialog(); break;
      case 'save-rule': {
        const error = el('r-error');
        try {
          state.settings = await post('/api/app-rules/save', {
            process: el('r-process').value, style: el('r-style').value,
          });
          closeModal(); await refresh(); toast('Rule saved');
        } catch (err) { error.textContent = err.message; }
        break;
      }
      case 'delete-rule':
        state.settings = await post('/api/app-rules/delete', { process: target.dataset.process });
        await refresh();
        break;

      case 'add-term': termDialog(); break;
      case 'save-terms': {
        const error = el('d-error');
        const terms = el('d-terms').value.split(/[\n,]/).map((t) => t.trim()).filter(Boolean);
        if (!terms.length) { error.textContent = 'Type at least one word.'; break; }
        try {
          await post('/api/dictionary/add', { terms });
          closeModal(); state.dictionary = null; await refresh();
          toast(`Added ${terms.length} term${terms.length === 1 ? '' : 's'}`);
        } catch (err) { error.textContent = err.message; }
        break;
      }
      case 'remove-term':
        await post('/api/dictionary/remove', { terms: [target.dataset.term] });
        state.dictionary = null; await refresh();
        break;

      case 'pin-term':
        await post('/api/dictionary/pin', {
          term: target.dataset.term,
          pinned: !target.dataset.pinned,
        });
        state.dictionary = null; await refresh();
        break;

      case 'export-vocab': {
        const pack = await api('/api/dictionary/export');
        // Shown as text rather than pushed as a download: this runs in a
        // webview where a file save can be refused outright, and a button
        // that silently does nothing is worse than one that hands you the
        // text to copy.
        openModal({
          title: 'Share your vocabulary',
          body: `
            <div class="small muted">Send this to a teammate and have them paste it into Import.
              It carries only the words — not how often you use them, and not which you pinned.</div>
            <textarea class="input mono selectable" id="v-pack" rows="9" readonly>${esc(JSON.stringify(pack, null, 2))}</textarea>`,
          actions: `<div class="spacer"></div>
            <button class="btn" data-act="close-modal">Done</button>
            <button class="btn primary" data-act="copy-pack">${icon('copy')} Copy</button>`,
        });
        break;
      }
      case 'copy-pack':
        try {
          await navigator.clipboard.writeText(el('v-pack').value);
          toast('Copied to the clipboard');
        } catch {
          el('v-pack').select();
          toast('Select it and press Ctrl+C');
        }
        break;

      case 'import-vocab':
        openModal({
          title: 'Import a vocabulary pack',
          body: `
            <div class="small muted">Paste a pack someone exported. Its words are added
              alongside yours, labelled “shared”, and you can remove any of them.</div>
            <textarea class="input mono" id="v-import" rows="9" placeholder='{"version": 1, "terms": ["..."]}'></textarea>
            <div id="v-error" class="small" style="color:var(--red)"></div>`,
          actions: `<div class="spacer"></div>
            <button class="btn" data-act="close-modal">Cancel</button>
            <button class="btn primary" data-act="save-import">Import</button>`,
        });
        break;
      case 'save-import': {
        const error = el('v-error');
        let pack;
        try {
          pack = JSON.parse(el('v-import').value);
        } catch {
          error.textContent = 'That is not valid JSON — paste the whole pack, braces and all.';
          break;
        }
        try {
          const result = await post('/api/dictionary/import', { pack });
          closeModal(); state.dictionary = null; await refresh();
          toast(result.added
            ? `Added ${result.added} term${result.added === 1 ? '' : 's'}`
            : 'You already had every word in that pack');
        } catch (err) { error.textContent = err.message; }
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
            try {
              state.settings = await post('/api/shortcuts/save', { target: bindingTarget, op: 'add', binding: spec });
              toast('Added'); await refresh();
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
          onCaptured: async (spec) => {
            try {
              state.settings = await post('/api/shortcuts/save', {
                target: bindingTarget, op: 'replace', old_binding: oldValue, binding: spec,
              });
              toast('Updated'); await refresh();
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
          toast('Removed');
          await refresh();
        } catch (err) {
          toast(err.message);
        }
        break;
      case 'reset-binding':
        try {
          await post('/api/shortcuts/reset', { target: target.dataset.target });
          toast('Reset to default');
          await refresh();
        } catch (err) {
          toast(err.message);
        }
        break;
      case 'open-data': toast('Your data folder is shown in Settings › Data'); break;
      case 'refresh-diagnostics':
        state.diagnostics = await api('/api/diagnostics');
        await refresh();
        break;

      case 'set': {
        // Toggles carry their state in aria-checked; selects in value.
        if (target.classList.contains('toggle')) {
          await saveSetting(target.dataset.field, target.getAttribute('aria-checked') !== 'true');
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
    await saveSetting(target.dataset.field, target.dataset.numeric ? Number(raw) : (raw || null));
    return;
  }
  const rule = event.target.closest('[data-act="set-rule"]');
  if (rule) {
    state.settings = await post('/api/app-rules/save', {
      process: rule.dataset.process, style: rule.value,
    });
    await refresh();
  }
});

el('scrim').addEventListener('click', closeDrawer);
el('drawer-close').addEventListener('click', closeDrawer);
el('modal-scrim').addEventListener('click', (event) => {
  if (event.target === el('modal-scrim')) closeModal();
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  closeModal();
  closeDrawer();
});

/* --------------------------------------------------------------- routing */

function renderNav() {
  document.querySelectorAll('.nav-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.view === state.view);
    item.onclick = () => go(item.dataset.view);
  });
  // Red is for a count that needs the user (§5). Each badge counts only the
  // problems its own screen can fix, so neither sends the user to the wrong
  // place: Settings owns the API key, Snippets owns phrase conflicts.
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

async function refresh(options = {}) {
  const view = views[state.view];
  const page = el('page');
  try {
    await view.load();
  } catch (err) {
    page.innerHTML = `<div class="banner">${esc(err.message)}</div>`;
    return;
  }
  const active = document.activeElement;
  const selectionStart = active && active.selectionStart;
  page.innerHTML = view.render();
  if (view.bind) view.bind(page);
  renderNav();

  if (options.keepFocus) {
    const field = page.querySelector(options.keepFocus);
    if (field) {
      field.focus();
      if (selectionStart !== null && selectionStart !== undefined) {
        try { field.setSelectionRange(selectionStart, selectionStart); } catch { /* not a text input */ }
      }
    }
  } else {
    el('main').scrollTop = 0;
  }
}

async function go(view) {
  if (!views[view]) return;
  state.view = view;
  closeDrawer();
  await refresh();
}

/* --------------------------------------------------------------- hotkey */

/**
 * Hotkey capture. Derived from the reference's own hotkey screen (§1 4.png):
 * caps go indigo *while physically held* (§5), and the user is asked to
 * observe that rather than being told the hook registered.
 *
 * Only modifier keys are accepted. A hold-to-talk key that is also a
 * character key would type that character into whatever has focus every time
 * the user dictates.
 */
const MODIFIER_CODES = {
  ControlLeft: 'ctrl_l', ControlRight: 'ctrl_r',
  ShiftLeft: 'shift_l', ShiftRight: 'shift_r',
  AltLeft: 'alt_l', AltRight: 'alt_r',
  // Windows does not reliably distinguish left/right Win the way it does
  // ctrl/alt/shift -- KNOWN_KEYS on the Python side only has a single
  // "cmd", not "cmd_l"/"cmd_r". Both codes map to it, or a captured Win key
  // would be sent as a name the server rejects.
  MetaLeft: 'cmd', MetaRight: 'cmd',
  CapsLock: 'caps_lock', F13: 'f13',
};

/** Modifier identification for a *chord* capture: position-based (ctrl/alt/
 * shift/cmd don't vary by keyboard layout), same set flow.hotkey.Chord uses
 * -- left and right count as the one modifier, since "which side" is not
 * part of what a chord means. */
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
 * combination is refused before the round trip, not just after. */
const RESERVED_CHORDS = {
  'ctrl+c': 'used internally to read a selection — pick something else',
  'ctrl+v': 'used internally to paste — pick something else',
  'ctrl+x': 'used internally to read a selection — pick something else',
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

/** The terminal (non-modifier) key of a chord, from a keydown event.
 * event.key -- not event.code -- because event.key reflects what the OS's
 * active keyboard layout actually produces (matching pynput's KeyCode.char
 * server-side), while event.code is physical-position-based and would
 * disagree with it on a non-QWERTY layout. */
function chordTerminalKeyName(event) {
  const key = event.key;
  if (key.length === 1 && /[a-zA-Z0-9]/.test(key)) return key.toLowerCase();
  if (CHORD_NAMED_KEYS[key]) return CHORD_NAMED_KEYS[key];
  const fMatch = /^F([1-9]|1[0-9]|20)$/.exec(key);
  return fMatch ? `f${fMatch[1]}` : null;
}

/** Mouse buttons that may be bound, by MouseEvent.button. Left (0) and
 * right (2) are deliberately absent -- see sway.hotkey.MOUSE_BUTTONS. */
const MOUSE_BUTTON_NAMES = { 1: 'middle', 3: 'back', 4: 'forward' };
const MOUSE_LABELS = { middle: 'Middle Click', back: 'Back Button', forward: 'Forward Button' };

/** A raw binding spec (e.g. "ctrl+cmd", "ctrl_r", "mouse:middle",
 * "ctrl+alt+p") as key caps, for a capture that has not round-tripped
 * through the server yet (which is the only place that already knows how
 * to label a sided hold-key name or a mouse button). */
function specChips(spec, cls) {
  const SIDED = ['ctrl_r', 'ctrl_l', 'alt_r', 'alt_l', 'shift_r', 'shift_l', 'cmd', 'caps_lock', 'f13'];
  const parts = String(spec).split('+');
  return parts.map((p) => {
    let label;
    if (p.startsWith('mouse:')) label = MOUSE_LABELS[p.slice(6)] || p;
    else if (SIDED.includes(p)) label = prettyKey(p);
    else label = chordPartLabel(p);
    return `<span class="cap big ${cls}">${esc(label)}</span>`;
  }).join('');
}

/**
 * One capture dialog for every binding, activation or transform: a bare
 * held key, two or more modifiers held together (a combo), a modifier
 * chord ending in a key, a bare mouse button, or a chord ending in a mouse
 * button -- see sway.hotkey.Binding. Derived from the reference's own
 * hotkey screen (§1 4.png): caps go indigo *while physically held* (§5),
 * and the user is asked to observe that rather than being told the hook
 * registered.
 *
 * Deliberately dumb about what the captured spec *means* -- it just reports
 * whatever gesture was pressed back to `onCaptured`, which persists it
 * however its caller needs to (an activation target's list, or a
 * transform's shortcuts array). The server is the single source of truth
 * for validating and canonicalising it (sway.hotkey.parse_binding); this
 * only needs to build something plausible.
 */
function captureBindingDialog({ title, hint, onCaptured }) {
  let captured = null;
  // Generic (unsided) modifiers currently held, for combo/chord detection.
  const heldGeneric = new Set();
  // How many relevant physical members (modifiers + a held mouse button)
  // are down right now -- purely for the held/hot visual, independent of
  // what has been captured so far.
  let physicalCount = 0;
  let locked = false; // a terminal (key or mouse) has fired: the capture is final

  openModal({
    title,
    body: `
      <div class="well" style="text-align:center;padding:26px">
        <div id="ab-caps" style="display:flex;gap:12px;flex-wrap:wrap;justify-content:center;min-height:76px;align-items:center">
          <span class="cap big capturing"></span>
        </div>
      </div>
      <div class="small muted">${esc(hint)}</div>
      <div id="ab-note" class="small muted" style="min-height:18px"></div>
      <div id="ab-error" class="small" style="color:var(--red);min-height:18px"></div>`,
    actions: `<div class="spacer"></div>
      <button class="btn" data-act="close-modal">Cancel</button>
      <button class="btn primary" id="ab-save" disabled>Save</button>`,
  });

  const capsBox = document.getElementById('ab-caps');
  const note = document.getElementById('ab-note');
  const error = document.getElementById('ab-error');
  const save = document.getElementById('ab-save');

  // A capture that never settles (AltGr quirks, an unmapped media key)
  // should not leave the dialog silently stuck -- name it and invite a
  // retry rather than leaving Save disabled with no explanation.
  const timeout = setTimeout(() => {
    if (!captured) {
      error.textContent = "Couldn't detect a steady combination — try again, or pick a different key.";
    }
  }, 6000);

  const render = () => {
    if (!captured) return;
    capsBox.innerHTML = specChips(captured, physicalCount > 0 ? 'held' : 'hot');
    // Informational only -- Save stays enabled. Windows' shell specially
    // intercepts keystrokes while the physical Win key is held (checking
    // for reserved system shortcuts), which can keep this binding's own
    // synthetic actions (reading a selection, pasting) from reaching the
    // app you're using. Not a reason to block the combination, just to be
    // honest that it is the least dependable modifier to build on.
    note.textContent = captured.split('+').includes('cmd')
      ? "Shortcuts using the Win key can be less reliable — Windows sometimes intercepts them before they reach the app you're using."
      : '';
  };

  const onKeyDown = (event) => {
    if (locked) return;
    const mod = CHORD_MODIFIER_CODES[event.code];
    if (mod) {
      event.preventDefault();
      const wasHeld = heldGeneric.has(mod);
      heldGeneric.add(mod);
      if (!wasHeld) physicalCount += 1;
      // A single modifier held alone captures as that sided key (matches a
      // bare hold binding); two or more captures as a modifier-only combo.
      // Recomputed fresh on every additional press, so the fullest gesture
      // pressed so far is always what is shown and what would be saved.
      captured = heldGeneric.size === 1 ? MODIFIER_CODES[event.code] : CHORD_MODIFIER_ORDER.filter((m) => heldGeneric.has(m)).join('+');
      error.textContent = '';
      save.disabled = false;
      render();
      return;
    }
    const key = chordTerminalKeyName(event);
    if (!key) return; // an unrecognised key: not part of a usable binding, ignore
    event.preventDefault();
    if (heldGeneric.size === 0) {
      error.textContent = 'A shortcut needs at least one modifier, like Ctrl+Alt+P — or hold a single key on its own.';
      return;
    }
    const order = CHORD_MODIFIER_ORDER.filter((m) => heldGeneric.has(m));
    const spec = [...order, key].join('+');
    if (RESERVED_CHORDS[spec]) {
      error.textContent = `${chordLabel(spec)} is ${RESERVED_CHORDS[spec]}.`;
      return;
    }
    error.textContent = '';
    captured = spec;
    locked = true;
    physicalCount += 1;
    save.disabled = false;
    render();
  };

  const onKeyUp = (event) => {
    const mod = CHORD_MODIFIER_CODES[event.code];
    if (!mod) return;
    if (heldGeneric.delete(mod)) physicalCount = Math.max(0, physicalCount - 1);
    render();
  };

  const onMouseDown = (event) => {
    const name = MOUSE_BUTTON_NAMES[event.button];
    if (!name || locked) return;
    event.preventDefault(); // stops the browser's own back/forward/autoscroll behaviour
    const order = CHORD_MODIFIER_ORDER.filter((m) => heldGeneric.has(m));
    captured = order.length ? [...order, `mouse:${name}`].join('+') : `mouse:${name}`;
    locked = true;
    physicalCount += 1;
    error.textContent = '';
    save.disabled = false;
    render();
  };

  const onMouseUp = (event) => {
    const name = MOUSE_BUTTON_NAMES[event.button];
    if (!name) return;
    physicalCount = Math.max(0, physicalCount - 1);
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
  // Torn down whichever way the dialog closes, so the capture handlers cannot
  // outlive it and swallow keystrokes/clicks on the page behind.
  const scrim = document.getElementById('modal-scrim');
  const observer = new MutationObserver(() => {
    if (!scrim.classList.contains('open')) { cleanup(); observer.disconnect(); }
  });
  observer.observe(scrim, { attributes: true, attributeFilter: ['class'] });

  save.onclick = () => {
    if (!captured) return;
    cleanup();
    observer.disconnect();
    closeModal();
    onCaptured(captured);
  };
}

/** Onboarding still only ever rebinds the dictation hold key. */
function hotkeyDialog() {
  const current = primaryBinding('dictation');
  captureBindingDialog({
    title: 'Press the key you want to hold',
    hint: 'Hold-to-talk needs a key you never type with — a Ctrl, Shift, Alt or Win key. '
      + 'Right-hand keys work best, since your left hand keeps using shortcuts.',
    onCaptured: async (spec) => {
      try {
        state.settings = current
          ? await post('/api/shortcuts/save', { target: 'dictation', op: 'replace', old_binding: current.value, binding: spec })
          : await post('/api/shortcuts/save', { target: 'dictation', op: 'add', binding: spec });
        toast('Saved');
        await refresh();
      } catch (err) {
        toast(err.message);
      }
    },
  });
}

/* ----------------------------------------------------------- onboarding */

/*
 * Five phases, matching the reference's rail (§2) with SIGN UP dropped —
 * there are no accounts. The rail's continuous underline carries progress;
 * the phase labels do not light up individually.
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

function onboardScreen({ title, sub, card, actions, back = true }) {
  return `
    ${back && onboarding.step > 0
      ? `<button class="btn ghost onboard-back" data-act="ob-back">${icon('back')} Back</button>`
      : '<div class="onboard-back"></div>'}
    <h1>${title}</h1>
    ${sub ? `<p class="sub">${sub}</p>` : ''}
    ${card ? `<div class="onboard-card">${card}</div>` : ''}
    <div class="onboard-foot" style="width:700px;max-width:100%">${actions}</div>`;
}

async function drawOnboarding() {
  renderRail();
  const body = document.getElementById('onboard-body');
  const step = onboarding.step;

  if (step === 0) {
    body.innerHTML = onboardScreen({
      title: 'Talk. It types.',
      sub: 'Hold one key, say what you mean, let go. Cleaned-up text appears wherever your cursor is — in any application.',
      card: `
        <div style="display:flex;gap:18px;align-items:flex-start">
          <span class="dot ok" style="margin-top:8px"></span>
          <div>
            <div style="font-weight:600">Three things to set up</div>
            <div class="small muted" style="margin-top:6px;line-height:1.8">
              Your microphone · your Groq key · the key you will hold.<br>
              It takes about a minute, and Sway will not ask again.
            </div>
          </div>
        </div>`,
      actions: '<div class="spacer"></div><button class="btn primary" data-act="ob-next">Get started</button>',
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
      sub: 'Sway uses Groq to transcribe. Keys are free, and yours is stored in Windows Credential Manager — never in a file Sway writes.',
      card: key.configured
        ? `<div style="display:flex;align-items:center;gap:12px">
             <span class="dot ok"></span>
             <div style="flex:1">
               <div style="font-weight:600">Key saved</div>
               <div class="small muted mono">${esc(key.masked)}</div>
             </div>
             <button class="btn small" data-act="ob-key">Change</button>
             <button class="btn small" data-act="ob-test">Test</button>
           </div>
           <div id="key-result" style="margin-top:16px"></div>`
        : `<label class="field-label" for="ob-key-input">Paste your key</label>
           <input class="input mono" id="ob-key-input" type="password" placeholder="gsk_…" autocomplete="off" spellcheck="false">
           <div class="small muted" style="margin-top:10px">Get one at console.groq.com — it takes about 30 seconds.</div>
           <div id="ob-key-error" class="small" style="color:var(--red);margin-top:8px"></div>`,
      actions: key.configured
        ? '<div class="spacer"></div><button class="btn primary" data-act="ob-next">Continue</button>'
        : '<div class="spacer"></div><button class="btn primary" data-act="ob-save-key">Save and continue</button>',
    });
    return;
  }

  if (step === 3) {
    const binding = primaryBinding('dictation');
    const value = binding ? binding.value : 'ctrl_r';
    body.innerHTML = onboardScreen({
      title: 'Choose your key',
      sub: 'Hold it while you speak. It should be a key you never type with.',
      card: `
        <div style="font-weight:600;margin-bottom:18px">Does it light up when you hold it?</div>
        <div class="well" style="text-align:center;padding:26px">
          <div id="ob-caps" style="min-height:76px;display:flex;gap:8px;align-items:center;justify-content:center">
            ${specChips(value, 'hot')}
          </div>
        </div>
        <div class="small muted" style="margin-top:14px">
          Press and hold it now. The key turns purple while Sway is receiving it.
        </div>`,
      actions: `<button class="btn ghost" data-act="ob-rebind">${icon('edit')} Use a different key</button>
        <div class="spacer"></div>
        <button class="btn primary" data-act="ob-next">It works</button>`,
    });
    watchHotkeyPreview(value);
    return;
  }

  // Step 4 — the live try-it-now box. The reference frames this inside a fake
  // Slack (§1 5.png); we use a neutral card, because rendering another
  // company's product chrome inside our app would be impersonation.
  const binding = primaryBinding('dictation');
  body.innerHTML = onboardScreen({
    title: 'Try it right here',
    sub: 'Click the box, then hold your key and say a sentence. This is the real thing — the same pipeline you will use everywhere else.',
    card: `
      <div class="small muted" style="margin-bottom:10px">
        Hold ${binding ? capsFromLabel(binding.label) : 'your key'} · say something · let go
      </div>
      <textarea class="input" id="ob-try" style="min-height:120px;font-size:16px"
        placeholder="Your words will appear here…"></textarea>
      <div id="ob-try-state" class="small muted" style="margin-top:12px">Waiting for you…</div>`,
    actions: `<div class="spacer"></div>
      <button class="btn ghost" data-act="ob-finish">Skip</button>
      <button class="btn primary" data-act="ob-finish">Finish</button>`,
  });
  const box = document.getElementById('ob-try');
  if (box) {
    box.focus();
    box.addEventListener('input', () => {
      onboarding.tried = true;
      const words = box.value.trim().split(/\s+/).filter(Boolean).length;
      document.getElementById('ob-try-state').textContent =
        words ? `${words} word${words === 1 ? '' : 's'} — that is Sway working.` : 'Waiting for you…';
    });
  }
}

/**
 * The permissions screen (§4 of docs/ui-derived-screens.md). Windows has no
 * permission to grant, so this does not claim to grant one: it opens the
 * stream, shows the level, and asks the user to observe — exactly the shape
 * of the reference's hotkey test.
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
    // Genuinely zero, so the zero is printed (§6 strategy 2).
    body.innerHTML = onboardScreen({
      title: 'Sway cannot hear anything yet',
      card: `
        ${stat({ label: 'microphones found', value: '0',
                 foot: 'Windows is not reporting any input device.' })}
        <div class="small muted" style="margin-top:18px">
          Plug a microphone in, or turn on microphone access for desktop apps in
          Windows privacy settings, then check again.
        </div>`,
      actions: `<button class="btn" data-act="ob-recheck">Check again</button>
        <div class="spacer"></div>
        <button class="btn ghost" data-act="ob-next">Skip for now</button>`,
    });
    return;
  }

  const defaultDevice = devices.find((d) => d.default) || devices[0];
  body.innerHTML = onboardScreen({
    title: 'Let Sway hear you',
    sub: 'Sway listens only while you hold your key. Nothing is recorded before that, and audio is never stored.',
    card: `
      <div style="font-weight:600;margin-bottom:18px">Say something. Do the bars move?</div>
      <div class="well" style="text-align:center;padding:26px">
        <div id="ob-level">${levelMeter(0)}</div>
        <div class="small muted" style="margin-top:12px">${esc(defaultDevice.name)}</div>
      </div>
      <div class="small muted" style="margin-top:14px">
        ${devices.length} microphone${devices.length === 1 ? '' : 's'} available.
        You can change which one Sway uses in Settings.
      </div>`,
    actions: `<button class="btn ghost" data-act="ob-recheck">Check again</button>
      <div class="spacer"></div>
      <button class="btn" data-act="ob-next">No, it is not working</button>
      <button class="btn primary" data-act="ob-next">Yes</button>`,
  });
  watchLevel();
}

/**
 * Poll the engine's live mic level while a screen is showing a meter.
 * Cancelled whenever the onboarding step changes, so no timer outlives the
 * screen that started it.
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
 * preview: the same purpose watchHotkeyPreview serves onboarding, extended
 * to every configurable activation binding at once. Self-terminating --
 * checked each tick rather than torn down explicitly on navigation, since
 * nothing else in this file currently hooks a per-tab teardown.
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
        // Hands-free is tap-based: there is no "physically held right now"
        // to show on its chip, only the dot below reporting a session in
        // progress, so those two keys are left alone here.
        if (key === 'hands_free' || key === 'hands_free_chord') return;
        const down = key === 'dictation' ? status.hotkey_down === true : !!bindings[key];
        node.querySelectorAll('.cap').forEach((cap) => {
          cap.classList.toggle('held', down);
          cap.classList.toggle('hot', !down);
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
      const error = document.getElementById('ob-key-error');
      const value = (input.value || '').trim();
      if (!value) { error.textContent = 'Paste a key first.'; return; }
      try {
        await post('/api/key', { key: value });
        state.settings = await api('/api/settings');
        onboarding.step += 1;
        await drawOnboarding();
      } catch (err) { error.textContent = err.message; }
      break;
    }
    case 'ob-key': keyDialog(); break;
    case 'ob-test': await testKey(); break;
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
  // already-open window) has to be honoured too -- including a repeat of the
  // hash already showing, which fires no hashchange but must still switch
  // tabs.
  window.addEventListener('hashchange', () => {
    const next = routeFor(location.hash);
    if (next) go(next);
  });

  // Live updates without a manual refresh (D6). Slow enough to be free, fast
  // enough that a dictation shows up before the user wonders why it hasn't.
  setInterval(() => {
    if (state.view === 'home' && !el('modal-scrim').classList.contains('open')) refresh();
  }, 5000);
})();
