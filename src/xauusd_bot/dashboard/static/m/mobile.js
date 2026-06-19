/* GoldManager mobile PWA — shares the desktop API/auth, mobile-first UI.
   Polls the active view; controls + push live under "Mehr". */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const state = { user: null, view: 'status', timer: null, reasoning: null, ai: null, emergency: false, chart: null, series: null, pushSub: null };

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
    loadView();
    if (state.timer) clearInterval(state.timer);
    // chart refreshes itself; others poll
    const period = v === 'decisions' ? 6000 : 4000;
    if (v !== 'chart' && v !== 'more') state.timer = setInterval(loadView, period);
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
    // last score from recent decisions
    try { const recent = await api('/api/decisions/recent?count=1'); const d = recent && recent[0]; $('#st-score').textContent = d ? `${Math.round(d.score)} · ${d.band || ''}` : '—'; } catch (e) {}
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
    const items = await api('/api/decisions/recent?count=40').catch(() => null);
    $('#de-meta').textContent = items ? `(${items.length})` : '';
    if (!items || !items.length) { $('#de-feed').innerHTML = '<span class="muted">noch keine Decisions</span>'; return; }
    $('#de-feed').innerHTML = items.map((d, i) => {
      const act = d.action || '—', badgeCls = d.score >= 85 ? 'hi' : d.score >= 65 ? 'mid' : '';
      const why = d.qualified ? `✓ ${esc(d.entry_type || 'qualified')}` : (d.block_reason ? esc(d.block_reason) : '');
      return `<div class="row tap" data-i="${i}"><span class="badge ${badgeCls}">${d.score == null ? '—' : Math.round(d.score)}</span>` +
        `<span class="act ${esc(act)}">${esc(act)}</span><span class="muted">${esc(d.direction || '')}</span>` +
        `<span class="why">${why}<br><span style="font-size:10px">${esc(fmtTs(d.ts))}</span></span></div>`;
    }).join('');
    $$('#de-feed .row').forEach(r => r.onclick = () => showDecision(items[+r.dataset.i]));
  }
  function showDecision(d) {
    const ai = (d.ai_reasoning || d.ai_comment);
    let html = `<button class="close" onclick="document.getElementById('modal').classList.add('hidden')">Schließen</button><h3>Decision</h3>`;
    html += kv('Zeit', fmtTs(d.ts)) + kv('Richtung', d.direction || '—') + kv('Score / Band', `${d.score == null ? '—' : Math.round(d.score)} · ${d.band || ''}`) +
      kv('Aktion', d.action || '—') + kv('Grund', d.block_reason || (d.qualified ? '✓ ' + (d.entry_type || 'qualified') : '—'));
    if (d.ai_confidence != null) html += kv('KI-Konfidenz', Math.round(d.ai_confidence) + '%');
    if (ai) html += `<h3 style="margin-top:14px">🧠 KI-Begründung</h3><div class="rationale">${esc(ai)}</div>`;
    if (d.ai_invalidations && d.ai_invalidations.length) html += `<div class="muted" style="margin-top:8px;font-size:13px">✗ ${d.ai_invalidations.map(esc).join(' · ')}</div>`;
    $('#sheet').innerHTML = html; $('#modal').classList.remove('hidden');
  }
  const kv = (k, v) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`;
  $('#modal').onclick = (e) => { if (e.target.id === 'modal') $('#modal').classList.add('hidden'); };

  // ---------- CHART ----------
  async function loadChart() {
    if (!window.LightweightCharts) return;
    if (!state.chart) {
      const el = $('#ch-container'); el.innerHTML = '';
      state.chart = LightweightCharts.createChart(el, {
        width: el.clientWidth, height: 340, layout: { background: { color: 'transparent' }, textColor: '#7d8590' },
        grid: { vertLines: { color: '#21262d' }, horzLines: { color: '#21262d' } },
        timeScale: { timeVisible: true, borderColor: '#30363d' }, rightPriceScale: { borderColor: '#30363d' },
      });
      state.series = state.chart.addCandlestickSeries({ upColor: '#3fb950', downColor: '#f85149', borderVisible: false, wickUpColor: '#3fb950', wickDownColor: '#f85149' });
      window.addEventListener('resize', () => state.chart && state.chart.applyOptions({ width: el.clientWidth }));
    }
    try {
      const bars = await api('/api/chart/history?timeframe=M5&limit=200');
      if (Array.isArray(bars) && bars.length) {
        state.series.setData(bars.map(b => ({ time: Math.floor(new Date(b.ts || b.time).getTime() / 1000), open: +b.open, high: +b.high, low: +b.low, close: +b.close })));
        const last = bars[bars.length - 1]; $('#ch-last').textContent = num(+last.close);
      }
    } catch (e) { $('#ch-last').textContent = '—'; }
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
  async function refreshPushState() {
    const el = $('#push-state'); const hint = $('#notify-hint');
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) { el.textContent = 'n/v'; hint.textContent = 'Dieses Gerät unterstützt keine Web-Push-Benachrichtigungen.'; $('#push-enable').classList.add('hidden'); return; }
    if (window.navigator.standalone === false && /iPhone|iPad/.test(navigator.userAgent)) hint.textContent = 'Tipp: für iOS zuerst über Teilen → „Zum Home-Bildschirm" installieren, dann Push aktivieren.';
    try {
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      state.pushSub = sub;
      el.className = 'pill ' + (sub ? 'on' : 'off'); el.textContent = sub ? 'an' : 'aus';
      $('#push-enable').textContent = sub ? '🔕 Push deaktivieren' : '🔔 Push aktivieren';
    } catch (e) {}
  }
  $('#push-enable').onclick = async () => {
    try {
      const reg = await navigator.serviceWorker.ready;
      const existing = await reg.pushManager.getSubscription();
      if (existing) { // unsubscribe
        await api('/api/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint: existing.endpoint }), headers: { 'Content-Type': 'application/json' } }).catch(() => {});
        await existing.unsubscribe(); toast('Push deaktiviert'); return refreshPushState();
      }
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') return toast('Benachrichtigungen nicht erlaubt');
      const { public_key, enabled } = await api('/api/push/vapid');
      if (!enabled || !public_key) return toast('Push am Server nicht konfiguriert (VAPID fehlt)');
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8(public_key) });
      const r = await api('/api/push/subscribe', { method: 'POST', body: JSON.stringify(sub.toJSON()), headers: { 'Content-Type': 'application/json' } });
      toast(r.ok ? '🔔 Push aktiviert' : 'Fehler beim Registrieren'); refreshPushState();
    } catch (e) { toast('Push-Fehler: ' + e.message); }
  };
  $('#push-test').onclick = async () => { try { const r = await api('/api/push/test', { method: 'POST' }); toast(r.ok ? '✓ Push gesendet (' + r.count + ')' : '✗ ' + (r.reason || 'keine Empfänger')); } catch (e) { toast('✗ ' + e.message); } };
  function urlB64ToUint8(b64) { const pad = '='.repeat((4 - b64.length % 4) % 4); const s = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/'); const raw = atob(s); return Uint8Array.from([...raw].map(c => c.charCodeAt(0))); }

  // ---------- wire ----------
  $$('#nav button').forEach(b => b.onclick = () => switchView(b.dataset.view));
  $('#refresh-btn').onclick = () => loadView();
  $('#logout-btn').onclick = async () => { try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {} location.reload(); };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadView(); });
  boot().catch(e => { console.error('boot failed', e); document.body.innerHTML = '<p style="padding:24px;color:#f85149">Fehler beim Laden: ' + esc(e.message) + '</p>'; });
})();
