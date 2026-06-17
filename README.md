# XAUUSD Trading Bot — GoldManager

Vantage-MT5 → Python Feature-Engine → AI-Decision-Layer (OpenRouter / MiniMax BYOK) → Risk/Execution → Journal/Review → MT5-Overlay-Visualisierung.

**Aktueller Stand (2026-06-17):** Blöcke 1–8 + 5c + 9 ship-ready auf `dev`. 1159 Tests grün, alle Architektur-Invarianten I-1..I-5 verifiziert (I-1 in Block 8 verschärft: `import MetaTrader5` ist nur noch im Windows-Python-Bridge-Server erlaubt). Siehe `00_FINAL_PLAN.md` für die volle Architektur und `AGENTS.md` für operative Details (Caveats, Live-Bugs, Memory).

---

## Build-Status

| Block | Inhalt | Status |
|-------|--------|--------|
| 1 | Repo-Skeleton, Docker-Stack, Connector-Abstraktion, Replay/Paper, Data Layer | ✅ ship-ready |
| 2 | Feature-Engine (Session, Triple-VWAP, FixedVolumeRange, FVG, MarketStructure, CandleMomentum, Liquidity, News) + Overlay-Writer | ✅ ship-ready |
| 3 | Aggregator + Scoring + RuleBasedFallback + TradeQualification | ✅ ship-ready |
| 4 | Execution + Risk + Pending/Stop/TP + EmergencyStop | ✅ ship-ready |
| 5a | TradeJournalDB (TimescaleDB) + FeatureSnapshotStore + Read-API | ✅ ship-ready |
| 5b | BacktestEngine + WalkForwardEngine (Event-Replay, Slippage/Spread-Modelle, IS/OOS-Windows) | ✅ ship-ready |
| 5c | Daily/WeeklyReview + FittingProposal + ReviewerOpenRouterClient + BacktestSpec-Parser | ✅ ship-ready |
| 6 | AIDecisionLayer (OpenRouter) parallel zu RuleBasedFallback | ✅ ship-ready |
| 7 | MT5-Viz-Bridge + `BotOverlay.mq5` (MQL5-Indikator + Python-Simulator + Static-Check) | ✅ ship-ready |
| 8 | LiveMT5Connector (RPyC-Client) + mt5-terminal-Container (Wine + MT5 + RPyC-Bridge) + Vantage-XAUUSD-SymbolSpec | ✅ ship-ready |
| 9 | Custom Web-Dashboard (FastAPI + WebSocket + Multi-User + Lightweight-Charts Frontend + Backtest-Trigger + Live-Mode-Toggle) | ✅ ship-ready |
| 10 | Demo-Forward auf Ubuntu → Monitoring → (erst dann) Live mit Mini-Volumen | offen |

Roadmap-Anpassung 2026-06-16: Custom-Dashboard wurde von "optional nach Block 9" zu eigenem **Block 9** hochgestuft. Demo-Forward + Live verschiebt sich auf Block 10.

---

## Repository layout

