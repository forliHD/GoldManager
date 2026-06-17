/* XAUUSD Bot — Dashboard frontend (Block 9, Phase 2/2)
 *
 * Pure vanilla JS + Lightweight-Charts via CDN. Reads the FastAPI backend
 * (Block 9 Phase 1) and subscribes to WebSocket topics for real-time
 * updates. No build pipeline.
 *
 * Hard rules (see AGENTS.md §4j):
 *  - Never store or log plaintext passwords.
 *  - All API calls use credentials: "include" to send the session cookie.
 *  - WebSocket auth via the same cookie (browsers send it automatically).
 *  - Mode toggle is admin-only and always requires a confirmation modal.
 */

(() => {
  'use strict';

  // ----- State -----
  const state = {
    user: null,           // UserSession
    chart: null,          // Lightweight-Charts IChartApi
    candleSeries: null,   // candlestick series
    overlaySeries: [],    // [{name, line, kind, color, style}]
    timeframe: 'M5',
    ws: null,
    wsTopics: new Set(['ticks', 'features', 'decisions', 'orders', 'journal']),
    reconnectAttempt: 0,
    reconnectTimer: null,
    lastEventTime: null,
    currentMode: 'replay',  // 'replay' or 'live'
  };

  // ----- Helpers -----
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  function show(id) { $(id).classList.remove('hidden'); }
  function hide(id) { $(id).classList.add('hidden'); }

  function setText(sel, text) { const el = $(sel); if (el) el.textContent = text; }
  function setHtml(sel, html) { const el = $(sel); if (el) el.innerHTML = html; }

  function fmtNum(n, digits = 2) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return Number(n).toFixed(digits);
  }
  function fmtPct(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    return (Number(n) * 100).toFixed(1) + '%';
  }
  function fmtPnl(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const cls = n > 0 ? 'pos' : n < 0 ? 'neg' : 'muted';
    return `<span class="${cls}">${n > 0 ? '+' : ''}${Number(n).toFixed(2)}R</span>`;
  }
  function fmtTs(s) {
    if (!s) return '—';
    const d = new Date(s);
    return d.toLocaleString('en-GB', { year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }
  function fmtDate(s) {
    if (!s) return '—';
    return s.substring(0, 10);
  }
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ----- API -----
  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: 'include', ...opts });
    if (!res.ok) {
      let body = null;
      try { body = await res.json(); } catch (e) {}
      const err = new Error((body && body.detail) || res.statusText);
      err.status = res.status;
      throw err;
    }
    if (res.status === 204) return null;
    return res.json();
  }

  // ----- Auth -----
  async function login(username, password) {
    const form = new URLSearchParams({ username, password });
    return api('/api/auth/login', { method: 'POST', body: form, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
  }
  async function logout() { try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {} }
  async function me() { return api('/api/auth/me'); }

  async function tryRestoreSession() {
    try {
      const user = await me();
      if (user) { state.user = user; return true; }
    } catch (e) {}
    return false;
  }

  // ----- Login flow -----
  $('#login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const username = $('#login-username').value.trim();
    const password = $('#login-password').value;
    setText('#login-error', '');
    try {
      await login(username, password);
      const user = await me();
      state.user = user;
      onLoginSuccess();
    } catch (e) {
      setText('#login-error', e.status === 401 ? 'Invalid credentials' : `Server error: ${e.message}`);
    }
  });

  function onLoginSuccess() {
    hide('#login');
    show('#app');
    renderUser();
    initChart();
    connectWebSocket();
    activateTab('indicators');
    loadIndicators();
    loadTrades();
    loadBacktestList();
    loadProposals();
    setDefaultDates();
  }

  function renderUser() {
    if (!state.user) return;
    setText('#user-info', state.user.username);
    setText('#user-role', state.user.role);
    // Mode toggle visible only for admin
    if (state.user.role === 'admin') show('#mode-toggle-wrap');
    else hide('#mode-toggle-wrap');
  }

  $('#logout-btn').addEventListener('click', async () => {
    await logout();
    if (state.ws) try { state.ws.close(); } catch (e) {}
    state.user = null;
    hide('#app');
    show('#login');
    $('#login-username').value = '';
    $('#login-password').value = '';
  });

  // ----- Tabs -----
  $$('.tab').forEach(t => {
    t.addEventListener('click', () => activateTab(t.dataset.tab));
  });
  function activateTab(name) {
    $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    const tabs = ['indicators', 'trades', 'backtest', 'reviews', 'proposals'];
    tabs.forEach(n => {
      const el = $('#tab-' + n);
      if (el) el.classList.toggle('hidden', n !== name);
    });
    if (name === 'trades') loadTrades();
    if (name === 'backtest') loadBacktestList();
    if (name === 'reviews') setDefaultDates();
    if (name === 'proposals') loadProposals();
  }

  // ----- Chart -----
  function initChart() {
    const container = $('#chart');
    if (state.chart) { state.chart.remove(); state.chart = null; }
    state.chart = LightweightCharts.createChart(container, {
      layout: { background: { color: '#161b22' }, textColor: '#e6edf3' },
      grid: { vertLines: { color: '#1f242c' }, horzLines: { color: '#1f242c' } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#30363d' },
      rightPriceScale: { borderColor: '#30363d' },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    });
    state.candleSeries = state.chart.addCandlestickSeries({
      upColor: '#3fb950', downColor: '#f85149', borderVisible: false,
      wickUpColor: '#3fb950', wickDownColor: '#f85149',
    });
    new ResizeObserver(() => state.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight })).observe(container);
    state.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    loadChartData();
  }

  async function loadChartData() {
    try {
      const [candles, overlays] = await Promise.all([
        api(`/api/chart/candles?symbol=XAUUSD&timeframe=${state.timeframe}&count=500`),
        api(`/api/chart/overlays?symbol=XAUUSD`),
      ]);
      state.candleSeries.setData(candles.map(c => ({ time: Math.floor(new Date(c.time).getTime() / 1000), open: c.open, high: c.high, low: c.low, close: c.close })));
      applyOverlays(overlays);
    } catch (e) {
      console.error('chart load failed', e);
    }
  }

  function clearOverlays() {
    for (const o of state.overlaySeries) {
      try { state.chart.removeSeries(o.line); } catch (e) {}
    }
    state.overlaySeries = [];
  }

  function applyOverlays(o) {
    clearOverlays();
    // VWAPs
    if (o.vwap) {
      const colors = { utc00: '#1f77b4', utc07: '#ff7f0e', utc12: '#e377c2' };
      for (const [k, v] of Object.entries(o.vwap)) {
        if (v === null || v === undefined) continue;
        const series = state.chart.addLineSeries({ color: colors[k] || '#fff', lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
        // For VWAP we approximate a horizontal-ish line using the latest bar time + a forward time
        const lastTime = state.candleSeries.dataByIndex ? Math.floor(Date.now() / 1000) : Math.floor(Date.now() / 1000);
        series.setData([{ time: lastTime - 3600, value: v }, { time: lastTime + 3600, value: v }]);
        state.overlaySeries.push({ name: 'vwap_' + k, line: series });
      }
    }
    // Volume profile
    if (o.volume_profile) {
      for (const [period, vp] of Object.entries(o.volume_profile)) {
        if (!vp) continue;
        const isDeveloping = vp.state === 'developing';
        const color = period.startsWith('prev_') ? '#ffd700' : (isDeveloping ? '#888' : '#fff');
        const lineWidth = isDeveloping ? 1 : 2;
        const lineStyle = isDeveloping ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid;
        for (const level of ['vah', 'vpoc', 'val']) {
          if (vp[level] === null || vp[level] === undefined) continue;
          const series = state.chart.addLineSeries({ color, lineWidth, lineStyle, priceLineVisible: false, lastValueVisible: false });
          const now = Math.floor(Date.now() / 1000);
          series.setData([{ time: now - 3600, value: vp[level] }, { time: now + 3600, value: vp[level] }]);
          state.overlaySeries.push({ name: 'vp_' + period + '_' + level, line: series });
        }
      }
    }
  }

  $$('#timeframe-selector button').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('#timeframe-selector button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.timeframe = btn.dataset.tf;
      loadChartData();
    });
  });

  $('#chart-refresh').addEventListener('click', () => loadChartData());

  // ----- Indicators tab -----
  async function loadIndicators() {
    try {
      const k = await api('/api/journal/aggregate?period=last_24h');
      renderKpis(k);
    } catch (e) {
      console.error('aggregate load failed', e);
    }
  }
  function renderKpis(k) {
    const grid = $('#kpi-grid');
    grid.innerHTML = '';
    const items = [
      ['Sharpe', fmtNum(k.sharpe)],
      ['Sortino', fmtNum(k.sortino)],
      ['Max DD', fmtPct(k.max_dd)],
      ['Winrate', fmtPct(k.winrate)],
      ['N Trades', String(k.n_trades ?? 0)],
      ['Expectancy (R)', fmtNum(k.expectancy)],
    ];
    for (const [label, value] of items) {
      const div = document.createElement('div');
      div.className = 'kpi';
      div.innerHTML = `<div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div>`;
      grid.appendChild(div);
    }
  }

  // ----- Trades tab -----
  async function loadTrades() {
    try {
      const trades = await api('/api/journal/trades?limit=20&symbol=XAUUSD');
      const tbody = $('#trades-table tbody');
      tbody.innerHTML = '';
      for (const t of trades) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${escapeHtml(fmtTs(t.timestamp_open))}</td><td>${escapeHtml(t.side || '—')}</td><td class="num">${fmtNum(t.entry)}</td><td class="num">${fmtNum(t.exit)}</td><td class="num">${fmtPnl(t.pnl_r)}</td><td>${escapeHtml(t.decision_kind || '—')}</td><td class="num">${fmtNum(t.score, 1)}</td>`;
        tr.addEventListener('click', () => alert(`Trade ${t.id}\n\nObservation: ${t.comment || '—'}`));
        tbody.appendChild(tr);
      }
    } catch (e) { console.error('trades load failed', e); }
  }

  // ----- Backtest tab -----
  $('#bt-wf').addEventListener('change', (e) => {
    const params = $('#bt-wf-params');
    if (params) params.classList.toggle('hidden', !e.target.checked);
  });
  $('#bt-run-btn').addEventListener('click', runBacktest);
  async function runBacktest() {
    const body = {
      start_date: $('#bt-start').value,
      end_date: $('#bt-end').value,
      warmup_bars: parseInt($('#bt-warmup').value, 10),
      max_bars: parseInt($('#bt-max').value, 10),
    };
    if ($('#bt-wf').checked) {
      body.walk_forward = { in_sample_days: parseInt($('#bt-is').value, 10), out_of_sample_days: parseInt($('#bt-oos').value, 10), step_days: parseInt($('#bt-step').value, 10) };
    }
    setText('#bt-status', 'Submitting...');
    try {
      const r = await api('/api/backtest/run', { method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' } });
      pollBacktest(r.task_id);
    } catch (e) { setText('#bt-status', 'Error: ' + e.message); }
  }
  async function pollBacktest(taskId) {
    setText('#bt-status', `Running ${taskId}...`);
    const interval = setInterval(async () => {
      try {
        const s = await api(`/api/backtest/status?task_id=${encodeURIComponent(taskId)}`);
        if (s.status === 'completed') { clearInterval(interval); setText('#bt-status', `Done — Sharpe ${fmtNum(s.result?.stats?.sharpe)}`); loadBacktestList(); }
        else if (s.status === 'failed') { clearInterval(interval); setText('#bt-status', 'Failed: ' + (s.error || 'unknown')); }
        else { setText('#bt-status', `Running ${taskId}... (${s.progress_percent || 0}%)`); }
      } catch (e) { clearInterval(interval); setText('#bt-status', 'Error: ' + e.message); }
    }, 2000);
  }
  async function loadBacktestList() {
    try {
      const items = await api('/api/backtest/list');
      const tbody = $('#bt-history-table tbody');
      tbody.innerHTML = '';
      for (const b of items) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${escapeHtml(fmtTs(b.timestamp))}</td><td>${escapeHtml(b.period)}</td><td class="num">${b.n_trades ?? '—'}</td><td class="num">${fmtNum(b.sharpe)}</td><td>${b.is_overfit ? '<span class="badge warn">overfit</span>' : '<span class="badge ok">ok</span>'}</td>`;
        tbody.appendChild(tr);
      }
    } catch (e) { console.error('backtest list load failed', e); }
  }

  // ----- Reviews tab -----
  function setDefaultDates() {
    const today = new Date();
    const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
    const lastMon = new Date(today); lastMon.setDate(today.getDate() - ((today.getDay() + 6) % 7) - 7);
    if (!$('#rv-daily').value) $('#rv-daily').value = yesterday.toISOString().substring(0, 10);
    if (!$('#rv-weekly').value) $('#rv-weekly').value = lastMon.toISOString().substring(0, 10);
  }
  $('#rv-load-daily').addEventListener('click', () => loadReview('daily'));
  $('#rv-load-weekly').addEventListener('click', () => loadReview('weekly'));
  async function loadReview(kind) {
    const param = kind === 'daily' ? $('#rv-daily').value : $('#rv-weekly').value;
    const path = kind === 'daily' ? `/api/review/daily?day=${encodeURIComponent(param)}` : `/api/review/weekly?week_start=${encodeURIComponent(param)}`;
    const out = $('#rv-output');
    out.innerHTML = '<div class="muted">Loading...</div>';
    try {
      const r = await api(path);
      let sufficiency = '<span class="badge muted">—</span>';
      if (r.data_sufficiency === 'sufficient') sufficiency = '<span class="badge ok">sufficient</span>';
      else if (r.data_sufficiency === 'marginal') sufficiency = '<span class="badge warn">marginal</span>';
      else if (r.data_sufficiency === 'insufficient') sufficiency = '<span class="badge err">insufficient</span>';
      const proposals = (r.proposals || []).map(p => `
        <tr>
          <td>${escapeHtml(String(p.proposal_number))}</td>
          <td>${escapeHtml(p.category || '—')}</td>
          <td>${escapeHtml(p.observation || '—')}</td>
          <td>${escapeHtml(p.hypothesis || '—')}</td>
          <td>${escapeHtml(p.overfitting_risk || '—')}</td>
        </tr>
      `).join('');
      out.innerHTML = `
        <div class="form-section">
          <h4 style="margin:0 0 6px;">${kind === 'daily' ? 'Daily' : 'Weekly'} Review — ${escapeHtml(param)}</h4>
          <p>${sufficiency} <span class="muted">data sufficiency</span></p>
          <p>${escapeHtml(r.overall_assessment || '—')}</p>
          ${proposals ? `<table><thead><tr><th>#</th><th>Cat</th><th>Observation</th><th>Hypothesis</th><th>Risk</th></tr></thead><tbody>${proposals}</tbody></table>` : ''}
        </div>
      `;
    } catch (e) { out.innerHTML = `<div class="badge err">Error: ${escapeHtml(e.message)}</div>`; }
  }

  // ----- Proposals tab -----
  $('#fp-refresh').addEventListener('click', loadProposals);
  $('#fp-status-filter').addEventListener('change', loadProposals);
  async function loadProposals() {
    const status = $('#fp-status-filter').value;
    try {
      const items = await api('/api/fitting-proposal/list', { method: 'POST', body: JSON.stringify({ status: status ? [status] : null }), headers: { 'Content-Type': 'application/json' } });
      const tbody = $('#fp-table tbody');
      tbody.innerHTML = '';
      for (const p of items) {
        const isTerminal = p.status === 'approved' || p.status === 'rejected';
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${escapeHtml(fmtDate(p.period_start))}</td>
          <td>${escapeHtml(String(p.proposal_number))}</td>
          <td>${escapeHtml(p.category || '—')}</td>
          <td>${escapeHtml(p.overfitting_risk || '—')}</td>
          <td><span class="badge ${p.status === 'approved' ? 'ok' : p.status === 'rejected' ? 'err' : 'muted'}">${escapeHtml(p.status)}</span></td>
          <td>${escapeHtml(p.hypothesis || '—')}</td>
          <td>
            <button class="cell-btn ok" data-id="${escapeHtml(p.id)}" data-action="approve" ${isTerminal ? 'disabled' : ''}>Approve</button>
            <button class="cell-btn bad" data-id="${escapeHtml(p.id)}" data-action="reject" ${isTerminal ? 'disabled' : ''}>Reject</button>
            <button class="cell-btn" data-id="${escapeHtml(p.id)}" data-action="validate" ${isTerminal ? 'disabled' : ''}>Validate</button>
          </td>
        `;
        tbody.appendChild(tr);
      }
      // Wire actions
      $$('#fp-table button').forEach(btn => {
        btn.addEventListener('click', () => proposalAction(btn.dataset.id, btn.dataset.action));
      });
    } catch (e) { console.error('proposals load failed', e); }
  }
  async function proposalAction(id, action) {
    const operator = state.user?.username || 'unknown';
    const note = prompt(`${action} proposal ${id}\n\nAdd a note:`) || '';
    try {
      if (action === 'approve') await api('/api/fitting-proposal/approve', { method: 'POST', body: JSON.stringify({ proposal_id: id, operator, note }), headers: { 'Content-Type': 'application/json' } });
      else if (action === 'reject') await api('/api/fitting-proposal/reject', { method: 'POST', body: JSON.stringify({ proposal_id: id, operator, note }), headers: { 'Content-Type': 'application/json' } });
      else if (action === 'validate') await api('/api/fitting-proposal/validate', { method: 'POST', body: JSON.stringify({ proposal_id: id }), headers: { 'Content-Type': 'application/json' } });
      loadProposals();
    } catch (e) { alert(`Error: ${e.message}`); }
  }

  // ----- Mode toggle -----
  $('#mode-toggle-btn').addEventListener('click', () => {
    const target = state.currentMode === 'replay' ? 'live' : 'replay';
    showConfirmModal(
      'Switch Mode',
      `Switching to <b>${target}</b> mode affects the trading process. This action is logged. Are you sure?`,
      () => doModeToggle(target)
    );
  });
  async function doModeToggle(target) {
    try {
      const r = await api('/api/mode/toggle', { method: 'POST', body: JSON.stringify({ target_mode: target, confirm: true }), headers: { 'Content-Type': 'application/json' } });
      if (r.mode) state.currentMode = r.mode;
      renderModePill();
    } catch (e) { alert('Mode toggle failed: ' + e.message); }
  }
  function renderModePill() {
    const pill = $('#mode-pill');
    pill.textContent = state.currentMode;
    pill.className = 'pill ' + state.currentMode;
  }
  async function loadMode() {
    try {
      const r = await api('/api/health');
      if (r.connector_mode) state.currentMode = r.connector_mode;
      renderModePill();
    } catch (e) {}
  }

  // ----- Modal -----
  function showConfirmModal(title, body, onConfirm) {
    setText('#modal-title', title);
    setHtml('#modal-body', body);
    show('#modal-bg');
    const confirm = $('#modal-confirm');
    const cancel = $('#modal-cancel');
    const cleanup = () => { hide('#modal-bg'); confirm.onclick = null; cancel.onclick = null; };
    confirm.onclick = () => { cleanup(); onConfirm(); };
    cancel.onclick = cleanup;
  }

  // ----- WebSocket -----
  function connectWebSocket() {
    if (state.ws) try { state.ws.close(); } catch (e) {}
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws`;
    setText('#ws-text', 'WS: connecting...');
    setWsDot(false);
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => {
      state.reconnectAttempt = 0;
      setText('#ws-text', `WS: connected (${state.wsTopics.size} topics)`);
      setText('#ws-topics', [...state.wsTopics].join(' · '));
      setWsDot(true);
      // Subscribe to all topics
      for (const topic of state.wsTopics) ws.send(JSON.stringify({ action: 'subscribe', topic }));
    };
    ws.onmessage = (ev) => {
      state.lastEventTime = new Date();
      setText('#ws-last', 'last event: ' + state.lastEventTime.toLocaleTimeString());
      try {
        const msg = JSON.parse(ev.data);
        handleWsEvent(msg);
      } catch (e) {}
    };
    ws.onerror = () => setWsDot(false);
    ws.onclose = () => {
      setWsDot(false);
      setText('#ws-text', 'WS: disconnected, reconnecting...');
      const delay = Math.min(30000, 1000 * Math.pow(2, state.reconnectAttempt++));
      state.reconnectTimer = setTimeout(connectWebSocket, delay);
    };
  }
  function setWsDot(ok) { const d = $('#ws-dot'); d.classList.toggle('ok', ok); d.classList.toggle('err', !ok); }

  function handleWsEvent(msg) {
    if (!msg || !msg.topic) return;
    const t = msg.topic;
    const d = msg.data || {};
    if (t === 'ticks') {
      // Update last candle close
      if (d.close && state.candleSeries) {
        const now = Math.floor(Date.now() / 1000);
        try { state.candleSeries.update({ time: now, close: d.close }); } catch (e) {}
      }
    } else if (t === 'features') {
      // Re-fetch overlays
      api('/api/chart/overlays?symbol=XAUUSD').then(applyOverlays).catch(e => {});
    } else if (t === 'decisions') {
      renderLastDecision(d);
    } else if (t === 'orders' || t === 'journal') {
      // Re-fetch trades
      if (!$('#tab-trades').classList.contains('hidden')) loadTrades();
    }
  }
  function renderLastDecision(d) {
    const el = $('#last-decision');
    if (!d || (!d.decision_kind && !d.score)) { el.innerHTML = '<span class="muted">—</span>'; return; }
    el.innerHTML = `
      <div><b>Kind:</b> ${escapeHtml(d.decision_kind || '—')}</div>
      <div><b>Side:</b> ${escapeHtml(d.side || '—')}</div>
      <div><b>Score:</b> ${fmtNum(d.score, 1)}</div>
      <div><b>Confidence:</b> ${d.confidence ? Math.round(d.confidence * 100) + '%' : '—'}</div>
      <div><b>Entry type:</b> ${escapeHtml(d.entry_type || '—')}</div>
      <div><b>Comment:</b> ${escapeHtml(d.comment || '—')}</div>
      <div class="muted" style="margin-top:6px;font-size:11px">${escapeHtml(fmtTs(d.timestamp))}</div>
    `;
  }

  // ----- Boot -----
  (async function init() {
    if (await tryRestoreSession()) { onLoginSuccess(); loadMode(); }
    else show('#login');
  })();

})();
