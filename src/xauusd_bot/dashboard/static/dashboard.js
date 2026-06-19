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
    overlaySeries: [],    // legacy (unused after price-line refactor)
    overlayLines: [],     // VWAP / value-area priceLine handles
    tradeLines: [],       // open-position entry/SL/TP priceLine handles
    timeframe: 'M5',
    symbol: 'XAUUSD',     // configured trading symbol, fetched from /api/health
    lastCandle: null,     // active-timeframe candle being live-updated
    chartFrom: null,      // first loaded candle time (for overlay line spans)
    chartTo: null,        // latest candle time (extended by live updates)
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
  // Realized P/L in account currency (EUR), signed + coloured.
  function fmtEur(n) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    const cls = n > 0 ? 'pos' : n < 0 ? 'neg' : 'muted';
    const v = Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return `<span class="${cls}">${n > 0 ? '+' : ''}${v} €</span>`;
  }
  function fmtTs(s) {
    if (!s) return '—';
    const d = new Date(s);
    // Show in UTC (= the broker/exchange bar time the chart also displays), NOT
    // the browser's local timezone — otherwise the feed is offset from the chart.
    return d.toLocaleString('en-GB', { timeZone: 'UTC', year: '2-digit', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
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
      await loadMode();
      onLoginSuccess();
    } catch (e) {
      setText('#login-error', e.status === 401 ? 'Invalid credentials' : `Server error: ${e.message}`);
    }
  });

  function onLoginSuccess() {
    hide('#login');
    show('#app');
    renderUser();
    // Never let a chart-library failure (e.g. CDN serving an incompatible
    // version) block the WebSocket and the rest of the app.
    try { initChart(); } catch (e) { console.error('initChart failed:', e); }
    connectWebSocket();
    activateTab('live');
    loadIndicators();
    startLivePolling();
    setupAlerts();
    setupFvgSettings();
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
    // AI toggle + Emergency stop visible for operator + admin
    if (state.user.role === 'admin' || state.user.role === 'operator') {
      show('#ai-toggle-wrap');
      show('#emergency-btn');
      loadAIState();
      loadEmergencyState();
    } else {
      hide('#ai-toggle-wrap');
      hide('#emergency-btn');
    }
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
    const tabs = ['live', 'performance', 'indicators', 'trades', 'backtest', 'reviews', 'proposals'];
    tabs.forEach(n => {
      const el = $('#tab-' + n);
      if (el) el.classList.toggle('hidden', n !== name);
    });
    state.activeTab = name;
    if (name === 'live') loadLive();
    if (name === 'performance') loadPerformance();
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
    // FVG box layer — HTML rectangles positioned via the chart's coordinate API.
    const fvgLayer = document.createElement('div');
    fvgLayer.className = 'fvg-layer';
    container.appendChild(fvgLayer);
    state.fvgLayer = fvgLayer;
    state.chart.timeScale().subscribeVisibleTimeRangeChange(() => positionFvgZones());
    new ResizeObserver(() => {
      state.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      positionFvgZones();
    }).observe(container);
    state.chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    loadChartData();
  }

  async function loadChartData() {
    try {
      const sym = encodeURIComponent(state.symbol);
      const [candles, overlays] = await Promise.all([
        api(`/api/chart/candles?symbol=${sym}&timeframe=${state.timeframe}&count=500`),
        api(`/api/chart/overlays?symbol=${sym}`),
      ]);
      const mapped = candles.map(c => ({ time: Math.floor(new Date(c.time).getTime() / 1000), open: c.open, high: c.high, low: c.low, close: c.close }));
      state.candleSeries.setData(mapped);
      state.lastCandle = mapped.length ? { ...mapped[mapped.length - 1] } : null;
      state.chartFrom = mapped.length ? mapped[0].time : null;
      state.chartTo = mapped.length ? mapped[mapped.length - 1].time : null;
      // Show a consistent, comfortable window — the most recent ~120 bars —
      // instead of fitContent (which stretches few bars into giant candles,
      // worst on H1 with little history). Falls back to "show all" when fewer.
      try {
        const n = mapped.length;
        const visible = 120;
        state.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - visible), to: n + 4 });
      } catch (e) {}
      applyOverlays(overlays);
    } catch (e) {
      console.error('chart load failed', e);
    }
  }

  // Seconds per timeframe bucket, for live candle aggregation.
  const TF_SECONDS = { M1: 60, M5: 300, M15: 900, H1: 3600 };

  // Merge a live bar (closed or forming) into the active timeframe's candle so
  // the chart animates on every timeframe (like MT5's forming candle).
  function liveUpdateCandle(bar) {
    if (!bar || !state.candleSeries) return;
    const tfSec = TF_SECONDS[state.timeframe] || 60;
    const barT = Math.floor(new Date(bar.time).getTime() / 1000);
    if (isNaN(barT)) return;
    const bucketT = Math.floor(barT / tfSec) * tfSec;
    const o = Number(bar.open), h = Number(bar.high), l = Number(bar.low), c = Number(bar.close);
    const last = state.lastCandle;
    let candle;
    if (last && last.time === bucketT) {
      // Same bucket → extend the forming candle (keep open, widen H/L, latest close).
      candle = { time: bucketT, open: last.open, high: Math.max(last.high, h), low: Math.min(last.low, l), close: c };
    } else if (last && bucketT < last.time) {
      return; // stale/out-of-order bar — ignore
    } else {
      // New bucket → start a fresh candle (the M1 open is the bucket open).
      candle = { time: bucketT, open: o, high: h, low: l, close: c };
    }
    state.lastCandle = candle;
    if (state.chartTo == null || bucketT > state.chartTo) state.chartTo = bucketT;
    try { state.candleSeries.update(candle); } catch (e) {}
    // Price autoscale can shift without a time-range change → keep FVG boxes aligned.
    positionFvgZones();
  }

  // Draw a labelled horizontal price line on the candle series (right-axis
  // label + an on-line title). Returns the priceLine handle for later removal.
  function priceLine(price, { color, style, width = 1, title }) {
    if (price == null || isNaN(Number(price)) || !state.candleSeries) return null;
    try {
      return state.candleSeries.createPriceLine({
        price: Number(price), color, lineWidth: width,
        lineStyle: style, axisLabelVisible: true, title,
      });
    } catch (e) { return null; }
  }

  function clearOverlays() {
    for (const pl of (state.overlayLines || [])) { try { state.candleSeries.removePriceLine(pl); } catch (e) {} }
    state.overlayLines = [];
    state.fvgZones = [];
    positionFvgZones();
  }

  // ---- Fair Value Gap boxes -----------------------------------------------
  // Keep the latest zones and (re)draw them as HTML rectangles over the chart.
  // Per-TF distance scale (gold points): how far a zone of that TF stays
  // relevant — H1 levels matter from afar, M1 only locally.
  const FVG_TF_SCALE = { H1: 80, M5: 30, M1: 12 };
  const FVG_DEFAULTS = { max: 6, tf: { H1: true, M5: true, M1: true }, type: { bullish: true, bearish: true }, partial: true, mode: 'blend' };
  function loadFvgSettings() {
    let s = {};
    try { s = JSON.parse(localStorage.getItem('fvgSettings') || '{}'); } catch (e) {}
    return {
      max: (s.max != null ? s.max : FVG_DEFAULTS.max),
      tf: Object.assign({}, FVG_DEFAULTS.tf, s.tf),
      type: Object.assign({}, FVG_DEFAULTS.type, s.type),
      partial: (s.partial != null ? s.partial : FVG_DEFAULTS.partial),
      mode: s.mode || FVG_DEFAULTS.mode,
    };
  }
  // Wire the gear popover: reflect saved settings into the controls, persist on
  // change, and re-curate the already-fetched zones (no re-fetch, no trading
  // impact — this is display only).
  function setupFvgSettings() {
    const s = state.fvgSettings || (state.fvgSettings = loadFvgSettings());
    const set = (id, prop, val) => { const el = $(id); if (el) el[prop] = val; };
    set('#fvg-max', 'value', s.max);
    set('#fvg-tf-H1', 'checked', s.tf.H1); set('#fvg-tf-M5', 'checked', s.tf.M5); set('#fvg-tf-M1', 'checked', s.tf.M1);
    set('#fvg-bull', 'checked', s.type.bullish); set('#fvg-bear', 'checked', s.type.bearish);
    set('#fvg-partial', 'checked', s.partial);
    set('#fvg-mode', 'value', s.mode);

    const apply = () => {
      s.max = Math.max(0, Math.min(20, parseInt($('#fvg-max').value, 10) || 0));
      s.tf = { H1: $('#fvg-tf-H1').checked, M5: $('#fvg-tf-M5').checked, M1: $('#fvg-tf-M1').checked };
      s.type = { bullish: $('#fvg-bull').checked, bearish: $('#fvg-bear').checked };
      s.partial = $('#fvg-partial').checked;
      s.mode = $('#fvg-mode').value;
      try { localStorage.setItem('fvgSettings', JSON.stringify(s)); } catch (e) {}
      renderFvgZones(state.fvgRawZones || []);  // re-curate, no re-fetch
    };
    $('#fvg-settings').querySelectorAll('input,select').forEach(el => el.addEventListener('change', apply));

    const pop = $('#fvg-settings'), btn = $('#fvg-settings-btn');
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = pop.classList.toggle('hidden');
      btn.classList.toggle('on', !open);
    });
    // Click outside closes it.
    document.addEventListener('click', (e) => {
      if (!pop.classList.contains('hidden') && !pop.contains(e.target) && e.target !== btn) {
        pop.classList.add('hidden'); btn.classList.remove('on');
      }
    });
  }

  // Build the curated set from the raw zones using the user's display settings.
  function renderFvgZones(zones) {
    state.fvgRawZones = Array.isArray(zones) ? zones.slice() : [];  // keep raw for re-filtering on settings change
    const s = state.fvgSettings || (state.fvgSettings = loadFvgSettings());
    const px = (state.lastCandle && Number(state.lastCandle.close)) || null;
    const dist = (x) => {
      const top = Number(x.top), bot = Number(x.bottom);
      if (px == null) return 0;
      if (px > top) return px - top;
      if (px < bot) return bot - px;
      return 0;
    };
    const rank = (x) => Number(x.rank_score || x.size_points || 0) || 0.01;
    const relevance = (x) => {
      if (s.mode === 'strength' || px == null) return rank(x);
      if (s.mode === 'proximity') return 1 / (1 + dist(x));      // nearest first
      return rank(x) / (1 + dist(x) / (FVG_TF_SCALE[x.tf] || 20)); // blend
    };
    const z = state.fvgRawZones.filter(x =>
      s.tf[x.tf] !== false &&
      s.type[x.type] !== false &&
      (s.partial || x.status !== 'partially_mitigated'));
    z.sort((a, b) => relevance(b) - relevance(a));
    const perTf = {};
    const kept = [];
    for (const zone of z) {
      const tf = zone.tf || '?';
      perTf[tf] = (perTf[tf] || 0) + 1;
      if (perTf[tf] <= s.max) kept.push(zone);
    }
    state.fvgZones = kept;
    positionFvgZones();
  }
  function positionFvgZones() {
    const layer = state.fvgLayer, series = state.candleSeries, chart = state.chart;
    if (!layer || !series || !chart) return;
    layer.innerHTML = '';
    const zones = state.fvgZones || [];
    if (!zones.length) return;
    const ts = chart.timeScale();
    let paneW = $('#chart').clientWidth;
    try { paneW -= (chart.priceScale('right').width() || 0); } catch (e) {}
    if (!(paneW > 0)) paneW = $('#chart').clientWidth;
    for (const z of zones) {
      const yTop = series.priceToCoordinate(Number(z.top));
      const yBot = series.priceToCoordinate(Number(z.bottom));
      if (yTop == null || yBot == null) continue;
      const top = Math.min(yTop, yBot), h = Math.max(4, Math.abs(yBot - yTop));
      // Left edge = where the gap formed; clamp into view, extend to the right edge.
      let xl = ts.timeToCoordinate(Math.floor(new Date(z.created_at).getTime() / 1000));
      if (xl == null || xl < 0) xl = 0;          // formed off-screen left → from edge
      if (xl > paneW) continue;                   // formed beyond the view → skip
      const bull = (z.type === 'bullish');
      const partial = (z.status === 'partially_mitigated');
      const col = bull ? '63,185,80' : '248,81,73';
      const box = document.createElement('div');
      box.className = 'fvg-box';
      box.style.left = xl + 'px';
      box.style.top = top + 'px';
      box.style.width = Math.max(2, paneW - xl) + 'px';
      box.style.height = h + 'px';
      box.style.background = `rgba(${col},${partial ? 0.06 : 0.13})`;
      const bstyle = `1px ${partial ? 'dashed' : 'solid'} rgba(${col},0.85)`;
      box.style.borderTop = bstyle; box.style.borderBottom = bstyle;
      const tag = document.createElement('div');
      tag.className = 'fvg-tag';
      tag.textContent = `FVG ${z.tf} ${bull ? '▲' : '▼'}${partial ? ' ◑' : ''}`;
      tag.style.background = `rgba(${col},0.9)`;
      box.appendChild(tag);
      layer.appendChild(box);
    }
  }

  function applyOverlays(o) {
    clearOverlays();
    if (!o || !state.candleSeries) return;
    const S = LightweightCharts.LineStyle;
    const lines = [];
    const add = (...a) => { const pl = priceLine(...a); if (pl) lines.push(pl); };
    // VWAPs — distinct colours, solid, labelled (e.g. "VWAP 12").
    const vwaps = o.vwaps || o.vwap || {};
    const vwCol = { utc00: '#3b82f6', utc07: '#f59e0b', utc12: '#ec4899' };
    for (const k of ['utc00', 'utc07', 'utc12']) {
      if (vwaps[k] != null) add(vwaps[k], { color: vwCol[k], style: S.Solid, width: 1, title: 'VWAP ' + k.slice(3) });
    }
    // Value areas — keep the chart readable: weekly (developing, dotted) +
    // previous week (locked, dashed). VAH red / VPOC yellow / VAL green.
    const vp = o.volume_profile || {};
    const drawVA = (key, label, style, width) => {
      const p = vp[key]; if (!p) return;
      add(p.vah, { color: '#ef4444', style, width, title: label + ' VAH' });
      add(p.vpoc, { color: '#eab308', style, width, title: label + ' VPOC' });
      add(p.val, { color: '#22c55e', style, width, title: label + ' VAL' });
    };
    drawVA('weekly', 'W', S.Dotted, 1);
    drawVA('prev_week', 'pW', S.Dashed, 2);
    state.overlayLines = lines;
    renderFvgZones(o.fvg_zones || o.fvgZones || []);
  }

  // Draw the open position(s) on the chart: entry + SL + TP1/2/3, labelled.
  function clearTradeLines() {
    for (const pl of (state.tradeLines || [])) { try { state.candleSeries.removePriceLine(pl); } catch (e) {} }
    state.tradeLines = [];
  }

  function renderTradeLevels(positions) {
    clearTradeLines();
    if (!positions || !positions.length || !state.candleSeries) return;
    const S = LightweightCharts.LineStyle;
    const lines = [];
    const add = (...a) => { const pl = priceLine(...a); if (pl) lines.push(pl); };
    for (const p of positions) {
      const long = p.side === 'buy';
      add(p.open_price, { color: long ? '#22c55e' : '#ef4444', style: S.Solid, width: 2, title: `${long ? '▲ LONG' : '▼ SHORT'} ${fmtNum(p.open_price)}` });
      add(p.sl, { color: '#ef4444', style: S.Dashed, width: 1, title: 'SL' });
      const pl = p.plan;
      if (pl) {
        add(pl.tp1, { color: '#16a34a', style: S.Dotted, width: 1, title: 'TP1' + (pl.tp1_taken ? ' ✓' : '') });
        add(pl.tp2, { color: '#16a34a', style: S.Dotted, width: 1, title: 'TP2' + (pl.tp2_taken ? ' ✓' : '') });
        add(pl.tp3, { color: '#15803d', style: S.Dotted, width: 1, title: 'TP3' });
      } else if (p.tp != null) {
        add(p.tp, { color: '#16a34a', style: S.Dotted, width: 1, title: 'TP' });
      }
    }
    state.tradeLines = lines;
  }

  $$('#timeframe-selector button').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('#timeframe-selector button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.timeframe = btn.dataset.tf;
      setText('#chart-symbol', `${state.symbol} · ${state.timeframe}`);
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
      const trades = await api(`/api/journal/trades?limit=20&symbol=${encodeURIComponent(state.symbol)}`);
      const tbody = $('#trades-table tbody');
      tbody.innerHTML = '';
      for (const t of trades) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${escapeHtml(fmtTs(t.timestamp_open))}</td><td>${escapeHtml(t.side || '—')}</td><td class="num">${fmtNum(t.entry)}</td><td class="num">${fmtNum(t.exit)}</td><td class="num">${fmtEur(t.pnl_realized)}</td><td class="num">${fmtPnl(t.pnl_r)}</td><td>${escapeHtml(t.decision_kind || '—')}</td><td class="num">${fmtNum(t.score, 1)}</td>`;
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
      if (r.mode) state.currentMode = r.mode;
      if (r.symbol) state.symbol = r.symbol;
      renderModePill();
      setText('#chart-symbol', `${state.symbol} · ${state.timeframe}`);
    } catch (e) {}
  }

  // ----- AI layer toggle -----
  $('#ai-toggle-btn').addEventListener('click', () => doAIToggle(!state.aiEnabled));
  async function doAIToggle(enabled) {
    try {
      const r = await api('/api/ai/toggle', { method: 'POST', body: JSON.stringify({ enabled }), headers: { 'Content-Type': 'application/json' } });
      state.aiEnabled = r.enabled;
      state.aiAvailable = r.available;
      renderAIPill();
    } catch (e) { alert('AI toggle failed: ' + e.message); }
  }
  function renderAIPill() {
    const pill = $('#ai-pill');
    if (!pill) return;
    pill.textContent = state.aiEnabled ? (state.aiAvailable ? 'on' : 'on (no key)') : 'off';
    pill.className = 'pill ' + (state.aiEnabled ? 'on' : 'off');
  }
  async function loadAIState() {
    try {
      const r = await api('/api/ai/state');
      state.aiEnabled = r.enabled;
      state.aiAvailable = r.available;
      renderAIPill();
    } catch (e) {}
  }

  // ----- Live ops cockpit -----
  function fmtMoney(n) {
    return (n === null || n === undefined || isNaN(n)) ? '—'
      : Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  async function loadLive() {
    try {
      const [acc, risk, positions, decisions, orders, health, usage, news, aiLast] = await Promise.all([
        api('/api/account'), api('/api/risk'), api('/api/positions/managed'),
        api('/api/decisions/recent?count=40'), api('/api/orders/recent?count=30'),
        api('/api/health/services'), api('/api/usage'),
        api('/api/news').catch(() => null), api('/api/ai/last').catch(() => null),
      ]);
      renderAccount(acc); renderRisk(risk); renderPositions(positions); renderTradeLevels(positions);
      renderDecisionFeed(decisions); renderOrders(orders); renderServiceHealth(health); renderMarketStatus(health);
      renderUsage(usage); renderNews(news); renderScores(decisions && decisions[0]); renderAILast(aiLast);
      const ts = acc && acc.ts ? new Date(acc.ts) : null;
      const stale = !ts || (Date.now() - ts.getTime() > 20000);
      setText('#live-stale', stale ? '⚠ keine aktuellen Daten — läuft die execution-engine?' : '');
      // Overlay fallback: normally indicators refresh on each `features` WS event,
      // but if the WebSocket is down they'd freeze — so re-fetch them here too.
      if (!state.wsOk) {
        api(`/api/chart/overlays?symbol=${encodeURIComponent(state.symbol)}`).then(applyOverlays).catch(e => {});
      }
    } catch (e) { setText('#live-stale', 'Fehler beim Laden der Live-Daten'); }
  }
  function scoreBadge(score, band) {
    const s = (score == null) ? '—' : Math.round(score);
    const bg = (band && band.startsWith('a_plus')) || score >= 85 ? 'var(--ok)'
      : score >= 65 ? 'var(--warn)' : 'var(--border)';
    const fg = score >= 65 ? '#0e1116' : 'var(--text)';
    return `<span class="sbadge" style="background:${bg};color:${fg}">${s}</span>`;
  }
  function decisionRow(d) {
    const act = (d.action || '—');
    const ai = d.source_ai ? '<span class="ai">AI</span>' : '';
    const why = d.qualified ? `<span class="why">✓ ${escapeHtml(d.entry_type || 'qualified')}</span>`
      : (d.block_reason ? `<span class="why">${escapeHtml(d.block_reason)}</span>` : '');
    return `<div class="frow">${scoreBadge(d.score, d.band)}<span class="act ${act}">${escapeHtml(act)}</span>${ai}
      <span class="muted">${escapeHtml(d.direction || '')}</span>${why}
      <span class="muted" style="font-size:10px">${escapeHtml(fmtTs(d.ts))}</span></div>`;
  }
  function renderDecisionFeed(items) {
    const el = $('#decision-feed'); if (!el) return;
    if (!items || !items.length) { el.innerHTML = '<span class="muted">noch keine Decisions</span>'; return; }
    el.innerHTML = items.map(decisionRow).join('');
  }
  function renderOrders(orders) {
    const tb = $('#orders-table tbody'); if (!tb) return;
    if (!orders || !orders.length) { tb.innerHTML = '<tr><td colspan="6" class="muted">noch keine Orders</td></tr>'; return; }
    tb.innerHTML = '';
    for (const o of orders) {
      const tr = document.createElement('tr');
      const sideCls = o.side === 'buy' ? 'pos-long' : 'pos-short';
      tr.innerHTML = `<td>${escapeHtml(fmtTs(o.ts))}</td><td class="${sideCls}">${escapeHtml((o.side||'').toUpperCase())}</td>
        <td class="num">${fmtNum(o.volume,2)}</td><td class="num">${o.fill_price!=null?fmtNum(o.fill_price):'—'}</td>
        <td class="num">${o.slippage_pips!=null?fmtNum(o.slippage_pips,1):'—'}</td><td>${escapeHtml(o.status||'—')}</td>`;
      tb.appendChild(tr);
    }
  }
  function renderServiceHealth(h) {
    const el = $('#svc-health'); if (!el) return;
    if (!h || !h.redis) { el.innerHTML = '<span class="svc"><span class="sdot"></span>redis down</span>'; return; }
    const parts = [];
    const streams = h.streams || {};
    for (const [topic, s] of Object.entries(streams)) {
      const age = s.last_age_s;
      let cls, note = '';
      if (topic === 'orders') {
        // Orders are sporadic trade events, not a heartbeat — "no recent order"
        // is normal and must NOT show red. Reflect the engine's liveness
        // (state freshness) instead of stream age.
        cls = h.execution_alive ? 'ok' : '';
        note = ' · sporadic (green = engine alive, not order age)';
      } else {
        // Bars/features/decisions publish once per closed M1 bar (~1/min), so a
        // 15s green window flickered to yellow between bars. Widen to the bar
        // cadence: green < 90s (a bar every 60s stays green), yellow < 180s.
        cls = age == null ? '' : age < 90 ? 'ok' : age < 180 ? 'warn' : '';
        // When the market is closed these streams legitimately go stale — don't
        // alarm the user with red; show amber + a note instead.
        if (h.market_status === 'closed' && cls === '') { cls = 'warn'; note = ' · Markt geschlossen'; }
      }
      parts.push(`<span class="svc" title="${escapeHtml(s.service)} · ${topic} · len ${s.len} · ${age==null?'?':age+'s'}${note}"><span class="sdot ${cls}"></span>${escapeHtml(s.service.replace('-engine','').replace('-',''))}</span>`);
    }
    parts.push(`<span class="svc" title="execution-engine state"><span class="sdot ${h.execution_alive?'ok':''}"></span>exec</span>`);
    el.innerHTML = parts.join('');
  }
  // Show a prominent "market closed" / "feed down" pill so a quiet chart and the
  // amber status dots are self-explanatory instead of looking broken.
  function renderMarketStatus(h) {
    const el = $('#market-banner'); if (!el) return;
    const st = h && h.market_status;
    if (st === 'closed') {
      el.className = 'market-pill closed';
      el.textContent = '🌙 Markt geschlossen — aktuell keine neuen Candles';
    } else if (st === 'feed_down') {
      el.className = 'market-pill down';
      el.textContent = '⚠ Daten-Feed offline — Bridge / data-collector prüfen';
    } else {
      el.className = 'market-pill hidden';
    }
  }
  function renderUsage(u) {
    const el = $('#llm-usage'); if (!el) return;
    if (!u || !u.calls) { el.textContent = 'OpenRouter/M3: noch keine Calls'; return; }
    const tok = (u.prompt_tokens || 0) + (u.completion_tokens || 0);
    el.textContent = `OpenRouter/M3: ${u.calls} Calls · ${tok.toLocaleString('en-US')} Tokens · ~$${(u.est_cost_usd || 0).toFixed(4)}`;
  }
  function renderAccount(a) {
    if (!a || a.equity === undefined) { setHtml('#live-account', '<span class="muted">keine Daten</span>'); return; }
    const m = (label, val) => `<div class="metric"><div class="label">${label}</div><div class="value">${val}</div></div>`;
    setHtml('#live-account',
      m('Equity', fmtMoney(a.equity) + ' ' + (a.currency || '')) +
      m('Balance', fmtMoney(a.balance)) +
      m('Free Margin', fmtMoney(a.free_margin)) +
      m('Margin', fmtMoney(a.margin)) +
      m('Leverage', '1:' + (a.leverage ?? '—')) +
      m('Spread', a.current_spread != null ? a.current_spread + ' pts' : '—'));
  }
  function riskBar(label, pnl, cap) {
    const loss = pnl < 0 ? -pnl : 0;
    const capAbs = Math.abs(cap || 0) || 1;
    const pct = Math.min(100, (loss / capAbs) * 100);
    const cls = pct >= 90 ? 'crit' : pct >= 60 ? 'warn' : '';
    const pnlCls = pnl < 0 ? 'pnl-neg' : 'pnl-pos';
    return `<div class="risk-row"><div class="risk-head"><span>${label}</span>
      <span class="${pnlCls}">${fmtMoney(pnl)} / cap ${fmtMoney(cap)} (${pct.toFixed(0)}%)</span></div>
      <div class="risk-bar"><span class="${cls}" style="width:${pct}%"></span></div></div>`;
  }
  function renderRisk(r) {
    if (!r || r.daily_cap_pct === undefined) { setHtml('#live-risk', '<span class="muted">keine Daten</span>'); return; }
    // Compact gauge: short label · slim color bar (visible even at low %) · €/%/cap.
    const gauge = (label, capPct, pnl, cap) => {
      const loss = pnl < 0 ? -pnl : 0;
      const capAbs = Math.abs(cap || 0) || 1;
      const pct = Math.min(100, (loss / capAbs) * 100);
      const cls = pct >= 80 ? 'crit' : pct >= 50 ? 'warn' : '';
      const w = loss > 0 ? Math.max(3, pct) : 0;
      return `<div class="rk-row" title="${label} (${(capPct * 100).toFixed(0)}% Cap): ${fmtMoney(pnl)} von ${fmtMoney(cap)} €">
        <span class="rk-lbl">${label}</span>
        <span class="rk-gauge"><span class="rk-fill ${cls}" style="width:${w}%"></span></span>
        <span class="rk-val">${fmtEur(pnl)} <span class="rk-cap">· ${pct.toFixed(0)}% v. ${fmtMoney(cap)}€</span></span>
      </div>`;
    };
    const open = (r.unrealized_pnl != null && r.open_positions > 0) ? ` · offen ${fmtEur(r.unrealized_pnl)}` : '';
    setHtml('#live-risk',
      gauge('Tag', r.daily_cap_pct, r.daily_pnl, r.daily_loss_cap) +
      gauge('Woche', r.weekly_cap_pct, r.weekly_pnl, r.weekly_loss_cap) +
      `<div class="rk-pos"><span class="muted">Positionen</span><span>${r.open_positions}/${r.max_open_positions}${open}</span></div>`);
  }
  function renderPositions(positions) {
    const tb = $('#positions-table tbody');
    setText('#live-pos-count', positions && positions.length ? `(${positions.length})` : '');
    if (!tb) return;
    if (!positions || !positions.length) { tb.innerHTML = '<tr><td colspan="6" class="muted">keine offenen Positionen</td></tr>'; return; }
    tb.innerHTML = '';
    for (const p of positions) {
      const tr = document.createElement('tr');
      const sideCls = p.side === 'buy' ? 'pos-long' : 'pos-short';
      const pnlCls = (p.profit || 0) < 0 ? 'pnl-neg' : 'pnl-pos';
      const pl = p.plan;
      let tpCell;
      if (pl) {
        const m = (v, taken) => v == null ? '—' : `<span style="${taken ? 'color:var(--ok);text-decoration:line-through' : ''}">${fmtNum(v)}</span>`;
        tpCell = `<span style="font-size:10px">T1 ${m(pl.tp1, pl.tp1_taken)} · T2 ${m(pl.tp2, pl.tp2_taken)} · T3 ${pl.tp3 != null ? fmtNum(pl.tp3) : '—'}${pl.breakeven ? ' · <b style="color:var(--ok)">BE</b>' : ''}</span>`;
      } else {
        tpCell = p.tp != null ? fmtNum(p.tp) : '—';
      }
      tr.innerHTML = `<td class="${sideCls}">${escapeHtml((p.side || '').toUpperCase())}</td>
        <td class="num">${fmtNum(p.volume, 2)}</td><td class="num">${fmtNum(p.open_price)}</td>
        <td class="num">${p.sl != null ? fmtNum(p.sl) : '—'}</td><td class="num">${tpCell}</td>
        <td class="num ${pnlCls}">${fmtMoney(p.profit)}</td>`;
      tb.appendChild(tr);
    }
  }

  // ----- News / Score-breakdown / AI panels (Live cockpit) -----
  function renderNews(n) {
    const el = $('#live-news'), badge = $('#live-news-badge'); if (!el) return;
    if (badge) badge.innerHTML = (n && n.in_blackout) ? '<span style="color:var(--bad);font-weight:700">⛔ BLACKOUT</span>' : '';
    if (!n) { el.innerHTML = '<span class="muted">—</span>'; return; }
    let html;
    if (n.next) {
      const mins = n.minutes_until_next;
      const cd = mins == null ? '' : (mins < 60 ? `in ${Math.round(mins)} min` : `in ${(mins / 60).toFixed(1)} h`);
      html = `<div>nächstes High-Impact: <b>${escapeHtml(n.next.title || '')}</b> <span class="muted">${escapeHtml(n.next.currency || '')} · ${cd}</span></div>`;
    } else {
      html = '<div class="muted">kein High-Impact in den nächsten 24 h</div>';
    }
    const up = (n.upcoming || []).filter(e => e.impact === 'high').slice(0, 4);
    if (up.length) html += '<div style="margin-top:4px;font-size:11px">' + up.map(e => `<div>${escapeHtml(fmtTs(e.ts))} · ${escapeHtml(e.title || '')} <span class="muted">${escapeHtml(e.currency || '')}</span></div>`).join('') + '</div>';
    el.innerHTML = html;
  }

  const _ENGINE_LABELS = { h1_zone: 'H1-Zone', m5_zone: 'M5-Zone', triple_vwap: 'VWAP', htf_volume_profile: 'VolProfile', session_liquidity: 'Sess/Liq', news: 'News', momentum: 'Momentum' };
  function renderScores(d) {
    const el = $('#live-scores'), tot = $('#live-score-total'); if (!el) return;
    if (!d || !d.subscores || !Object.keys(d.subscores).length) { el.innerHTML = '<span class="muted">—</span>'; if (tot) tot.textContent = ''; return; }
    if (tot) tot.textContent = d.score != null ? `(${Math.round(d.score)}/100 · ${escapeHtml(d.band || '')})` : '';
    el.innerHTML = Object.entries(d.subscores).map(([k, v]) => {
      const val = Math.round(v || 0), w = Math.max(2, Math.min(100, val));
      // Low scores must NOT use var(--border) — that's the track background, so
      // the bar would be invisible. Use a visible muted grey instead.
      const col = val >= 65 ? 'var(--ok)' : val >= 45 ? 'var(--warn)' : '#8b949e';
      return `<div style="display:flex;align-items:center;gap:6px;margin:2px 0;font-size:11px">
        <span style="width:68px;color:var(--muted)">${escapeHtml(_ENGINE_LABELS[k] || k)}</span>
        <span style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden"><span style="display:block;height:100%;width:${w}%;background:${col}"></span></span>
        <span style="width:22px;text-align:right">${val}</span></div>`;
    }).join('');
  }

  function renderAILast(a) {
    const el = $('#live-ai'); if (!el) return;
    if (!a || !a.comment) { el.innerHTML = '<span class="muted">noch kein M3-Call</span>'; return; }
    const conf = a.confidence != null ? `${a.confidence}%` : '—';
    const ez = a.entry_zone || {};
    const zone = (ez.min != null || ez.max != null) ? ` · Zone ${ez.min != null ? fmtNum(ez.min) : '?'}–${ez.max != null ? fmtNum(ez.max) : '?'}` : '';
    let html = `<div><b>${escapeHtml(a.decision || '—')}</b> ${a.entry_side ? escapeHtml(a.entry_side) : ''} <span class="muted">conf ${conf}${zone}</span></div>`;
    html += `<div style="font-size:11px;margin-top:3px">${escapeHtml(a.comment)}</div>`;
    if (a.invalidations && a.invalidations.length) html += `<div class="muted" style="font-size:10px;margin-top:3px">✗ ${a.invalidations.map(escapeHtml).join(' · ')}</div>`;
    html += `<div class="muted" style="font-size:10px;margin-top:2px">${escapeHtml(fmtTs(a.ts))}</div>`;
    el.innerHTML = html;
  }

  function setupAlerts() {
    api('/api/alerts/state').then(s => {
      setText('#live-alerts-state', s && s.configured ? (s.enabled ? '· aktiv' : '· konfiguriert (aus)') : '· nicht konfiguriert');
    }).catch(() => {});
    const btn = $('#alerts-test-btn');
    if (btn && !btn._wired) {
      btn._wired = true;
      btn.addEventListener('click', async () => {
        setText('#alerts-test-result', '…');
        try {
          const r = await api('/api/alerts/test', { method: 'POST' });
          setText('#alerts-test-result', r.ok ? '✓ gesendet' : ('✗ ' + (r.reason || 'Fehler')));
        } catch (e) { setText('#alerts-test-result', '✗ ' + (e.message || 'Fehler')); }
      });
    }
  }
  function startLivePolling() {
    if (state.liveTimer) clearInterval(state.liveTimer);
    state.liveTimer = setInterval(() => { if (state.activeTab === 'live') loadLive(); }, 3000);
  }

  // ----- Emergency stop (kill switch) -----
  $('#emergency-btn').addEventListener('click', () => {
    const target = !state.emergencyEngaged;
    showConfirmModal(
      target ? 'NOTAUS aktivieren' : 'NOTAUS aufheben',
      target ? 'Alle offenen Positionen werden <b>geschlossen</b> und neue Trades <b>gestoppt</b>. Sicher?'
             : 'Trading wieder <b>freigeben</b>?',
      () => doEmergency(target));
  });
  async function doEmergency(engaged) {
    try {
      const r = await api('/api/emergency', { method: 'POST', body: JSON.stringify({ engaged }), headers: { 'Content-Type': 'application/json' } });
      state.emergencyEngaged = r.engaged; renderEmergency();
    } catch (e) { alert('Emergency toggle failed: ' + e.message); }
  }
  function renderEmergency() {
    const b = $('#emergency-btn'); if (!b) return;
    b.classList.toggle('engaged', !!state.emergencyEngaged);
    b.textContent = state.emergencyEngaged ? '⛔ STOPP AKTIV' : '⛔ STOP';
  }
  async function loadEmergencyState() {
    try { const r = await api('/api/emergency'); state.emergencyEngaged = r.engaged; renderEmergency(); } catch (e) {}
  }

  // ----- Performance analytics -----
  const _perfSel = $('#perf-period');
  if (_perfSel) _perfSel.addEventListener('change', loadPerformance);
  async function loadPerformance() {
    const period = (_perfSel && _perfSel.value) || 'last_week';
    try {
      const d = await api(`/api/journal/aggregate?period=${encodeURIComponent(period)}`);
      renderPerfKpis(d); renderEquity(d.equity_curve || []); renderRDist(d.r_distribution || {}); renderSetup(d.setup_breakdown || {});
    } catch (e) {
      setHtml('#perf-kpis', '<span class="muted">keine Daten</span>');
    }
  }
  function renderPerfKpis(d) {
    const grid = $('#perf-kpis'); if (!grid) return;
    const pct = (x) => (x == null ? '—' : (x * 100).toFixed(1) + '%');
    const cards = [
      ['Trades', d.n_trades ?? 0], ['Closed', d.n_closed ?? 0],
      ['Win-Rate', pct(d.winrate)], ['Profit-Factor', fmtNum(d.profit_factor)],
      ['Expectancy (R)', fmtNum(d.expectancy)], ['Gewinn/Verlust (€)', fmtEur(d.total_pnl)],
      ['Sharpe', fmtNum(d.sharpe)], ['Max Drawdown', fmtNum(d.max_drawdown)],
    ];
    // Values may carry HTML (coloured €); render trusted formatter output directly.
    grid.innerHTML = cards.map(([l, v]) =>
      `<div class="metric"><div class="label">${l}</div><div class="value">${v}</div></div>`).join('');
  }
  function renderEquity(ec) {
    const host = $('#perf-equity'); if (!host) return;
    if (!ec || ec.length < 2) { host.innerHTML = '<span class="muted">noch zu wenig Trades</span>'; return; }
    const vals = ec.map(p => p[1]);
    const min = Math.min(...vals), max = Math.max(...vals), rng = (max - min) || 1;
    const W = 600, H = 90, pad = 4;
    const pts = vals.map((v, i) => {
      const x = pad + (i / (vals.length - 1)) * (W - 2 * pad);
      const y = pad + (1 - (v - min) / rng) * (H - 2 * pad);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    const up = vals[vals.length - 1] >= vals[0];
    host.innerHTML = `<svg class="equity-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline fill="none" stroke="${up ? '#3fb950' : '#f85149'}" stroke-width="1.5" points="${pts}"/></svg>`;
  }
  function renderRDist(rd) {
    const host = $('#perf-rdist'); if (!host) return;
    const entries = Object.entries(rd || {});
    if (!entries.length) { host.innerHTML = '<span class="muted">keine geschlossenen Trades</span>'; return; }
    const max = Math.max(1, ...entries.map(([, c]) => Number(c) || 0));
    host.innerHTML = entries.map(([bucket, cnt]) => {
      const c = Number(cnt) || 0;
      const loss = bucket.trim().startsWith('-') || bucket.toLowerCase().includes('loss');
      return `<div class="rbar"><span class="lbl">${escapeHtml(bucket)}</span>
        <span class="track"><span class="${loss ? 'loss' : 'win'}" style="width:${(c / max * 100).toFixed(0)}%"></span></span>
        <span class="cnt">${c}</span></div>`;
    }).join('');
  }
  function renderSetup(bd) {
    const host = $('#perf-setup'); if (!host) return;
    const entries = Object.entries(bd || {});
    if (!entries.length) { host.innerHTML = '<span class="muted">keine Daten</span>'; return; }
    const rows = entries.map(([name, s]) => {
      const o = (s && typeof s === 'object') ? s : {};
      const n = o.n ?? o.count ?? o.n_trades ?? '';
      const wr = o.winrate != null ? (o.winrate * 100).toFixed(0) + '%' : '';
      return `<div class="rbar"><span class="lbl" style="width:auto;text-align:left;flex:1">${escapeHtml(name)}</span>
        <span class="cnt" style="width:auto">${n}</span><span class="muted">${wr}</span></div>`;
    }).join('');
    host.innerHTML = rows;
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
  function setWsDot(ok) { state.wsOk = ok; const d = $('#ws-dot'); d.classList.toggle('ok', ok); d.classList.toggle('err', !ok); }

  function handleWsEvent(msg) {
    if (!msg || !msg.topic) return;
    const t = msg.topic;
    const d = msg.data || {};
    if (t === 'ticks') {
      // Live bar (envelope: { bar: {time,open,high,low,close} }). Comes from
      // both the closed-bar stream (market_ticks, once/min) and the forming-bar
      // stream (market_live, ~1/s). We bucket each into the active timeframe's
      // candle and merge — so the candle animates like MT5 on every timeframe.
      liveUpdateCandle(d.bar);
    } else if (t === 'features') {
      // Re-fetch overlays
      api(`/api/chart/overlays?symbol=${encodeURIComponent(state.symbol)}`).then(applyOverlays).catch(e => {});
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
    if (await tryRestoreSession()) { await loadMode(); onLoginSuccess(); }
    else show('#login');
  })();

})();