```
GoldManager/
├── 00_FINAL_PLAN.md                  # Architektur-Spezifikation
├── 01_orchestrator.md                # Agent-Briefings
├── 02_data_layer_mt5_bridge.md
├── 03_feature_engine.md
├── 04_decision_scoring.md
├── 05_execution_risk.md
├── 06_journal_backtest_review.md
├── 07_devops_docker_viz.md
├── AGENTS.md                         # Project memory: invariants, caveats, live-bugs
├── README.md                         # Diese Datei
├── decision_agent.md                 # System-Prompt für AI-Decision-Layer
├── news_context_agent.md
├── review_agent.md
│
├── docker-compose.base.yml           # redis, timescaledb, alle Python-Services
├── docker-compose.dev.yml            # Mac: CONNECTOR_MODE=replay, kein MT5
├── docker-compose.prod.yml           # Ubuntu: + mt5-terminal (Wine)
├── .env.example
├── pyproject.toml
│
├── docker/
│   ├── service/Dockerfile            # shared Python service image
│   └── mt5-terminal/                  # Wine + MT5 + RPyC bridge (Block 8)
│
├── mql5/
│   └── BotOverlay.mq5                # MQL5-Indikator (Block 7)
│
├── tools/
│   ├── generate_sample_data.py       # deterministischer 30d XAUUSD M1 Sample
│   ├── check_mql5_syntax.py          # MQL5 Static-Check (Block 7)
│   └── run_simulator_against_smoke.py # feature_smoke → MQL5-Simulator (Block 7)
│
├── data/
│   └── sample/
│       └── xauusd_m1_sample.parquet
│
├── src/xauusd_bot/
│   ├── connectors/                   # IMarketConnector + Replay / Live / Paper
│   ├── data/                         # OHLCBuilder, SpreadMonitor, Quality, SymbolSpec
│   ├── features/                     # Session, TripleVWAP, VolumeRange, FVG, ...
│   ├── decision/                     # Aggregator, Scoring, RuleBasedFallback, AI-Layer, Orchestrator
│   ├── execution/                    # RiskManager, PositionSizer, OrderManager, SL/TP, EmergencyStop
│   ├── journal/                      # InMemory + TimescaleDB Stores, Queries, Schemas
│   ├── backtest/                     # BacktestEngine, WalkForwardEngine, Models (Block 5b)
│   ├── review/                       # ReviewEngine, FittingProposalEngine (Block 5c)
│   ├── viz/                          # Overlay-Writer (Block 2) + Bot-Overlay-Simulator (Block 7)
│   ├── cli/                          # replay_smoke, feature_smoke, decision_smoke, execution_smoke,
│   │                                 #   journal_smoke, backtest_smoke, ai_smoke
│   └── common/
│       ├── config/                   # Pydantic-Settings
│       ├── schemas/                  # Pydantic-Schemas (bar, tick, features, journal, AI-Decision)
│       ├── messaging/                # Redis-Streams-Wrapper
│       └── logging/                  # structlog
│
├── tests/                            # pytest, 952 Tests, 100% Coverage auf viz/
│   ├── connectors/                   # test_replay, test_schema_parity, test_paper
│   ├── data/
│   ├── features/
│   ├── decision/                     # test_aggregator, test_scoring, test_ai_schemas,
│   │                                 #   test_openrouter_client, test_ai_layer, test_ai_orchestrator,
│   │                                 #   test_i4_audit
│   ├── execution/
│   ├── journal/
│   ├── backtest/
│   ├── viz/                          # test_overlay_writer, test_bot_overlay_logic
│   └── integration/                  # test_feature_smoke, cross-block
│
└── logs/                             # JSON-Reports aller Smoke-CLIs
```

---

## Quickstart (Replay mode, no MT5 required)

```bash
# 1. Set up Python 3.11+ environment
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# 2. Generate (or refresh) the sample M1 dataset
.venv/bin/python -m tools.generate_sample_data

# 3. Run the replay smoke CLI (10k bars end-to-end)
.venv/bin/python -m xauusd_bot.cli.replay_smoke
# → writes logs/replay_smoke.json
cat logs/replay_smoke.json
```

> **Heads-up (macOS Python 3.14):** some macOS Python 3.14 venvs have a macOS-`UF_HIDDEN` flag
> on `site-packages/` that prevents editable `.pth` files from loading.
> If `python -m xauusd_bot.cli.replay_smoke` fails with
> `ModuleNotFoundError: No module named 'xauusd_bot'`, either:
>
> 1. Use `PYTHONPATH=src .venv/bin/python -m xauusd_bot.cli.replay_smoke`
> 2. Or `pip install --no-build-isolation -e ".[dev]"` to bypass the .pth
> 3. Or rebuild the venv at a path that doesn't carry the hidden flag.
>
> `pytest` is unaffected because `pyproject.toml` sets `pythonpath = ["src"]`.

---

## Quickstart (Full Pipeline, decision_smoke)

