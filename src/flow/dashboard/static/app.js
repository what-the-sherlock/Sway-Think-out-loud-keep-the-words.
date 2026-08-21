/* Flow dashboard.
 *
 * Plain ES modules-free JavaScript against the local JSON API. No framework
 * and no build step, for the same reason the CSS has none: the whole surface
 * is five views over a dozen read-only endpoints, and a toolchain would be
 * more machinery than product.
 *
 * The rule that shapes almost every render function here: a value the engine
 * never measured arrives as null and must render as "Not measured" or an
 * em-dash, never as 0. Zero is a real measurement and must stay
 * distinguishable from an absent one. `fmt.*` below all return the em-dash
 * for null, which is why they are used everywhere instead of raw values.
 */

'use strict';

// The token authenticates this browser session; it arrives in the URL the
// engine opened. Kept in memory and stripped from the visible address bar so
// it does not end up in a screenshot or a pasted link.
const TOKEN = new URLSearchParams(location.search).get('token') || '';
history.replaceState({}, '', location.pathname);

const state = {
  view: 'overview',
  range: '7d',
  overview: null,
  performance: null,
  activity: null,
  query: '',
  status: null,
  heatMetric: 'words',
};

/* ---------- api ---------- */

async function api(path, params = {}) {
  const url = new URL(path, location.origin);
  url.searchParams.set('token', TOKEN);
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v);
  }
  const res = await fetch(url, { headers: { 'X-Flow-Token': TOKEN } });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `request failed (${res.status})`);
  }
  return res.json();
}

async function post(path, payload) {
  const url = new URL(path, location.origin);
  url.searchParams.set('token', TOKEN);
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Flow-Token': TOKEN },
    body: JSON.stringify(payload || {}),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `request failed (${res.status})`);
  }
  return res.json();
}

/* ---------- formatting ---------- */

const DASH = '—';

