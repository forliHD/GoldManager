/* GoldManager mobile PWA — shares the desktop API/auth, mobile-first UI.
   Polls the active view; controls + push live under "Mehr". */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const state = { user: null, view: 'status', timer: null, reasoning: null, ai: null, emergency: false, chart: null, series: null, pushSub: null, timeframe: 'M5', lastCandle: null, lastOverlays: null, ws: null, wsRetry: 0 };

  // ---------- helpers ----------
  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: 'include', ...opts });
    if (!res.ok) { let b = null; try { b = await res.json(); } catch (e) {} const err = new Error((b && b.detail) || res.statusText); err.status = res.status; throw err; }
    if (res.status === 204) return null;
    return res.json();
  }
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (n, d = 2) => (n == null || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const eur = (n) => (n == null || isNaN(n)) ? '—' : (n >= 0 ? '+' : '') + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' €';
  function fmtTs(s) { if (!s) return '—'; const d = new Date(s); return d.toLocaleString('en-GB', { timeZone: 'UTC', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }); }
  function ago(s) { if (!s) return '—'; const sec = (Date.now() - new Date(s).getTime()) / 1000; if (isNaN(sec)) return '—'; return sec < 90 ? Math.round(sec) + 's' : sec < 5400 ? Math.round(sec / 60) + 'm' : Math.round(sec / 3600) + 'h'; }
  function toast(msg) { const t = $('#toast'); t.textContent = msg; t.classList.remove('hidden'); clearTimeout(t._t); t._t = setTimeout(() => t.classList.add('hidden'), 2600); }
  function pnlCls(n) { return n > 0 ? 'pos' : n < 0 ? 'neg' : ''; }

  // ---------- auth ----------
  async function boot() {
    try { state.user = await api('/api/auth/me'); }
    catch (e) { if (e.status === 401) return showLogin(); throw e; }
    $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
    $('#more-user').textContent = state.user.username;
    $('#more-role').textContent = state.user.role;
    const priv = state.user.role === 'admin' || state.user.role === 'operator';
    $('#more-controls').classList.toggle('hidden', !priv);
    registerSW();
    refreshPushState();
    connectWS();
    switchView('status');
  }
  function showLogin() {
    $('#app').classList.add('hidden'); $('#login').classList.remove('hidden');
    $('#login-btn').onclick = async () => {
      $('#login-error').textContent = '';
      try {
        const form = new URLSearchParams({ username: $('#login-user').value, password: $('#login-pass').value });
        await api('/api/auth/login', { method: 'POST', body: form, headers: { 'Content-Type': 'application/x-www-form-urlencoded' } });
        boot();
      } catch (e) { $('#login-error').textContent = e.status === 401 ? 'Falsche Zugangsdaten' : ('Fehler: ' + e.message); }
    };
  }

  // ---------- navigation ----------
  function switchView(v) {
    state.view = v;
    $$('.view').forEach(el => el.classList.toggle('hidden', el.id !== 'view-' + v));
    $$('#nav button').forEach(b => b.classList.toggle('active', b.dataset.view === v));
    setTicksSub(v === 'chart'); // stream live ticks only while the chart is visible
    loadView();
    if (state.timer) clearInterval(state.timer);
    // Chart: live candles come over the WebSocket; only slow-refresh overlays as
    // a fallback (full re-fetch would reset the live candle/viewport every tick).
    if (v === 'chart') state.timer = setInterval(refreshOverlays, 20000);
    else if (v !== 'more') state.timer = setInterval(loadView, v === 'decisions' ? 6000 : 4000);
  }
  function loadView() {
    const fn = { status: loadStatus, positions: loadPositions, decisions: loadDecisions, chart: loadChart, more: loadMore }[state.view];
    if (fn) fn().catch(e => { if (e.status === 401) showLogin(); else console.error(state.view, e); });
  }

  // ---------- STATUS ----------
  async function loadStatus() {
    const [acc, health, aiLast, aiState, rState, risk, positions] = await Promise.all([
      api('/api/account').catch(() => null), api('/api/health/services').catch(() => null),
      api('/api/ai/last').catch(() => null), api('/api/ai/state').catch(() => null),
      api('/api/ai/reasoning/state').catch(() => null), api('/api/risk').catch(() => null),
      api('/api/positions/managed').catch(() => null),
    ]);
    // connection
    const alive = health && health.execution_alive;
    setDot('#conn-dot', alive ? 'ok' : 'err'); $('#conn-txt').textContent = alive ? 'live' : 'offline';
    // banner
    const mkt = health && health.market_status;
    const b = $('#status-banner');
    if (!health || !health.redis) { b.className = 'banner err'; b.innerHTML = 'Backend nicht erreichbar'; }
    else if (mkt === 'closed') { b.className = 'banner warn'; b.innerHTML = '🌙 Markt geschlossen<small>keine neuen Candles — normal am Wochenende</small>'; }
    else if (mkt === 'feed_down') { b.className = 'banner err'; b.innerHTML = '⚠ Daten-Feed offline<small>Bridge / data-collector prüfen</small>'; }
    else if (!alive) { b.className = 'banner err'; b.innerHTML = '⚠ Execution-Engine offline'; }
    else { b.className = 'banner ok'; b.innerHTML = '✅ Bot läuft<small>Markt offen · alle Services aktiv</small>'; }
    // stats
    $('#st-equity').textContent = acc && acc.equity != null ? num(acc.equity) + ' ' + (acc.currency || '') : '—';
    if (risk) { const p = risk.daily_pnl; const el = $('#st-pnl'); el.textContent = eur(p); el.className = 'val ' + pnlCls(p); }
    // services
    $('#st-services').innerHTML = renderServices(health);
    // AI
    $('#st-ai-status').innerHTML = aiState ? (aiState.enabled ? '<span class="ok">an</span>' : '<span class="muted">aus</span>') : '—';
    $('#st-reasoning').innerHTML = rState ? (rState.enabled ? '<span class="ok">an</span>' : '<span class="warn">aus (schnell)</span>') : '—';
    $('#st-ai-age').textContent = aiLast && aiLast.written_at ? 'vor ' + ago(aiLast.written_at) : (aiLast && aiLast.ts ? 'vor ' + ago(aiLast.ts) : 'noch keiner');
    // last score + subscore breakdown from the most recent decision
    try { const recent = await api('/api/decisions/recent?count=1'); const d = recent && recent[0]; $('#st-score').textContent = d ? `${Math.round(d.score)} · ${d.band || ''}` : '—'; renderScoreBreakdown(d); } catch (e) {}
    // position summary
    $('#st-position').innerHTML = renderPositionsList(positions, true);
  }
  function setDot(sel, cls) { const d = $(sel); d.className = 'dot ' + (cls || ''); }
  function renderServices(h) {
    if (!h || !h.redis) return '<span class="err">redis down</span>';
    const out = [];
    for (const [topic, s] of Object.entries(h.streams || {})) {
      let cls;
      if (topic === 'orders') cls = h.execution_alive ? 'ok' : 'err';
      else { const a = s.last_age_s; cls = a == null ? 'warn' : a < 90 ? 'ok' : a < 180 ? 'warn' : 'err'; if (h.market_status === 'closed' && cls === 'err') cls = 'warn'; }
      out.push(`<span class="svc"><span class="dot ${cls}"></span>${esc((s.service || topic).replace('-engine', ''))}</span>`);
    }
    out.push(`<span class="svc"><span class="dot ${h.execution_alive ? 'ok' : 'err'}"></span>exec</span>`);
    return out.join('');
  }
  const SCORE_LABELS = { h1_zone: 'H1-Zone', m5_zone: 'M5-Zone', triple_vwap: 'VWAP', htf_volume_profile: 'VolProfile', session_liquidity: 'Sess/Liq', news: 'News', momentum: 'Momentum' };
  function renderScoreBreakdown(d) {
    const el = $('#st-scores'); if (!el) return;
    const sub = d && d.subscores;
    if (!sub || !Object.keys(sub).length) { el.innerHTML = ''; return; }
    el.innerHTML = Object.entries(sub).map(([k, v]) => {
      const val = Math.round(Number(v) || 0), w = Math.max(2, Math.min(100, val));
      const col = val >= 65 ? 'var(--ok)' : val >= 45 ? 'var(--warn)' : '#8b949e';
      return `<div class="sb-row"><span class="sb-lbl">${esc(SCORE_LABELS[k] || k)}</span>` +
        `<span class="sb-bar"><span class="sb-fill" style="width:${w}%;background:${col}"></span></span>` +
        `<span class="sb-val">${val}</span></div>`;
    }).join('');
  }

  // ---------- POSITIONEN ----------
  async function loadPositions() {
    const [acc, risk, positions, trades] = await Promise.all([
      api('/api/account').catch(() => null), api('/api/risk').catch(() => null),
      api('/api/positions/managed').catch(() => null),
      api(`/api/journal/trades?limit=10`).catch(() => null),
    ]);
    if (acc) {
      $('#po-balance').textContent = num(acc.balance); $('#po-equity').textContent = num(acc.equity);
    }
    if (risk) {
      const d = $('#po-pnl-day'), w = $('#po-pnl-week');
      d.textContent = eur(risk.daily_pnl); d.className = 'val ' + pnlCls(risk.daily_pnl);
      w.textContent = eur(risk.weekly_pnl); w.className = 'val ' + pnlCls(risk.weekly_pnl);
      $('#po-risk').innerHTML = riskGauge('Tag', risk.daily_pnl, risk.daily_loss_cap) + riskGauge('Woche', risk.weekly_pnl, risk.weekly_loss_cap) +
        `<div class="kv"><span class="k">Positionen</span><span class="v">${risk.open_positions}/${risk.max_open_positions}</span></div>`;
    }
    $('#po-positions').innerHTML = renderPositionsList(positions, false);
    $('#po-trades').innerHTML = renderTrades(trades);
  }
  function riskGauge(label, pnl, cap) {
    const loss = pnl < 0 ? -pnl : 0, capAbs = Math.abs(cap || 0) || 1;
    const pct = Math.min(100, (loss / capAbs) * 100), w = loss > 0 ? Math.max(3, pct) : 0;
    const cls = pct >= 80 ? 'crit' : pct >= 50 ? 'warn' : '';
    return `<div style="margin:6px 0"><div class="kv" style="border:0;padding:2px 0"><span class="k">${label}</span><span class="v ${pnlCls(pnl)}">${eur(pnl)} <span class="muted">/ ${num(cap, 0)}€</span></span></div><div class="gauge"><span class="${cls}" style="width:${w}%"></span></div></div>`;
  }
  function renderPositionsList(positions, compact) {
    if (!positions || !positions.length) return '<span class="muted">keine offene Position</span>';
    return positions.map(p => {
      const side = (p.side || '').toUpperCase(), sCls = p.side === 'buy' ? 'pos' : 'neg';
      const pl = p.plan || {};
      const tp = pl.tp1 != null ? `T1 ${num(pl.tp1)}${pl.breakeven ? ' · BE' : ''}` : (p.tp != null ? num(p.tp) : '—');
      return `<div class="kv"><span class="k"><b class="${sCls}">${side}</b> ${num(p.volume, 2)} @ ${num(p.open_price)}</span><span class="v ${pnlCls(p.profit)}">${eur(p.profit)}</span></div>` +
        (compact ? '' : `<div class="muted" style="font-size:12px;padding-bottom:6px">SL ${p.sl != null ? num(p.sl) : '—'} · ${tp}</div>`);
    }).join('');
  }
  function renderTrades(trades) {
    if (!trades || !trades.length) return '<span class="muted">noch keine Trades</span>';
    let h = '<table><thead><tr><th>Zeit</th><th>Side</th><th class="num">€</th><th class="num">R</th></tr></thead><tbody>';
    for (const t of trades) h += `<tr><td>${esc(fmtTs(t.timestamp_open))}</td><td class="${t.side === 'buy' ? 'pos' : 'neg'}">${esc(t.side || '—')}</td><td class="num ${pnlCls(t.pnl_realized)}">${num(t.pnl_realized)}</td><td class="num">${num(t.pnl_r, 2)}</td></tr>`;
    return h + '</tbody></table>';
  }

  // ---------- DECISIONS ----------
  async function loadDecisions() {
    // history (journaled) carries the LLM rationale; recent (stream) does not.
    let items = await api('/api/decisions/history?limit=40').catch(() => null);
    if (!items) items = await api('/api/decisions/recent?count=40').catch(() => null);
    $('#de-meta').textContent = items ? `(${items.length})` : '';
    if (!items || !items.length) { $('#de-feed').innerHTML = '<span class="muted">noch keine Decisions</span>'; return; }
    $('#de-feed').innerHTML = items.map((d, i) => {
      const act = d.action || '—', badgeCls = d.score >= 85 ? 'hi' : d.score >= 65 ? 'mid' : '';
      const why = d.qualified ? `✓ ${esc(d.entry_type || 'qualified')}` : (d.block_reason ? esc(d.block_reason) : '');
      const ai = d.ai_reasoning || d.ai_comment;
      const conf = d.ai_confidence != null ? ` · ${Math.round(d.ai_confidence)}%` : '';
      const aiLine = ai
        ? `<div class="ai-snip">🧠 ${esc(ai.length > 120 ? ai.slice(0, 120) + '…' : ai)}<span class="muted">${conf}</span></div>`
        : (d.ai_status && d.ai_status !== 'ran' ? `<div class="ai-snip muted">🧠 ${esc(aiStatusLabel(d.ai_status))}</div>` : '');
      return `<div class="dwrap tap" data-i="${i}"><div class="row" style="border:0;padding:8px 0 2px">` +
        `<span class="badge ${badgeCls}">${d.score == null ? '—' : Math.round(d.score)}</span>` +
        `<span class="act ${esc(act)}">${esc(act)}</span><span class="muted">${esc(d.direction || '')}</span>` +
        `<span class="why">${why}<br><span style="font-size:10px">${esc(fmtTs(d.ts))}</span></span></div>${aiLine}</div>`;
    }).join('');
    $$('#de-feed .dwrap').forEach(r => r.onclick = () => showDecision(items[+r.dataset.i]));
  }
  function aiStatusLabel(s) {
    return { ai_off: 'KI aus', score_low: 'Score zu niedrig für KI', news_blackout: 'News-Blackout', llm_error: 'LLM-Fehler → Regel-Fallback' }[s] || s;
  }
  function showDecision(d) {
    const ai = (d.ai_reasoning || d.ai_comment);
    let html = `<button class="close" onclick="document.getElementById('modal').classList.add('hidden')">Schließen</button><h3>Decision</h3>`;
    html += kv('Zeit', fmtTs(d.ts)) + kv('Richtung', d.direction || '—') + kv('Score / Band', `${d.score == null ? '—' : Math.round(d.score)} · ${d.band || ''}`) +
      kv('Aktion', d.action || '—') + kv('Grund', d.block_reason || (d.qualified ? '✓ ' + (d.entry_type || 'qualified') : '—'));
    if (d.ai_confidence != null) html += kv('KI-Konfidenz', Math.round(d.ai_confidence) + '%');
    if (ai) html += `<h3 style="margin-top:14px">🧠 KI-Begründung</h3><div class="rationale">${esc(ai)}</div>`;
    else if (d.ai_status) html += `<h3 style="margin-top:14px">🧠 KI</h3><div class="rationale muted">${esc(aiStatusLabel(d.ai_status))}</div>`;
    if (d.ai_invalidations && d.ai_invalidations.length) html += `<div class="muted" style="margin-top:8px;font-size:13px">✗ ${d.ai_invalidations.map(esc).join(' · ')}</div>`;
    $('#sheet').innerHTML = html; $('#modal').classList.remove('hidden');
  }
  const kv = (k, v) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`;
  $('#modal').onclick = (e) => { if (e.target.id === 'modal') $('#modal').classList.add('hidden'); };

  // ---------- CHART ----------
  async function loadChart() {
    const el = $('#ch-container');
    if (!window.LightweightCharts) { $('#ch-last').textContent = 'Chart-Lib fehlt'; return; }
    const sz = () => ({ width: el.clientWidth || (window.innerWidth - 24), height: el.clientHeight || 360 });
    if (!state.chart) {
      el.innerHTML = '';
      const d = sz();
      state.chart = LightweightCharts.createChart(el, {
        width: d.width, height: d.height, layout: { background: { color: 'transparent' }, textColor: '#7d8590' },
        grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
        timeScale: { timeVisible: true, borderColor: '#30363d', rightOffset: 4 },
        rightPriceScale: { borderColor: '#30363d' },
        crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
        handleScale: true, handleScroll: true,
      });
      state.series = state.chart.addCandlestickSeries({ upColor: '#3fb950', downColor: '#f85149', borderVisible: false, wickUpColor: '#3fb950', wickDownColor: '#f85149', priceLineColor: '#58a6ff' });
      state.vwapLines = {};  // VWAP curve line-series (desktop parity), recreated with the chart
      // FVG box overlay layer (HTML rectangles positioned via the coordinate API).
      const layer = document.createElement('div'); layer.className = 'fvg-layer'; el.appendChild(layer); state.fvgLayer = layer;
      state.chart.timeScale().subscribeVisibleTimeRangeChange(() => positionFvgBoxes());
      if (window.ResizeObserver) new ResizeObserver(() => { try { state.chart.applyOptions(sz()); } catch (e) {} positionFvgBoxes(); }).observe(el);
      window.addEventListener('resize', () => { if (state.chart) { const x = sz(); state.chart.applyOptions(x); positionFvgBoxes(); } });
    } else { const x = sz(); if (x.width) state.chart.applyOptions(x); } // re-fit after the view was hidden (size 0) at create time
    // Generation token: a rapid timeframe switch fires loadChart again; bump the
    // token here and bail after the await if a newer call superseded us, so a
    // slow stale-TF fetch can't overwrite the current chart with old data.
    const myGen = (state.chartGen = (state.chartGen || 0) + 1);
    try {
      if (!state.symbol) { const h = await api('/api/health').catch(() => null); state.symbol = (h && h.symbol) || 'XAUUSD'; }
      const tf = state.timeframe || 'M5';
      const sym = encodeURIComponent(state.symbol);
      const [candles, overlays, positions] = await Promise.all([
        api(`/api/chart/candles?symbol=${sym}&timeframe=${tf}&count=300`),
        api(`/api/chart/overlays?symbol=${sym}`).catch(() => null),
        api('/api/positions/managed').catch(() => null),
      ]);
      if (myGen !== state.chartGen) return;  // a newer loadChart superseded this fetch
      if (Array.isArray(candles) && candles.length) {
        const mapped = candles.map(c => ({ time: Math.floor(new Date(c.time).getTime() / 1000), open: +c.open, high: +c.high, low: +c.low, close: +c.close }));
        state.series.setData(mapped);           // full (re)load on open / timeframe change
        state.lastCandle = { ...mapped[mapped.length - 1] };  // seed the live-merge bucket
        try { const n = mapped.length; state.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 80), to: n + 3 }); } catch (e) {}
        $('#ch-last').textContent = num(mapped[mapped.length - 1].close);
        state.lastOverlays = overlays;
        applyChartOverlays(overlays);
        renderTradeLevels(positions || []);  // draw the open position (entry/SL/TP) on the chart
        // Re-fit after the legend rendered (it shrinks the flex container a bit),
        // so the canvas matches the available space exactly — no inner scroll.
        try { state.chart.applyOptions({ width: el.clientWidth, height: el.clientHeight }); } catch (e) {}
        // Nudge box positioning after the first paint (priceToCoordinate is null
        // pre-paint; without live ticks at market close nothing else retries).
        setTimeout(positionFvgBoxes, 150);
      } else { $('#ch-last').textContent = 'keine Daten'; }
    } catch (e) { $('#ch-last').textContent = 'Fehler'; console.error('chart', e); }
  }
  // Seconds per timeframe bucket — for merging live bars into the active candle.
  const TF_SECONDS = { M1: 60, M5: 300, M15: 900, H1: 3600 };
  // Merge a live bar (forming, ~1/s via the WS 'ticks' topic) into the active
  // timeframe's candle so the chart animates like MT5 — no full re-fetch.
  function liveUpdateCandle(bar) {
    if (!bar || !state.series || state.view !== 'chart') return;
    const tfSec = TF_SECONDS[state.timeframe || 'M5'] || 300;
    const barT = Math.floor(new Date(bar.time).getTime() / 1000);
    if (isNaN(barT)) return;
    const bucketT = Math.floor(barT / tfSec) * tfSec;
    const o = +bar.open, h = +bar.high, l = +bar.low, c = +bar.close;
    const last = state.lastCandle;
    let candle;
    if (last && last.time === bucketT) candle = { time: bucketT, open: last.open, high: Math.max(last.high, h), low: Math.min(last.low, l), close: c };
    else if (last && bucketT < last.time) return; // stale/out-of-order
    else candle = { time: bucketT, open: o, high: h, low: l, close: c };
    state.lastCandle = candle;
    try { state.series.update(candle); $('#ch-last').textContent = num(candle.close); } catch (e) {}
    positionFvgBoxes(); // price autoscale can shift Y without a time-range change
  }
  async function refreshOverlays() {
    if (!state.series) return;
    const [o, positions] = await Promise.all([
      api(`/api/chart/overlays?symbol=${encodeURIComponent(state.symbol || 'XAUUSD')}`).catch(() => null),
      api('/api/positions/managed').catch(() => null),
    ]);
    if (o) { state.lastOverlays = o; applyChartOverlays(o); }
    renderTradeLevels(positions || []);  // keep entry/SL/TP lines fresh (trail, TP-taken)
  }
  const CHART_DEFAULTS = { vwap: true, va: true, fvg: true, fvgMax: 6, fvgTf: { H1: true, M5: true, M1: true }, fvgType: { bullish: true, bearish: true }, fvgPartial: true };
  function chartSettings() {
    if (state.cset) return state.cset;
    try { state.cset = Object.assign({}, CHART_DEFAULTS, JSON.parse(localStorage.getItem('mChartCfg2') || '{}')); }
    catch (e) { state.cset = { ...CHART_DEFAULTS }; }
    state.cset.fvgTf = Object.assign({}, CHART_DEFAULTS.fvgTf, state.cset.fvgTf);
    state.cset.fvgType = Object.assign({}, CHART_DEFAULTS.fvgType, state.cset.fvgType);
    return state.cset;
  }
  function saveChartSettings() { try { localStorage.setItem('mChartCfg2', JSON.stringify(state.cset)); } catch (e) {} }
  // VWAP evolving CURVES (desktop parity) — the backend sends a per-level point
  // series; we bucket it onto the active timeframe grid (one point per bucket so
  // an M1 point between M5 candles doesn't spread the candles apart).
  const VW_COL = { utc00: '#3b82f6', utc07: '#f59e0b', utc12: '#ec4899' };
  function vwapBucket(pts, tfSec) {
    const m = new Map();
    for (const p of (pts || [])) { if (!p || p.value == null) continue; m.set(Math.floor(p.time / tfSec) * tfSec, p.value); }
    return Array.from(m, ([time, value]) => ({ time, value })).sort((a, b) => a.time - b.time);
  }
  function applyVwapCurves(series, on) {
    state.vwapLines = state.vwapLines || {};
    const tfSec = TF_SECONDS[state.timeframe || 'M5'] || 300;
    for (const k of ['utc00', 'utc07', 'utc12']) {
      if (!on) { if (state.vwapLines[k]) { try { state.vwapLines[k].setData([]); } catch (e) {} } continue; }
      if (!state.vwapLines[k]) {
        try { state.vwapLines[k] = state.chart.addLineSeries({ color: VW_COL[k], lineWidth: 2, priceLineVisible: false, lastValueVisible: true, title: 'VWAP ' + k.slice(3), crosshairMarkerVisible: false }); }
        catch (e) { continue; }
      }
      try { state.vwapLines[k].setData(vwapBucket((series || {})[k], tfSec)); } catch (e) {}
    }
  }

  function applyChartOverlays(o) {
    (state.priceLines || []).forEach(pl => { try { state.series.removePriceLine(pl); } catch (e) {} });
    state.priceLines = [];
    if (!o || !window.LightweightCharts || !state.series) { state.hasVA = false; renderLegend(); return; }
    const s = chartSettings();
    const S = LightweightCharts.LineStyle;
    const add = (price, color, title, style, w) => {
      if (price == null || isNaN(+price)) return;
      try { state.priceLines.push(state.series.createPriceLine({ price: +price, color, lineWidth: w || 1, lineStyle: style, axisLabelVisible: true, title })); } catch (e) {}
    };
    // VWAP — evolving curves from vwap_series.
    applyVwapCurves(o.vwap_series || {}, !!s.vwap);
    // Volume Profile — LOCKED references only (the tradeable levels): month always
    // (dashed), plus yesterday's profile (prev_day, solid) or — on Monday, with no
    // completed prior day — the previous week (prev_week, dashed). VAH red / VPOC
    // yellow / VAL green, matching the desktop.
    const vp = o.volume_profile || {};
    let vaAny = false;
    if (s.va) {
      const drawVA = (key, label, style, width) => {
        const p = vp[key]; if (!p || p.vpoc == null) return false;
        add(p.vah, '#ef4444', label + ' VAH', style, width);
        add(p.vpoc, '#eab308', label + ' VPOC', style, width);
        add(p.val, '#22c55e', label + ' VAL', style, width);
        return true;
      };
      vaAny = drawVA('prev_month', 'M', S.Dashed, 2);
      if (vp.prev_day && vp.prev_day.vpoc != null) vaAny = drawVA('prev_day', 'D', S.Solid, 2) || vaAny;
      else vaAny = drawVA('prev_week', 'W', S.Dashed, 2) || vaAny;
    }
    state.hasVA = vaAny;
    // FVG as HTML boxes (desktop parity) — store raw, curate, position.
    state.fvgRaw = (o.fvg_zones || o.fvgZones || []);
    renderFvgZones();
    renderLegend();
  }

  // Open position(s) drawn ON the chart: entry + SL + TP1/2/3, labelled (desktop
  // parity). Kept on a separate priceLine list so an overlay refresh doesn't wipe
  // them and vice-versa.
  function renderTradeLevels(positions) {
    (state.tradeLines || []).forEach(pl => { try { state.series.removePriceLine(pl); } catch (e) {} });
    state.tradeLines = [];
    state.lastPositions = positions || [];
    if (state.series && positions && positions.length) {
      const S = LightweightCharts.LineStyle;
      const tl = (price, color, title, style, w) => {
        if (price == null || isNaN(+price)) return;
        try { state.tradeLines.push(state.series.createPriceLine({ price: +price, color, lineWidth: w || 1, lineStyle: style, axisLabelVisible: true, title })); } catch (e) {}
      };
      for (const p of positions) {
        const long = p.side === 'buy';
        tl(p.open_price, long ? '#22c55e' : '#ef4444', (long ? '▲ LONG ' : '▼ SHORT ') + num(p.open_price), S.Solid, 2);
        tl(p.sl, '#ef4444', 'SL', S.Dashed, 1);
        const pl = p.plan;
        if (pl) {
          tl(pl.tp1, '#16a34a', 'TP1' + (pl.tp1_taken ? ' ✓' : ''), S.Dotted, 1);
          tl(pl.tp2, '#16a34a', 'TP2' + (pl.tp2_taken ? ' ✓' : ''), S.Dotted, 1);
          tl(pl.tp3, '#15803d', 'TP3', S.Dotted, 1);
        } else if (p.tp != null) { tl(p.tp, '#16a34a', 'TP', S.Dotted, 1); }
      }
    }
    renderLegend();
  }

  function renderLegend() {
    const s = chartSettings();
    const legend = [];
    if (s.vwap) legend.push(`<span><i style="background:#3b82f6"></i>VWAP</span>`);
    if (s.va && state.hasVA) legend.push(`<span><i style="background:#ef4444"></i>VAH</span><span><i style="background:#eab308"></i>VPOC</span><span><i style="background:#22c55e"></i>VAL</span>`);
    if (s.fvg && (state.fvgZones || []).length) legend.push(`<span><i style="background:#3fb950"></i>FVG↑</span><span><i style="background:#f85149"></i>FVG↓</span>`);
    if ((state.lastPositions || []).length) legend.push(`<span><i style="background:#58a6ff"></i>Position E/SL/TP</span>`);
    const el = $('#ch-legend'); if (el) el.innerHTML = legend.join('');
  }
  // ----- FVG boxes (HTML overlay) — mirrors desktop renderFvgZones/positionFvgZones -----
  const FVG_TF_SCALE = { H1: 80, M5: 30, M1: 12 };
  function renderFvgZones() {
    const s = chartSettings();
    if (!s.fvg) { state.fvgZones = []; return positionFvgBoxes(); }
    const px = state.lastCandle && Number(state.lastCandle.close);
    const dist = x => { const top = +x.top, bot = +x.bottom; if (px == null) return 0; if (px > top) return px - top; if (px < bot) return bot - px; return 0; };
    const rank = x => (Number(x.rank_score || x.size_points || 0)) || 0.01;
    const relevance = x => px == null ? rank(x) : rank(x) / (1 + dist(x) / (FVG_TF_SCALE[String(x.tf).toUpperCase()] || 20)); // blend: strength + proximity
    const z = (state.fvgRaw || []).filter(x => {
      const tf = String(x.tf || '').toUpperCase();
      return ((tf in s.fvgTf) ? s.fvgTf[tf] : true) && (s.fvgType[x.type] !== false) && (s.fvgPartial || x.status !== 'partially_mitigated');
    });
    z.sort((a, b) => relevance(b) - relevance(a));
    const perTf = {}; const kept = [];
    for (const zone of z) { const tf = zone.tf || '?'; perTf[tf] = (perTf[tf] || 0) + 1; if (perTf[tf] <= s.fvgMax) kept.push(zone); } // cap PER timeframe
    state.fvgZones = kept;
    positionFvgBoxes();
  }
  function positionFvgBoxes() {
    const layer = state.fvgLayer, series = state.series, chart = state.chart;
    if (!layer || !series || !chart) return;
    layer.innerHTML = '';
    const zones = state.fvgZones || [];
    if (!zones.length) return;
    const el = $('#ch-container');
    const ts = chart.timeScale();
    let paneW = el.clientWidth;
    try { paneW -= (chart.priceScale('right').width() || 0); } catch (e) {}
    if (!(paneW > 0)) paneW = el.clientWidth;
    for (const z of zones) {
      const yTop = series.priceToCoordinate(+z.top), yBot = series.priceToCoordinate(+z.bottom);
      if (yTop == null || yBot == null) continue;
      const top = Math.min(yTop, yBot), h = Math.max(4, Math.abs(yBot - yTop));
      let xl = ts.timeToCoordinate(Math.floor(new Date(z.created_at).getTime() / 1000));
      if (xl == null) continue;                    // formation not on the loaded axis → don't pin full-width to the left
      if (xl < 0) xl = 0;                           // formed off visible-left but active → span from the edge
      if (xl > paneW) continue;                    // formed beyond the view → skip
      const bull = z.type === 'bullish', partial = z.status === 'partially_mitigated';
      const col = bull ? '63,185,80' : '248,81,73';
      const box = document.createElement('div'); box.className = 'fvg-box';
      box.style.left = xl + 'px'; box.style.top = top + 'px';
      box.style.width = Math.max(2, paneW - xl) + 'px'; box.style.height = h + 'px';
      box.style.background = `rgba(${col},${partial ? 0.06 : 0.13})`;
      const bs = `1px ${partial ? 'dashed' : 'solid'} rgba(${col},0.85)`;
      box.style.borderTop = bs; box.style.borderBottom = bs;
      const tag = document.createElement('div'); tag.className = 'fvg-tag';
      const extTag = z.extension_tf ? ` +${z.extension_tf}` : '';
      tag.textContent = `${z.tf} ${bull ? '▲' : '▼'}${partial ? ' ◑' : ''}${extTag}`;
      tag.style.background = `rgba(${col},0.9)`;
      box.appendChild(tag); layer.appendChild(box);
    }
  }
  // ----- chart settings sheet (timeframe lives in the head; this is overlays) -----
  function openChartSettings() {
    const s = chartSettings();
    const sw = on => `<div class="switch ${on ? 'on' : ''}"></div>`;
    const chip = (k, on) => `<button class="chip ${on ? 'on' : ''}" data-tf="${k}">${k}</button>`;
    $('#sheet').innerHTML =
      `<button class="close" onclick="document.getElementById('modal').classList.add('hidden')">Schließen</button><h3>Chart-Einstellungen</h3>` +
      `<div class="set-row"><span>VWAP-Linien</span><div id="cs-vwap">${sw(s.vwap)}</div></div>` +
      `<div class="set-row"><span>Value Area (VAH/VPOC/VAL)</span><div id="cs-va">${sw(s.va)}</div></div>` +
      `<div class="set-row"><span>Fair Value Gaps</span><div id="cs-fvg">${sw(s.fvg)}</div></div>` +
      `<div class="set-row"><span>FVG Zonen je Timeframe</span><input type="number" id="cs-fvgmax" min="0" max="12" value="${s.fvgMax}"></div>` +
      `<div class="set-row"><span>FVG Timeframes</span><div class="chips" id="cs-fvgtf">${chip('H1', s.fvgTf.H1)}${chip('M5', s.fvgTf.M5)}${chip('M1', s.fvgTf.M1)}</div></div>` +
      `<div class="set-row"><span>FVG Typ</span><div class="chips" id="cs-fvgtype"><button class="chip ${s.fvgType.bullish ? 'on' : ''}" data-t="bullish">▲ Bull</button><button class="chip ${s.fvgType.bearish ? 'on' : ''}" data-t="bearish">▼ Bear</button></div></div>` +
      `<div class="set-row"><span>Teilw. mitigierte zeigen</span><div id="cs-fvgpartial">${sw(s.fvgPartial)}</div></div>`;
    const reapply = () => { saveChartSettings(); applyChartOverlays(state.lastOverlays); };
    const tog = (id, key) => { $(id).onclick = () => { s[key] = !s[key]; $(id).querySelector('.switch').classList.toggle('on', s[key]); reapply(); }; };
    tog('#cs-vwap', 'vwap'); tog('#cs-va', 'va'); tog('#cs-fvg', 'fvg'); tog('#cs-fvgpartial', 'fvgPartial');
    $('#cs-fvgmax').onchange = () => { s.fvgMax = Math.max(0, Math.min(12, parseInt($('#cs-fvgmax').value, 10) || 0)); reapply(); };
    $$('#cs-fvgtf .chip').forEach(c => c.onclick = () => { const k = c.dataset.tf; s.fvgTf[k] = !s.fvgTf[k]; c.classList.toggle('on', s.fvgTf[k]); reapply(); });
    $$('#cs-fvgtype .chip').forEach(c => c.onclick = () => { const k = c.dataset.t; s.fvgType[k] = !s.fvgType[k]; c.classList.toggle('on', s.fvgType[k]); reapply(); });
    $('#modal').classList.remove('hidden');
  }
  // ----- live WebSocket (forming bar ~1/s -> chart animates; features -> overlays) -----
  function setTicksSub(on) {
    const ws = state.ws;
    if (!ws || ws.readyState !== 1) return;
    try { ws.send(JSON.stringify({ action: on ? 'subscribe' : 'unsubscribe', topic: 'ticks' })); } catch (e) {}
  }
  function connectWS() {
    try { if (state.ws) state.ws.close(); } catch (e) {}
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let ws; try { ws = new WebSocket(`${proto}://${location.host}/ws`); } catch (e) { return; }
    state.ws = ws;
    ws.onopen = () => {
      state.wsRetry = 0;
      try { ws.send(JSON.stringify({ action: 'subscribe', topic: 'features' })); } catch (e) {}
      setTicksSub(state.view === 'chart'); // only stream ~1/s ticks while the chart is open
    };
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.topic === 'ticks') liveUpdateCandle((m.data || {}).bar);
        else if (m.topic === 'features' && state.view === 'chart') refreshOverlays();
      } catch (e) {}
    };
    ws.onerror = () => {};
    ws.onclose = () => { const d = Math.min(30000, 1000 * Math.pow(2, (state.wsRetry++) || 0)); clearTimeout(state.wsTimer); state.wsTimer = setTimeout(connectWS, d); };
  }

  // ---------- MEHR ----------
  async function loadMore() {
    const [ai, r, em] = await Promise.all([api('/api/ai/state').catch(() => null), api('/api/ai/reasoning/state').catch(() => null), api('/api/emergency').catch(() => null)]);
    if (ai) { state.ai = ai.enabled; setToggle('#t-ai', ai.enabled); }
    if (r) { state.reasoning = r.enabled; setToggle('#t-reasoning', r.enabled, r.enabled ? 'an' : 'aus'); }
    if (em) { state.emergency = em.engaged; const b = $('#t-emergency'); b.classList.toggle('engaged', em.engaged); b.textContent = em.engaged ? '⛔ NOT-AUS AKTIV — aufheben' : '⛔ NOT-AUS'; }
  }
  function setToggle(sel, on, label) { const b = $(sel); b.classList.toggle('on', on); b.classList.toggle('off', !on); b.textContent = label || (on ? 'an' : 'aus'); }
  $('#t-ai').onclick = async () => { try { const r = await api('/api/ai/toggle', { method: 'POST', body: JSON.stringify({ enabled: !state.ai }), headers: { 'Content-Type': 'application/json' } }); state.ai = r.enabled; setToggle('#t-ai', r.enabled); toast('KI ' + (r.enabled ? 'an' : 'aus')); } catch (e) { toast('Fehler: ' + e.message); } };
  $('#t-reasoning').onclick = async () => { try { const r = await api('/api/ai/reasoning/toggle', { method: 'POST', body: JSON.stringify({ enabled: !state.reasoning }), headers: { 'Content-Type': 'application/json' } }); state.reasoning = r.enabled; setToggle('#t-reasoning', r.enabled, r.enabled ? 'an' : 'aus'); toast('Reasoning ' + (r.enabled ? 'an' : 'aus (schnell)')); } catch (e) { toast('Fehler: ' + e.message); } };
  $('#t-emergency').onclick = async () => {
    const target = !state.emergency;
    if (!confirm(target ? 'NOT-AUS aktivieren? Alle Positionen werden geschlossen und neue Trades gestoppt.' : 'NOT-AUS aufheben und Trading wieder freigeben?')) return;
    try { const r = await api('/api/emergency', { method: 'POST', body: JSON.stringify({ engaged: target }), headers: { 'Content-Type': 'application/json' } }); state.emergency = r.engaged; loadMore(); toast(r.engaged ? '⛔ NOT-AUS aktiv' : 'Trading freigegeben'); } catch (e) { toast('Fehler: ' + e.message); }
  };
  $('#tg-test').onclick = async () => { try { const r = await api('/api/alerts/test', { method: 'POST' }); toast(r.ok ? '✓ Telegram gesendet' : '✗ ' + (r.reason || 'Fehler')); } catch (e) { toast('✗ ' + e.message); } };

  // ---------- PUSH ----------
  async function registerSW() {
    if (!('serviceWorker' in navigator)) return;
    try { await navigator.serviceWorker.register('sw.js'); } catch (e) { console.warn('SW register failed', e); }
  }
  function isStandalone() {
    return window.navigator.standalone === true || matchMedia('(display-mode: standalone)').matches;
  }
  async function refreshPushState() {
    const el = $('#push-state'); const hint = $('#notify-hint');
    const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      el.textContent = 'n/v';
      hint.innerHTML = isIOS
        ? 'iOS unterstützt Push nur in der <b>installierten App</b>: unten <b>Teilen → „Zum Home-Bildschirm"</b>, die App von dort öffnen, dann hier Push aktivieren.'
        : 'Dieses Gerät/dieser Browser unterstützt keine Web-Push-Benachrichtigungen.';
      $('#push-enable').classList.add('hidden');
      return;
    }
    if (isIOS && !isStandalone()) {
      hint.innerHTML = '⚠️ Für Push auf dem iPhone: <b>Teilen → „Zum Home-Bildschirm"</b>, dann die App vom Homescreen öffnen — im normalen Safari-Tab geht Push nicht.';
    } else { hint.textContent = ''; }
    try {
      state.vapidKey = (await api('/api/push/vapid').catch(() => null) || {}).public_key || state.vapidKey;
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      state.pushSub = sub;
      el.className = 'pill ' + (sub ? 'on' : 'off'); el.textContent = sub ? 'an' : 'aus';
      $('#push-enable').textContent = sub ? '🔕 Push deaktivieren' : '🔔 Push aktivieren';
      // Re-register an existing subscription so it carries the current user/role
      // tag (heals older untagged records, idempotent — keyed by endpoint).
      if (sub) api('/api/push/subscribe', { method: 'POST', body: JSON.stringify(sub.toJSON()), headers: { 'Content-Type': 'application/json' } }).catch(() => {});
    } catch (e) {}
  }
  $('#push-enable').onclick = async () => {
    // Already subscribed (known synchronously) → toggle OFF. No permission prompt.
    if (state.pushSub) {
      try {
        await api('/api/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint: state.pushSub.endpoint }), headers: { 'Content-Type': 'application/json' } }).catch(() => {});
        await state.pushSub.unsubscribe();
      } catch (e) {}
      state.pushSub = null; toast('Push deaktiviert'); return refreshPushState();
    }
    const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);
    if (isIOS && !isStandalone()) return toast('iOS: zuerst „Zum Home-Bildschirm" hinzufügen und die App von dort öffnen — im Safari-Tab geht Push nicht.');
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return toast('Push wird hier nicht unterstützt.');
    // iOS requires Notification.requestPermission() to run INSIDE the user gesture,
    // before any await — otherwise it silently fails. Call it first.
    let perm = Notification.permission;
    if (perm === 'default') { try { perm = await Notification.requestPermission(); } catch (e) {} }
    if (perm !== 'granted') return toast('Benachrichtigungen sind blockiert — in den Einstellungen für diese App erlauben.');
    try {
      const reg = await navigator.serviceWorker.ready;
      const key = state.vapidKey || ((await api('/api/push/vapid').catch(() => null)) || {}).public_key;
      if (!key) return toast('Push am Server nicht konfiguriert (VAPID fehlt).');
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8(key) });
      state.pushSub = sub;
      const r = await api('/api/push/subscribe', { method: 'POST', body: JSON.stringify(sub.toJSON()), headers: { 'Content-Type': 'application/json' } });
      toast(r.ok ? `🔔 Push aktiviert (${r.count} Gerät${r.count === 1 ? '' : 'e'})` : 'Speichern fehlgeschlagen');
      refreshPushState();
    } catch (e) { toast('Push-Fehler: ' + (e.message || e.name)); }
  };
  $('#push-test').onclick = async () => { try { const r = await api('/api/push/test', { method: 'POST' }); toast(r.ok ? '✓ Push gesendet (' + r.count + ')' : '✗ ' + (r.reason || 'keine Empfänger')); } catch (e) { toast('✗ ' + e.message); } };
  function urlB64ToUint8(b64) { const pad = '='.repeat((4 - b64.length % 4) % 4); const s = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/'); const raw = atob(s); return Uint8Array.from([...raw].map(c => c.charCodeAt(0))); }

  // ---------- wire ----------
  $$('#nav button').forEach(b => b.onclick = () => switchView(b.dataset.view));
  $$('#tf-row button').forEach(b => b.onclick = () => {
    $$('#tf-row button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    state.timeframe = b.dataset.tf;
    state.lastCandle = null;
    loadChart();
  });
  $('#ch-gear').onclick = openChartSettings;
  $('#refresh-btn').onclick = () => loadView();
  $('#logout-btn').onclick = async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {} location.reload(); };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadView(); });
  boot().catch(e => { console.error('boot failed', e); document.body.innerHTML = '<p style="padding:24px;color:#f85149">Fehler beim Laden: ' + esc(e.message) + '</p>'; });
})();