```bash
# Replay-Connector → Feature-Engine → Decision-Layer (Rule + AI) → TradeQualification
# → Risk → Execution → Paper-Broker → Journal. 9/200 qualified trades.
PYTHONPATH=src REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.decision_smoke \
    --n-bars 200 --start-bar 2000

# Mit AI-Decision-Layer (zusätzlich zu RuleBasedFallback):
PYTHONPATH=src OPENROUTER_API_KEY=<sk-or-v1-...> \
  REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.decision_smoke \
    --n-bars 200 --start-bar 2000 \
    --use-ai-layer --ai-max-calls 5 --ai-budget-usd 0.01

# AI-Layer standalone smoke (LLM direkt):
PYTHONPATH=src OPENROUTER_API_KEY=<sk-or-v1-...> \
  .venv/bin/python -m xauusd_bot.cli.ai_smoke
# → exit 0, logs/ai_snapshot.json (oder skipped wenn OPENROUTER_API_KEY unset)
```

**Caveats AI-Layer:**
- OpenRouter Zero-Data-Retention wird via `provider.zdr=true` + `data_collection="deny"` im Request-Body erzwungen (nicht via Header — der offizielle OpenRouter-Mechanismus).
- LLM gibt nur decision, entry_type, entry_side, entry_zone, invalidations, management, confidence, comment zurück. KEINE Positionsgröße, SL oder TP (Architektur-Invariante I-4: Brain vs Hands).
- Hard-Rule-Violations (News-Blackout, Zone out of range) → Orchestrator korrigiert auf `no_trade` + LLMFallbackDiscrepancy ins Journal.
- 1 Retry bei Validation/Zone-Fehler; Timeout/5xx/Auth werden nicht retryt (Latenz-Sturm-Vermeidung).
- LLM darf "nein" sagen (Veto) — RuleBasedFallback ist sicherheitsautoritativ, LLM kann ihn nicht überstimmen.

---

## Quickstart (Backtest, Block 5b)

Der BacktestEngine replayed historische M1-Bars durch die gleiche Pipeline wie der Live-Mode. WalkForwardEngine rollt IS/OOS-Windows und flagged Overfit.

```bash
# 1. Single backtest (kein walk-forward), ~20s auf 300 M1-Bars
PYTHONPATH=src REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.backtest_smoke \
    --start-date 2026-04-15 --end-date 2026-04-30 \
    --warmup-bars 200 --max-bars 300

# 2. Mit WalkForward (rolling IS/OOS-Windows), ~3 Min auf 1-Monat-Sample
PYTHONPATH=src REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.backtest_smoke \
    --start-date 2026-04-01 --end-date 2026-04-30 \
    --warmup-bars 100 --max-bars 200 \
    --in-sample-days 7 --out-of-sample-days 3 --step-days 3
```

`logs/backtest_snapshot.json` enthält: `n_bars_processed`, `n_trades`, `stats` (sharpe, sortino, max_dd, profit_factor, expectancy), `equity_curve_sample`, `r_distribution`, `setup_breakdown`, und bei WalkForward: `wf_windows`, `wf_oos_degradation`, `wf_is_overfit`.

**Caveats:**
- Sample-Datensatz ist **synthetisch**; Backtest injiziert synthetic TP-Zonen (real Liquidity-Engines brauchen echte Volume-Cluster). Production-Daten (Dukascopy / Vantage-Export) umgehen das automatisch.
- Slippage/Spread-Modelle: `FixedSlippage`, `VolatilitySlippage`, `FixedSpread`, `VolatilitySpread`, `NewsAwareSpread` — siehe `src/xauusd_bot/backtest/models.py`. Realistic Execution-Modeling ist Block-5c/Demo-Forward-Backlog.
- WalkForward flagged `is_overfit=true` wenn OOS-Sharpe > 30% degradiert vs IS-Sharpe. Heuristik, kein Verdict — review `wf_windows` und per-window stats.
- `--max-bars` cappt inner per-bar cost (default 200 für Smoke, höher für Production-Backtests).

---

## Quickstart (Daily/WeeklyReview + FittingProposal, Block 5c)