const fmt = {
  // Every one of these returns DASH for null. See the module comment.
  int(v) { return v === null || v === undefined ? DASH : Math.round(v).toLocaleString(); },
  dec(v, places = 0) {
    return v === null || v === undefined ? DASH : v.toFixed(places);
  },
  ms(v) { return v === null || v === undefined ? DASH : `${Math.round(v)}ms`; },
  pct(v, places = 0) {
    return v === null || v === undefined ? DASH : `${(v * 100).toFixed(places)}%`;
  },
  duration(seconds) {
    if (seconds === null || seconds === undefined) return DASH;
    const total = Math.max(0, Math.round(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h) return `${h}h ${m}m`;
    if (m) return `${m}m`;
    return `${s}s`;
  },
  clock(epoch) {
    const d = new Date(epoch * 1000);
    const today = new Date();
    const sameDay = d.toDateString() === today.toDateString();
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return sameDay ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
  },
  app(name) {
    if (!name) return 'Unknown';
    // Process names are what the OS gives us ("code.exe"); show the readable
    // stem but never invent a friendly name we cannot verify.
    return name.replace(/\.exe$/i, '');
  },
};

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* ---------- small components ---------- */

function metric({ label, value, unit, foot, estimate, hero, unavailable }) {
  const valueHtml = unavailable
    ? `<div class="metric-value unavailable">${esc(unavailable)}</div>`
    : `<div class="metric-value">${esc(value)}${unit ? `<span class="metric-unit">${esc(unit)}</span>` : ''}</div>`;
  return `
    <div class="metric${hero ? ' hero' : ''}">
      <div class="metric-label">${esc(label)}${estimate ? '<span class="est">est</span>' : ''}</div>
      ${valueHtml}
      ${foot ? `<div class="metric-foot">${esc(foot)}</div>` : ''}
    </div>`;
}

function emptyState(title, body) {
  return `<div class="empty"><div class="empty-title">${esc(title)}</div><div class="empty-body">${esc(body)}</div></div>`;
}

function card(title, inner, right = '') {
  return `<div class="card">
    <div class="card-title">${esc(title)}${right ? `<span class="spacer"></span>${right}` : ''}</div>
    ${inner}
  </div>`;
}

function segmented(id, options, active) {
  return `<div class="segmented" id="${id}">${options
    .map(([v, l]) => `<button data-value="${v}" class="${v === active ? 'active' : ''}">${esc(l)}</button>`)
    .join('')}</div>`;
}

/* ---------- charts ---------- */

/* A bar chart drawn as inline SVG. Days with no activity are drawn as a faint
 * baseline rather than omitted, so a gap in usage looks like a gap. */
function barChart(buckets, key) {
  if (!buckets.length) return emptyState('No activity yet', 'Your dictations will appear here.');
  const W = 100, H = 30, gap = 0.6;
  const max = Math.max(...buckets.map((b) => b[key]), 1);
  const slot = W / buckets.length;
  const bars = buckets.map((b, i) => {
    const value = b[key] || 0;
    const h = value > 0 ? Math.max(0.7, (value / max) * (H - 6)) : 0.7;
    const x = i * slot + gap / 2;
    const label = `${b.day}: ${value.toLocaleString()} ${key}`;
    return `<rect class="bar${value ? '' : ' empty'}" x="${x.toFixed(2)}" y="${(H - h).toFixed(2)}"
      width="${(slot - gap).toFixed(2)}" height="${h.toFixed(2)}" rx="0.4"><title>${esc(label)}</title></rect>`;
  }).join('');
  const first = buckets[0].day.slice(5), last = buckets[buckets.length - 1].day.slice(5);
  return `
    <svg class="chart" viewBox="0 0 ${W} ${H + 5}" preserveAspectRatio="none" style="height:132px">
      ${bars}
      <line class="axis" x1="0" y1="${H}" x2="${W}" y2="${H}" vector-effect="non-scaling-stroke"/>
    </svg>
    <div class="row small muted" style="justify-content:space-between;margin-top:6px">
      <span>${esc(first)}</span><span>peak ${max.toLocaleString()}</span><span>${esc(last)}</span>
    </div>`;
}

/* GitHub-style heatmap, laid out in week columns. */
function heatmap(buckets, metricKey) {
  if (!buckets.length) return emptyState('No activity yet', 'Days you dictate will fill in here.');
  const values = buckets.map((b) => b[metricKey] || 0);
  const max = Math.max(...values, 1);
  const level = (v) => (v <= 0 ? 0 : Math.min(4, Math.ceil((v / max) * 4)));

  // Pad the front so the first column starts on a Sunday and the calendar
  // reads as weeks rather than an arbitrary 7-row wrap.
  const lead = new Date(buckets[0].day + 'T00:00:00').getDay();
  const cells = [...Array(lead).fill(null), ...buckets];
  let html = '<div class="heat">';
  for (let i = 0; i < cells.length; i += 7) {
    html += '<div class="heat-col">';
    for (const b of cells.slice(i, i + 7)) {
      if (!b) { html += '<div class="heat-cell" style="visibility:hidden"></div>'; continue; }
      const v = b[metricKey] || 0;
      html += `<div class="heat-cell l${level(v)}" title="${esc(b.day)}: ${v.toLocaleString()} ${metricKey}"></div>`;
    }
    html += '</div>';
  }
  return html + '</div>';
}

/* ---------- views ---------- */

const views = {};

views.overview = {
  title: 'Overview',
  sub: () => "Today's flow, and how it compares.",
  toolbar: () => segmented('range-picker', [['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['all', 'All time']], state.range),
  async load() { state.overview = await api('/api/overview', { range: state.range }); },
  render() {
    const d = state.overview;
    if (!d.has_data) {
      return emptyState(
        'No dictations yet',
        `Hold your dictation key and speak. Once you do, this page shows what you said, how fast, and how much typing it saved.`
      );
    }
    const t = d.today, w = d.window, s = d.streaks;
    const rangeLabel = { '7d': 'last 7 days', '30d': 'last 30 days', '90d': 'last 90 days', all: 'all time' }[d.range];

    const hero = metric({
      label: `Time saved · ${rangeLabel}`,
      value: fmt.duration(w.time_saved_s),
      foot: `Estimated against typing at ${d.typing_wpm} WPM. Change this in Settings.`,
      estimate: true,
      hero: true,
    });

    const todayCards = [
      metric({ label: 'Words today', value: fmt.int(t.words) }),
      metric({ label: 'Sessions today', value: fmt.int(t.sessions) }),
      metric({
        label: 'Avg WPM today',
        ...(t.avg_wpm === null
          ? { unavailable: 'Not measured' }
          : { value: fmt.dec(t.avg_wpm, 0), foot: `across ${t.measured_sessions} measured session${t.measured_sessions === 1 ? '' : 's'}` }),
      }),
      metric({
        label: 'Median latency',
        ...(t.median_latency_ms === null
          ? { unavailable: 'No runs today' }
          : { value: fmt.ms(t.median_latency_ms), foot: 'hotkey release to paste' }),
      }),
      metric({
        label: 'Current streak',
        value: fmt.int(s.current),
        unit: s.current === 1 ? 'day' : 'days',
        foot: `longest ${s.longest}`,
      }),
    ];

    const wpmNote = w.measured_sessions === 0
      ? `<div class="small muted" style="margin-top:10px">Speaking rate isn't available for these sessions — it was only recorded from this version onward.</div>`
      : '';

    const apps = d.apps.length
      ? `<table class="data"><tbody>${d.apps.map((a) => `
          <tr>
            <td class="primary">${esc(fmt.app(a.app))}</td>
            <td style="width:45%">
              <div class="wf-track"><div class="wf-fill" style="width:${(a.share * 100).toFixed(1)}%"></div></div>
            </td>
            <td class="num">${fmt.pct(a.share)}</td>
            <td class="num muted">${fmt.int(a.words)} w</td>
          </tr>`).join('')}</tbody></table>`
      : emptyState('No application data', 'Flow records which app received each dictation.');

    return `
      <div class="stack">
        <div class="grid cols-6">${hero}${todayCards.join('')}</div>

        ${card(`Activity · ${rangeLabel}`,
          barChart(d.daily, 'words') + wpmNote,
          `<span class="small muted">${fmt.int(w.words)} words · ${fmt.int(w.sessions)} sessions</span>`)}

        <div class="grid cols-2">
          ${card('Where you use voice', apps)}
          ${card('Consistency',
            heatmap(d.daily, state.heatMetric) + `
            <div class="grid cols-3" style="margin-top:14px">
              ${metric({ label: 'Current streak', value: fmt.int(s.current), unit: 'd' })}
              ${metric({ label: 'Longest streak', value: fmt.int(s.longest), unit: 'd' })}
              ${metric({ label: 'Active days', value: fmt.int(s.active_days), foot: `of ${s.span_days} since first use` })}
            </div>`,
            segmented('heat-metric', [['words', 'Words'], ['sessions', 'Sessions']], state.heatMetric))}
        </div>
      </div>`;
  },
  bind(root) {
    // Only the in-content control. The topbar's range picker is bound by
    // refresh() for every view.
    bindSegmented(root, 'heat-metric', async (value) => { state.heatMetric = value; await refresh(); });
  },
};

views.activity = {
  title: 'Activity',
  sub: () => (state.activity ? `${state.activity.total} session${state.activity.total === 1 ? '' : 's'} recorded` : ''),
  toolbar: () => `<input class="input" id="search" placeholder="Search dictations…" value="${esc(state.query)}" style="width:260px">`,
  async load() { state.activity = await api('/api/activity', { q: state.query, limit: 200 }); },
  render() {
    const rows = state.activity.sessions;
    if (!rows.length) {
      return state.query
        ? emptyState('No matches', `Nothing in your history matches “${state.query}”.`)
        : emptyState('Your first dictation will appear here', 'Every session is recorded locally with its text, timing, and the app it went to.');
    }
    return card('Sessions', `
      <table class="data">
        <thead><tr>
          <th>When</th><th>App</th><th>Text</th>
          <th style="text-align:right">Words</th>
          <th style="text-align:right">WPM</th>
          <th style="text-align:right">Latency</th>
          <th>Status</th>
        </tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="clickable" data-id="${esc(r.id)}">
            <td class="mono" style="white-space:nowrap">${esc(fmt.clock(r.created_at))}</td>
            <td>${esc(fmt.app(r.app))}</td>
            <td class="truncate primary">${esc(r.preview)}</td>
            <td class="num">${fmt.int(r.word_count)}</td>
            <td class="num">${r.wpm === null ? DASH : fmt.dec(r.wpm, 0)}</td>
            <td class="num">${fmt.ms(r.latency_ms)}</td>
            <td>${statusPill(r)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
      <div class="small muted" style="margin-top:10px">Click a session to see its full text and latency breakdown.</div>
    `);
  },
  bind(root) {
    const search = root.querySelector('#search') || document.querySelector('#search');
    if (search) {
      let timer;
      search.addEventListener('input', (e) => {
        clearTimeout(timer);
        const value = e.target.value;
        // Debounced: a LIKE scan per keystroke on a long history would make
        // typing feel laggy for no benefit.
        timer = setTimeout(async () => { state.query = value; await refresh({ keepFocus: '#search' }); }, 180);
      });
    }
    root.querySelectorAll('tr[data-id]').forEach((tr) => {
      tr.addEventListener('click', () => openSession(tr.dataset.id));
    });
  },
};

views.insights = {
  title: 'Insights',
  sub: () => 'How your speaking rate is trending.',
  toolbar: () => segmented('range-picker', [['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['all', 'All time']], state.range),
  async load() { state.overview = await api('/api/overview', { range: state.range }); },
  render() {
    const d = state.overview;
    const measured = d.daily.filter((b) => b.avg_wpm !== null);
    const w = d.window, all = d.all_time;

    if (!measured.length) {
      return `<div class="stack">
        ${card('Your speed', emptyState(
          'Speaking rate not measured yet',
          'WPM is recorded from this version of Flow onward. Dictate a few times and your speed, trend, and personal best will appear here.'
        ))}
        ${card('What you have so far', `<div class="grid cols-3">
          ${metric({ label: 'Words all time', value: fmt.int(all.words) })}
          ${metric({ label: 'Sessions all time', value: fmt.int(all.sessions) })}
          ${metric({ label: 'Median latency', ...(all.median_latency_ms === null ? { unavailable: 'No data' } : { value: fmt.ms(all.median_latency_ms) }) })}
        </div>`)}
      </div>`;
    }

    const best = Math.max(...measured.map((b) => b.avg_wpm));
    // Compare against the user's own history only. There is no reliable
    // external WPM benchmark to rank them against, so we do not invent one.
    const recent = measured[measured.length - 1].avg_wpm;
    const baseline = all.avg_wpm;
    const delta = baseline ? (recent - baseline) / baseline : null;
    const trendText = delta === null ? 'No baseline yet.'
      : `You're ${Math.abs(delta * 100).toFixed(0)}% ${delta >= 0 ? 'faster' : 'slower'} than your all-time average.`;

    return `<div class="stack">
      <div class="grid cols-4">
        ${metric({ label: 'Most recent day', value: fmt.dec(recent, 0), unit: 'wpm', foot: trendText })}
        ${metric({ label: 'Personal best (daily avg)', value: fmt.dec(best, 0), unit: 'wpm' })}
        ${metric({ label: 'Window average', value: fmt.dec(w.avg_wpm, 0), unit: 'wpm', foot: `${w.measured_sessions} measured sessions` })}
        ${metric({ label: 'All-time average', value: fmt.dec(all.avg_wpm, 0), unit: 'wpm' })}
      </div>
      ${card('Speaking rate by day', barChart(measured.map((b) => ({ day: b.day, wpm: Math.round(b.avg_wpm) })), 'wpm'),
        '<span class="small muted">days with a measurable rate only</span>')}
      ${card('Time saved', `<div class="grid cols-3">
        ${metric({ label: 'This window', value: fmt.duration(w.time_saved_s), estimate: true })}
        ${metric({ label: 'All time', value: fmt.duration(all.time_saved_s), estimate: true })}
        ${metric({ label: 'Words all time', value: fmt.int(all.words) })}
      </div>
      <div class="small muted" style="margin-top:10px">Estimated by comparing your measured speaking time against typing the same words at ${d.typing_wpm} WPM.</div>`)}
    </div>`;
  },
  // No bind: the range picker lives in the topbar and refresh() binds it
  // centrally. Binding it here too would attach a second handler and run
  // every reload twice.
  bind() {},
};

views.performance = {
  title: 'Performance',
  sub: () => 'Technical health of the voice engine.',
  toolbar: () => segmented('range-picker', [['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['all', 'All time']], state.range),
  async load() { state.performance = await api('/api/performance', { range: state.range }); },
  render() {
    const p = state.performance;
    if (!p.total) {
      return emptyState('No runs recorded yet', 'Once you dictate, this page shows latency percentiles and where the time goes.');
    }
    const l = p.latency;
    const h = p.health;

    const stages = p.stages.length ? `<div class="waterfall">${p.stages.map((s) => `
      <div class="wf-row">
        <div class="wf-name">${esc(s.stage)}</div>
        <div class="wf-track"><div class="wf-fill${s.share > 0.4 ? ' slow' : ''}" style="width:${(s.share * 100).toFixed(1)}%"></div></div>
        <div class="wf-val">${fmt.ms(s.median_ms)}</div>
      </div>`).join('')}</div>
      <div class="small muted" style="margin-top:11px">Median duration per stage, and its share of the total. The widest bar is where your latency budget goes.</div>`
      : emptyState('No stage timings', 'Stage breakdowns are recorded per run.');

    const errors = p.errors.length
      ? `<table class="data"><tbody>${p.errors.map((e) => `
          <tr><td class="primary mono">${esc(e.error)}</td><td class="num">${fmt.int(e.count)}</td></tr>`).join('')}</tbody></table>`
      : emptyState('No failures', `All ${p.total} runs in this window succeeded.`);

    return `<div class="stack">
      <div class="health ${esc(h.status)}">
        <div class="orb"></div>
        <div>
          <div class="health-headline">${esc(h.headline)}</div>
          <div class="health-detail">${esc(h.detail)}</div>
        </div>
      </div>

      <div class="grid cols-4">
        ${metric({ label: 'Total runs', value: fmt.int(p.total), foot: p.skipped ? `${p.skipped} silent press${p.skipped === 1 ? '' : 'es'} excluded` : '' })}
        ${metric({ label: 'Succeeded', value: fmt.int(p.ok) })}
        ${metric({ label: 'Failed', value: fmt.int(p.failed) })}
        ${metric({ label: 'Error rate', ...(p.error_rate === null ? { unavailable: 'No data' } : { value: fmt.pct(p.error_rate, 1) }) })}
      </div>

      ${card('Latency distribution', `<div class="grid cols-6">
        ${metric({ label: 'p50', value: fmt.ms(l.p50) })}
        ${metric({ label: 'p95', value: fmt.ms(l.p95) })}
        ${metric({ label: 'p99', value: fmt.ms(l.p99) })}
        ${metric({ label: 'Mean', value: fmt.ms(l.mean) })}
        ${metric({ label: 'Min', value: fmt.ms(l.min) })}
        ${metric({ label: 'Max', value: fmt.ms(l.max) })}
      </div>
      <div class="small muted" style="margin-top:11px">Measured from hotkey release to text on screen, across ${fmt.int(l.count)} runs. Target is under 1000ms at p50.</div>`)}

      <div class="grid cols-2">
        ${card('Where the time goes', stages)}
        ${card('Failures', errors)}
      </div>
    </div>`;
  },
  // No bind: the range picker lives in the topbar and refresh() binds it
  // centrally. Binding it here too would attach a second handler and run
  // every reload twice.
  bind() {},
};

views.settings = {
  title: 'Settings',
  sub: () => 'Engine configuration and data.',
  toolbar: () => '',
  async load() { state.status = await api('/api/status'); },
  render() {
    const engine = (state.status && state.status.engine) || {};
    const rows = Object.entries(engine);
    const config = rows.length
      ? `<div class="kv">${rows.map(([k, v]) => `
          <div class="kv-row"><span class="kv-key">${esc(k.replace(/_/g, ' '))}</span><span class="kv-val">${esc(v ?? DASH)}</span></div>`).join('')}</div>`
      : emptyState('Engine not attached', 'Open the dashboard from the Flow tray icon to see live engine settings.');

    return `<div class="stack">
      ${card('Engine', config)}
      ${card('Editing settings', `
        <div class="small muted" style="line-height:1.7">
          Settings are read from <span class="mono">config.toml</span> and <span class="mono">.env</span> in your Flow data directory.
          Editing them from this page isn't wired up yet — this build reports configuration rather than changing it,
          so nothing here can put the engine into a state the config file disagrees with.
        </div>`)}
      ${card('Privacy', `
        <div class="small" style="line-height:1.8;color:var(--text-dim)">
          <div><strong style="color:var(--text)">Audio is never stored.</strong> Recorded audio lives in an in-memory ring buffer and is discarded after each dictation.</div>
          <div><strong style="color:var(--text)">Text is stored locally only</strong>, in SQLite files in your Flow data directory.</div>
          <div><strong style="color:var(--text)">Sent to Groq:</strong> the trimmed audio clip for transcription, and the transcript text for cleanup. Nothing else leaves the machine.</div>
          <div><strong style="color:var(--text)">This dashboard is loopback-only</strong> and requires a token minted at launch.</div>
        </div>`)}
    </div>`;
  },
  bind() {},
};

/* ---------- session drawer ---------- */

function statusPill(row) {
  if (row.status === 'clipboard_fallback') return '<span class="pill warn">clipboard</span>';
  if (row.status === 'pasted') return '<span class="pill good">pasted</span>';
  return `<span class="pill">${esc(row.kind || 'dictation')}</span>`;
}

async function openSession(id) {
  const drawer = document.getElementById('drawer');
  const scrim = document.getElementById('drawer-scrim');
  const body = document.getElementById('drawer-body');
  drawer.classList.add('open');
  scrim.classList.add('open');
  body.innerHTML = '<div class="empty"><div class="empty-body">Loading…</div></div>';

  let d;
  try {
    d = await api('/api/session', { id });
  } catch (err) {
    body.innerHTML = emptyState('Could not load session', String(err.message));
    return;
  }

  document.getElementById('drawer-title').textContent = fmt.app(d.app);
  document.getElementById('drawer-sub').textContent = fmt.clock(d.created_at);

  const changed = d.raw_text && d.raw_text !== d.text;
  const total = d.stages.reduce((sum, s) => sum + s.duration_ms, 0) || 1;
  const waterfall = d.stages.length
    ? `<div class="waterfall">${d.stages.map((s) => `
        <div class="wf-row">
          <div class="wf-name">${esc(s.stage)}</div>
          <div class="wf-track"><div class="wf-fill${s.duration_ms / total > 0.4 ? ' slow' : ''}" style="width:${((s.duration_ms / total) * 100).toFixed(1)}%"></div></div>
          <div class="wf-val">${fmt.ms(s.duration_ms)}</div>
        </div>`).join('')}</div>`
    : emptyState('No latency breakdown', 'This session was recorded before per-session timings were linked, so its stage timings cannot be matched to it.');

  const meta = [
    ['Words', fmt.int(d.word_count)],
    ['Characters', fmt.int(d.character_count)],
    ['Speaking rate', d.wpm === null ? 'Not measured' : `${fmt.dec(d.wpm, 0)} wpm`],
    ['Speech duration', d.speech_ms === null ? 'Not measured' : fmt.duration(d.speech_ms / 1000)],
    ['Clip duration', d.audio_ms === null ? 'Not measured' : fmt.duration(d.audio_ms / 1000)],
    ['Time saved', d.time_saved_s === null ? 'Not measured' : `${fmt.duration(d.time_saved_s)} (est)`],
    ['Language', d.language || 'Not recorded'],
    ['Application', fmt.app(d.app)],
    ['Felt latency', d.run && d.run.release_to_paste_ms !== null ? fmt.ms(d.run.release_to_paste_ms) : 'Not recorded'],
    ['Status', d.status || 'Not recorded'],
  ];

  body.innerHTML = `
    <div>
      <div class="section-label">Final text — what was inserted</div>
      <div class="text-block">${esc(d.text) || '<span class="muted">empty</span>'}</div>
      <button class="btn" style="margin-top:8px" id="copy-final">Copy</button>
    </div>
    ${changed ? `<div>
      <div class="section-label">Raw transcription — before cleanup</div>
      <div class="text-block dim">${esc(d.raw_text)}</div>
    </div>` : `<div class="small muted">Cleanup made no changes to this transcript.</div>`}
    <div>
      <div class="section-label">Performance</div>
      ${waterfall}
    </div>
    <div>
      <div class="section-label">Metadata</div>
      <div class="kv">${meta.map(([k, v]) => `
        <div class="kv-row"><span class="kv-key">${esc(k)}</span><span class="kv-val">${esc(v)}</span></div>`).join('')}</div>
    </div>`;

  const copy = document.getElementById('copy-final');
  if (copy) {
    copy.addEventListener('click', async () => {
      await navigator.clipboard.writeText(d.text || '');
      toast('Copied to clipboard');
    });
  }
}

function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-scrim').classList.remove('open');
}

/* ---------- chrome ---------- */

function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 1900);
}

function bindSegmented(root, id, handler) {
  const el = root.querySelector(`#${id}`) || document.querySelector(`#${id}`);
  if (!el) return;
  el.querySelectorAll('button').forEach((b) => {
    b.addEventListener('click', () => handler(b.dataset.value));
  });
}

function renderNav() {
  document.querySelectorAll('.nav-item').forEach((b) => {
    b.classList.toggle('active', b.dataset.view === state.view);
  });
  const count = document.getElementById('nav-activity-count');
  if (state.activity) count.textContent = state.activity.total || '';
  const dot = document.getElementById('nav-health-dot');
  if (state.performance && state.performance.health) {
    const s = state.performance.health.status;
    dot.textContent = s === 'excellent' ? '●' : s === 'unknown' ? '' : '●';
    dot.style.color = s === 'excellent' ? 'var(--good)' : s === 'degraded' ? 'var(--warn)' : s === 'problem' ? 'var(--bad)' : '';
  }
}

function renderStatus() {
  const s = state.status;
  const btn = document.getElementById('mic-toggle');
  const label = document.getElementById('mic-label');
  const sub = document.getElementById('mic-sub');
  if (!s || s.listening === null) {
    btn.disabled = true;
    btn.className = 'mic-toggle';
    label.textContent = 'Engine not attached';
    sub.textContent = 'open from the tray icon';
    return;
  }
  btn.disabled = !s.can_toggle;
  btn.className = `mic-toggle ${s.listening ? 'on' : 'off'}`;
  label.textContent = s.listening ? 'Listening' : 'Paused';
  const key = (s.engine && s.engine.hotkey) || 'your hotkey';
  sub.textContent = s.listening ? `hold ${key} to dictate` : 'hotkey disabled';
}

async function refresh(opts = {}) {
  const view = views[state.view];
  const content = document.getElementById('content');
  document.getElementById('page-title').textContent = view.title;

  try {
    await view.load();
  } catch (err) {
    content.innerHTML = emptyState('Could not load data', String(err.message));
    return;
  }

  document.getElementById('page-sub').textContent = view.sub() || '';
  document.getElementById('topbar-right').innerHTML = view.toolbar() || '';
  content.innerHTML = view.render();
  view.bind(content);
  bindSegmented(document.getElementById('topbar-right'), 'range-picker', async (value) => {
    state.range = value;
    await refresh();
  });
  renderNav();

  if (opts.keepFocus) {
    const el = document.querySelector(opts.keepFocus);
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  }
}

async function pollStatus() {
  try {
    state.status = await api('/api/status');
    renderStatus();
  } catch {
    /* The engine may have quit; the page stays useful as a history viewer. */
  }
}

async function go(view) {
  state.view = view;
  renderNav();
  await refresh();
}

/* ---------- boot ---------- */

document.querySelectorAll('.nav-item').forEach((b) => {
  b.addEventListener('click', () => go(b.dataset.view));
});
document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.getElementById('drawer-scrim').addEventListener('click', closeDrawer);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeDrawer();
});

document.getElementById('mic-toggle').addEventListener('click', async () => {
  if (!state.status || state.status.listening === null) return;
  try {
    state.status = await post('/api/listening', { listening: !state.status.listening });
    renderStatus();
    toast(state.status.listening ? 'Listening resumed' : 'Dictation paused');
  } catch (err) {
    toast(`Could not change state: ${err.message}`);
  }
});

pollStatus();
setInterval(pollStatus, 3000);
go('overview');