Block 5c sammelt Trade-Daten aus dem Journal, ruft den Review-Agent (LLM via OpenRouter) auf und produziert nummerierte, backtest-testbare Vorschläge. FittingProposal-Engine sammelt die Vorschläge mit State-Machine `proposed → backtested → approved/rejected` — **Statuswechsel nur durch Human, niemals automatisch**.

```bash
# 1. Daily Review (gestern oder spezifischer Tag)
PYTHONPATH=src OPENROUTER_API_KEY=<sk-or-v1-...> \
  REDIS_URL=redis://localhost:6379/0 \
  TIMESCALEDB_URL=postgresql+asyncpg://xauusd:xauusd@localhost:5432/xauusd \
  ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.daily_review_smoke --day 2026-04-15
# → exit 0, logs/daily_review.json mit proposals + data_sufficiency
# → ohne OPENROUTER_API_KEY: proposals=[] (gracefully degraded)

# 2. Weekly Review (mit Cross-Day-Patterns)
PYTHONPATH=src OPENROUTER_API_KEY=<sk-or-v1-...> \
  REDIS_URL=redis://localhost:6379/0 TIMESCALEDB_URL=... ENVIRONMENT=test \
  .venv/bin/python -m xauusd_bot.cli.weekly_review_smoke --week-start 2026-04-13

# 3. FittingProposal-Liste
.venv/bin/python -m xauusd_bot.cli.fitting_proposal_smoke --list
.venv/bin/python -m xauusd_bot.cli.fitting_proposal_smoke --list --status proposed
.venv/bin/python -m xauusd_bot.cli.fitting_proposal_smoke --list --overfitting-risk high

# 4. Approve/Reject (manuell, mit Operator-Name)
.venv/bin/python -m xauusd_bot.cli.fitting_proposal_smoke --approve <UUID> --operator lucas --note "tested in backtest, +5% Sharpe"
.venv/bin/python -m xauusd_bot.cli.fitting_proposal_smoke --reject <UUID> --operator lucas --note "already covered by Feature X"

# 5. Auto-Validation gegen BacktestEngine (semi-strukturiert)
#    Parser erkennt Patterns wie "score_threshold=70, IS=4w, OOS=1w".
.venv/bin/python -m xauusd_bot.cli.fitting_proposal_smoke --validate <UUID>
```

**Caveats (volle Liste in `AGENTS.md` §4i):**
- LLM-Vorschläge sind Hypothesen, KEINE Live-Regel-Änderungen. Status `approved` ist manuell getriggert, Auto-Apply auf `settings.*` ist explizit verboten (Caveat 4i.1 + 4i.8, regress-getestet).
- OpenRouter-Config wird mit Block 6 geteilt (gleiche `settings.openrouter_model` / `settings.openrouter_api_key`).
- `min_sample_size` ist konservativ (10 Daily, 30 Weekly). Unter Schwelle: `insufficient_data=True`, kein LLM-Call.
- BacktestSpec-Parser erkennt nur einfache Patterns (`score_threshold=`, `IS=`, `OOS=`, `session=`). Komplexere Proposals bleiben `proposed` bis Operator manuell validiert.
- Daily/Weekly-Reviews laufen per Cron / manueller Trigger, nicht pro Bar (Schedule in Block 10).
- Timescale-Storage für FittingProposal ist Stub (NotImplementedError, asyncpg kommt mit Block 8/10).
- LLM-Schema-Erweiterung-Konvention in `AGENTS.md §3.1` definiert — OpenRouterClient bleibt dünner Transport-Wrapper, Schema-Wissen lebt in den Layer-Clients.

---

---

## Quickstart (MT5-Overlay, Block 7)

`BotOverlay.mq5` ist der MQL5-Indikator, der die vom Bot geschriebene `overlay_levels.json` im MT5-Chart zeichnet. Er ist in CI nicht lauffähig (kein headless MT5 in macOS) — die Logik wird in Python simuliert.

```bash
# 1. Feature-Smoke schreibt overlay_levels.json (passiert automatisch)
PYTHONPATH=src .venv/bin/python -m xauusd_bot.cli.feature_smoke

# 2. MQL5-Simulator testet die Read-Logik (33 Tests, alle grün)
.venv/bin/pytest tests/viz/test_bot_overlay_logic.py -q

# 3. Static-Check auf dem MQL5-File (Brace-Balance, Whitelist, I-1 + I-4)
.venv/bin/python tools/check_mql5_syntax.py
# → OK: mql5/BotOverlay.mq5 (mit 4 WARNs zu user-defined helpers, Best-Effort)

# 4. End-to-End Roundtrip: feature_smoke → Simulator
.venv/bin/python -m tools.run_simulator_against_smoke
# → exit 0, 154 DrawOps (12 HLINE + 133 RECT + 9 LABEL)
```

**Caveats (volle Liste in `AGENTS.md` §4g):**
- MQL5 wird in CI NICHT compiliert — nur Best-Effort Static-Check.
- Python-Simulator testet die File-Read-Logik, NICHT die Chart-Visual. MT5-Chart-Validation bleibt manuell in MetaEditor.
- `prev_*` levels am ersten Tag einer neuen Periode sind `null` — MQL5-Indikator MUSS das gracefully handhaben (regress-getestet).
- FVG-Rechteck-Farben voll opak (MQL5-Limit, User filtert via Chart-Properties).
- Timer 5s = Latency/CPU-Trade-off; ZeroMQ-Push für Real-Time-Sync bleibt Block-8+ Optional.

**Manueller MT5-Chart-Test (Ubuntu/Win, mit MT5 installiert):**
1. `mql5/BotOverlay.mq5` in MetaTrader kopieren: `File → Open Data Folder → MQL5/Indicators/`
2. Im MetaEditor kompilieren (F7) — muss ohne Fehler kompilieren.
3. Im Chart: `Insert → Indicators → Custom → BotOverlay`
4. `MQL5/Files/overlay_levels.json` muss existieren (vom Bot geschrieben) — BotOverlay liest sie alle 5s.

---

## Quickstart (Custom Web-Dashboard, Block 9)

Block 9 liefert ein FastAPI-Backend + Single-Page HTML/JS-Frontend mit Lightweight-Charts (TradingView), WebSocket für Real-Time-Updates, Multi-User-Auth (Cookie-Session, 3 Rollen), Backtest-Trigger, Review- und FittingProposal-Panels, und Live-Mode-Toggle (admin-only).

```bash
# 1. .env setup — bcrypt-Hash für dein Passwort generieren
.venv/bin/python -c "import bcrypt; print(bcrypt.hashpw(b'mein_passwort', bcrypt.gensalt()).decode())"
# → Ausgabe in .env als DASHBOARD_USERS={"lucas": {"password_hash": "<hash>", "role": "admin"}}

# 2. .env: Dashboard aktivieren
cat >> .env <<EOF
DASHBOARD_ENABLED=true
DASHBOARD_USERS={"lucas": {"password_hash": "<hash>", "role": "admin"}}
DASHBOARD_LIVE_MODE_ENABLED=true   # nur wenn du Live-Mode-Toggle willst
EOF

# 3. Dashboard lokal starten
.venv/bin/python -m xauusd_bot.dashboard.app
# → serving on http://127.0.0.1:8080

# 4. Browser: http://127.0.0.1:8080 → Login → Chart + Tabs sichtbar

# 5. Auf Ubuntu-VM via Docker
docker compose -f docker-compose.base.yml -f docker-compose.prod.yml up -d dashboard
# → http://127.0.0.1:8080 (Cloudflare-Tunnel davor für Remote)
```

**Caveats (volle Liste in `AGENTS.md` §4j, 17 Einträge):**
- `DASHBOARD_ENABLED=false` per default — explizit aktivieren.
- Loopback-only (`127.0.0.1:8080`), Cloudflare-Tunnel für Remote.
- Cookie-Sessions in separater Redis-DB (`/1`), Trading-Streams bleiben auf `/0`.
- 3 Rollen: `viewer` (read-only), `operator` (approve proposals + backtest), `admin` (Live-Mode-Toggle).
- Bcrypt-Hashes, NIEMALS Klartext-Passwörter in .env oder Logs.
- Mode-Toggle immer mit Confirmation-Modal, Aktion wird geloggt.
- Single-Page, kein Routing, M5-Chart default (andere TFs verfügbar).
- Frontend ist read-only by default — Live-Trades laufen weiter über den Trading-Prozess, nicht über das Dashboard.

---

## Quickstart (Docker, dev)

```bash
cp .env.example .env
# edit OPENROUTER_API_KEY if you want to exercise the AI layer

docker compose -f docker-compose.base.yml -f docker-compose.dev.yml up
```

`docker-compose.dev.yml` excludiert den `mt5-terminal`-Service und setzt `CONNECTOR_MODE=replay`, damit die volle Pipeline auf macOS ohne Wine läuft.

## Quickstart (Docker, prod, Block 8)

Auf einer Ubuntu-VM mit Wine-fähigem Kernel:

```bash
cp .env.example .env
# fill in: MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_BRIDGE_AUTH_KEY, OPENROUTER_API_KEY

docker compose -f docker-compose.base.yml -f docker-compose.prod.yml up
```

Der `mt5-terminal`-Container bringt Wine + MetaTrader 5 + Windows-Python +
RPyC-Bridge mit. Build-Dauer beim Erstbuild: 10–15 Min (Wine + MT5 + Win-Python);
Re-Deploys nach Code-Änderungen am Bridge-Server: ~30s (Layer-Cache).

**Erstmaliges Vantage-Login:**

1. Browser öffnen: <http://127.0.0.1:6080/vnc.html> (noVNC-Web, loopback-only).
2. Im MT5-Terminal den Vantage-Demo-Login durchführen (Tools → Options → Server).
3. VNC nach erfolgreichem Login schließen (oder Port-Mapping
   ``127.0.0.1:5900:5900`` aus `docker-compose.prod.yml` rausnehmen).
4. Vom Host aus: `python -c "from xauusd_bot.connectors.live import LiveMT5Connector; c = LiveMT5Connector(host='127.0.0.1', port=18812, login=YOUR_LOGIN, password='YOUR_PASS', server='VantageInternational-Demo', auth_key='YOUR_BRIDGE_KEY'); print(c.get_account())"`.

**VNC-Sicherheit:** Niemals die Ports 5900/6080 auf 0.0.0.0 exposen.
Falls Remote-Zugriff nötig: Cloudflare Zero Trust Tunnel davor.

---

## Architektur-Invarianten (enforced via Tests + Audits)

1. **I-1: Connector-Isolation.** `import MetaTrader5` (oder `from MetaTrader5`) AUSSCHLIESSLICH in `docker/mt5-terminal/mt5_bridge_server.py` (Windows-Python / Wine). **Seit Block 8 (2026-06-17) auch nicht mehr in `src/xauusd_bot/connectors/live.py`** — der Linux-Connector ist ein reiner RPyC-Client. Alle anderen Module importieren `IMarketConnector` (Protocol aus `connectors/base.py`).
2. **I-2: Schema-Parität Replay ↔ Live.** `ReplayConnector` und `LiveMT5Connector` liefern identische Methodensignaturen und Rückgabe-Typen — enforced by `tests/connectors/test_schema_parity.py` (44 Tests, inkl. Live-Mock).
3. **I-3: Point-in-Time (PIT).** `ReplayConnector` liefert NUR Bars/Ticks mit `time <= current_t`. `advance_time(t)` ist monoton, time-travel backwards → `ValueError`.
4. **I-4: Brain vs Hands.** Der AI-Decision-Layer (Block 6) berechnet NIEMALS Positionsgröße, SL oder TP. LLM-Output ist strikt JSON via Pydantic, ungültig → 1 Retry → `no_trade`. RuleBasedFallback ist sicherheitsautoritativ — LLM-Veto gewinnt nie gegen harte Regeln.
5. **I-5: Tick-Volume nur relativ.** `Bar.tick_volume` ist Perzentil/Z-Score-Input, nie absolutes Signal.

**Adversarielle Audits pro Block:** Die Test-Suite enthält für jeden Block canary-Tests (z.B. `tests/decision/test_i4_audit.py`), die prüfen, dass die hartkodierten Regeln nicht durch Refactoring verloren gehen.

---

## Test-Übersicht

```bash
# Full suite (952 Tests, ~2.5 Min auf Mac)
PYTHONPATH=src:. .venv/bin/pytest -q --no-header

# Per-Block-Suites
.venv/bin/pytest tests/connectors/ tests/data/ -q       # Block 1
.venv/bin/pytest tests/features/ -q                     # Block 2
.venv/bin/pytest tests/decision/test_aggregator.py tests/decision/test_scoring.py -q  # Block 3
.venv/bin/pytest tests/execution/ -q                    # Block 4
.venv/bin/pytest tests/journal/ -q                      # Block 5a
.venv/bin/pytest tests/backtest/ -q                     # Block 5b
.venv/bin/pytest tests/decision/test_ai_*.py tests/decision/test_openrouter_client.py -q  # Block 6
.venv/bin/pytest tests/viz/ -q                          # Block 7
```

Smoke-CLIs (run real pipeline, JSON in `logs/`):

```bash
.venv/bin/python -m xauusd_bot.cli.replay_smoke
.venv/bin/python -m xauusd_bot.cli.feature_smoke
.venv/bin/python -m xauusd_bot.cli.decision_smoke --n-bars 200 --start-bar 2000
.venv/bin/python -m xauusd_bot.cli.execution_smoke --force-trade
.venv/bin/python -m xauusd_bot.cli.journal_smoke --n-bars 200 --start-bar 2000
.venv/bin/python -m xauusd_bot.cli.backtest_smoke --start-date 2026-04-15 --end-date 2026-04-30 --warmup-bars 200 --max-bars 300
.venv/bin/python -m xauusd_bot.cli.ai_smoke    # OPENROUTER_API_KEY optional
```

---

## Build-Roadmap (Details in `00_FINAL_PLAN.md` §9)

1. ✅ Repo-Skeleton + Docker + Connector-Interface
2. ✅ ReplayConnector + PaperBroker + Sample-Datensatz
3. ✅ Data Layer: OHLCBuilder, SpreadMonitor, DataQualityMonitor
4. ✅ Basis-Features: Session, TripleVWAP, MarketStructure, Candle/Momentum
5. ✅ FixedVolumeRangeEngine (Weekly → Monthly → Yearly)
6. ✅ FVG + Liquidity Engine
7. ✅ NewsContextEngine + Kalender-API
8. ✅ FeatureAggregator + ScoringEngine
9. ✅ Execution MVP: RiskManager, PositionSizer, OrderManager, SL/TP
10. ✅ TradeJournalDB + FeatureSnapshotStore (TimescaleDB)
11. ✅ AIDecisionLayer (OpenRouter) parallel zu RuleBasedFallback
12. ✅ MT5-Viz-Bridge + `BotOverlay.mq5`
13. ✅ BacktestEngine + WalkForwardEngine
14. ⏳ Daily/WeeklyReview + FittingProposal
15. ⏳ LiveMT5Connector + mt5-terminal-Container (Wine-Bridge)
16. ⏳ Custom Web-Dashboard
17. ⏳ Demo-Forward auf Ubuntu → Monitoring → (erst dann) Live

(15 von ~17 Punkten ship-ready, 2 offen — Block 10 = Demo-Forward+Live auf Ubuntu-VM.)

---

## Push-Workflow (an Remote)

```bash
# Status check
git status
git log dev --oneline -5

# Push (per expliziter User-Freigabe, nicht automatisch)
git push origin dev
```

Remote: `origin` = `https://github.com/forliHD/GoldManager.git` (GitHub, `forliHD`-Account). Commits werden lokal auf `dev` angehäuft und per expliziter Freigabe gepusht — kein Auto-Push.

---

## Disclaimer

Das ist ein Software-Projekt, keine Finanzberatung. Bis Block 10 (Demo-Forward) nur auf Demo-Accounts testen. Live-Trading erst nach expliziter Freigabe und mit Mini-Volumen.
