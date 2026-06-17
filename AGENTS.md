# GoldManager — Project Memory (AGENTS.md)

> Persistente Architektur-Invarianten, Plan-Stand und Live-Bugs, die jede
> zukünftige Session (Worker oder Orchestrator) kennen muss. Wird vom
> Orchestrator gepflegt, von Workern gelesen.

## 1. Projekt-Kontext

- **Was:** XAUUSD-Trading-Bot. Replay-Mode auf Mac (Dev) → LiveMT5Connector
  über Wine-Bridge auf Ubuntu-VM (Prod). Siehe `00_FINAL_PLAN.md` für die
  komplette Spezifikation — dies hier ist die **operative Kurzfassung**.
- **Stack:** Python 3.11+ (lokal 3.14), pydantic v2, pydantic-settings,
  pandas, pyarrow, redis, structlog, fastapi, pytest. Docker: redis,
  timescaledb, service-images, mt5-terminal (STUB).
- **Roadmap:** 16 Schritte in `00_FINAL_PLAN.md §9`. Build-Status siehe
  Abschnitt 2 unten.

## 2. Build-Status

| Block | Inhalt | Status |
|-------|--------|--------|
| 1 | Repo-Skeleton, Docker-Stack, Connector-Abstraktion, Replay/Paper, Data Layer | ✅ ship-ready, dev-branch |
| 2 | Feature-Engine (Session, Triple-VWAP, FixedVolumeRange, FVG, MarketStructure, CandleMomentum, Liquidity, News) + Overlay-Writer | ✅ ship-ready, dev-branch |
| 3 | Aggregator + Scoring + RuleBasedFallback + TradeQualification | ✅ ship-ready, dev-branch |
| 4 | Execution + Risk + Pending/Stop/TP + EmergencyStop | ✅ ship-ready, dev-branch |
| 5a | TradeJournalDB (TimescaleDB) + FeatureSnapshotStore + Read-API (queries) | ✅ ship-ready, dev-branch |
| 5b | BacktestEngine (Event-Replay über ReplayConnector) + WalkForwardEngine | ✅ ship-ready, dev-branch |
| 5c | Daily/WeeklyReview + FittingProposal | offen |
| 6 | AIDecisionLayer (OpenRouter) parallel zu RuleBasedFallback | ✅ ship-ready, dev-branch |
| 7 | MT5-Viz-Bridge + `BotOverlay.mq5` (MQL5-Indikator + Python-Simulator + Static-Check) | ✅ ship-ready, dev-branch |
| 8 | LiveMT5Connector (RPyC) + mt5-terminal-Container (Wine) | offen |
| 9 | Custom Web-Dashboard (eigenes Chart + Indikatoren-UI, webbasiert) | offen |
| 10 | Demo-Forward auf Ubuntu → Monitoring → (erst dann) Live | offen |

**Roadmap-Anpassung 2026-06-16 (Lucas):** Custom-Dashboard wurde von
"optional nach Block 9" zu **eigenem Block 9** hochgestuft. Demo-Forward
+ Live verschiebt sich auf Block 10. Begründung: Du willst die Indikatoren
zusätzlich zum MT5-Overlay auch in einem webbasierten Dashboard sehen
(Backtests, Replay, Live-Monitoring aus einer UI).

Producer-Commits landen auf `dev`. Remote: `origin` =
`https://github.com/forliHD/GoldManager.git`. Push-Workflow: lokale
Commits auf `dev` anhäufen, dann `git push origin dev` per expliziter
User-Freigabe (nicht automatisch).

**E2E-Integration (Stand 2026-06-17):** Replay-Connector → Feature-Engine →
Decision-Layer (Rule + AI) → TradeQualification → Risk → Execution →
Journal → KPI Pipeline-Smoke grün. Gesamte Test-Suite: **952 passed**
(Block 1: 217, Block 2: 70, Block 3: 85, Block 4: 117, Block 5a: 114,
Block 5b: 143, Block 6: 72, Block 7: 41). Alle Architektur-Invarianten
I-1..I-5 re-verifiziert.

**Meilensteine:**
- 2026-06-15: Block 1-4 ship-ready, 511 Tests, E2E-Smoke grün
- 2026-06-15: Block 5a (Journal) ship-ready, 625 Tests
- 2026-06-16: Block 5b (Backtest) ship-ready, 838 Tests
- 2026-06-16: Block 6 (AI Layer, v2 spec-conformance) ship-ready, 911 Tests
- 2026-06-17: Block 7 (MT5-Viz-Bridge + BotOverlay.mq5) ship-ready, 952 Tests
- 2026-06-17: AGENTS.md §4g (Block-7-Caveats) ergänzt, Memory + MQL5-Sim-Pattern
- 2026-06-17: `origin` = `https://github.com/forliHD/GoldManager.git` aktiv,
  dev-Branch 9 Commits ahead of origin/dev, Push-Workflow etabliert.

**Block-4 Lifecycle-Smoke (Stand 2026-06-15):** `execution_smoke --force-trade`
läuft komplette Lifecycle (risk → size → stops → order → sweep → trail) mit
Exit 0, plausibler `logs/execution_lifecycle.json`. `--simulate-losses 5`
triggert nachweisbar die Tages-Pause (EmergencyStop). Coverage execution/ = 92%
(Ziel ≥75%).

**Block-5a Journal-Smoke (Stand 2026-06-15):** `journal_smoke --n-bars 200
--start-bar 2000` läuft Replay → Features → Decision → TradeQualification →
Risk → Size → Stops → Order → PaperBroker → JournalStore → KPI-Aggregation.
Exit 0, 5 Trades + 200 Snapshots + 5 Orders in `logs/journal_snapshot.json`.
Coverage journal/ = 98% (Ziel ≥75%), common.schemas.journal = 100%.
TimescaleJournalStore ist Stub (Block 5b liefert asyncpg-Integration).

**Block-5b BacktestEngine + WalkForwardEngine (Stand 2026-06-16):**
`backtest_smoke --start-date 2026-04-01 --end-date 2026-04-02
--warmup-bars 50 --max-bars 30 --skip-walkforward` läuft komplette
Pipeline (Replay → Features → Decision → TradeQualification → Risk →
Size → Stops → Order → BacktestEngine → Aggregates) in ~5s, Exit 0,
plausible `logs/backtest_snapshot.json` (n_bars=30, n_trades=1,
stats, r_distribution, breakdowns, equity_curve_sample, tags).
`--in-sample-days 1 --out-of-sample-days 1 --step-days 1` aktiviert
WalkForwardEngine, liefert 1+ Windows, robustness_matrix,
`is_overfit`-Flag. Coverage backtest/ = 83% (Ziel ≥75%), 143 neue
Tests, gesamt 838 passed (vorher 695).

**Block-6 AIDecisionLayer + AIDecisionOrchestrator (Stand 2026-06-16):**
`python -m xauusd_bot.cli.ai_smoke` läuft End-to-End mit
OpenRouter, wenn `OPENROUTER_API_KEY` gesetzt ist (sonst
skipped, Exit 0). `decision_smoke --use-ai-layer --ai-max-calls 5
--ai-budget-usd 0.01` ruft die AI-Schicht zusätzlich auf
High-Score-Bars und loggt `ai_comparison` in
`logs/decision_snapshot.json`. 72 neue Tests
(test_ai_schemas 18 + test_openrouter_client 18 +
test_ai_layer 11 + test_ai_orchestrator 24 + 1 ai-smoke fix),
gesamt 911 passed (vorher 839). Coverage decision/ = 86%
(ai_layer 94%, ai_orchestrator 94%, openrouter_client 86%).

I-1 + I-4 re-verifiziert: keine `MetaTrader5`-Imports in
`decision/` oder `cli/ai_smoke.py`; keine
`position_size/lot_size/stop_loss/take_profit/sl_price/tp_price/VolumeInLots`
in den AI-Code-Statements (nur in Docstrings). LLM niemals mit
Account-PII gefüttert (`_account_redacted()`-Whitelist in
`ai_layer.py`); LLM-OUTPUT wird gegen die Snapshot-Zonen
plausibilisiert (LLMZoneViolation) und gegen News-Blackout
geprüft (LLMHardRuleViolation). RuleBasedFallback bleibt
sicherheitsautoritativ — LLM-Veto erlaubt, LLM kann keine harten
Regeln aushebeln.

**Block-7 BotOverlay.mq5 + Python-Simulator (Stand 2026-06-17):**
`mql5/BotOverlay.mq5` (178 LoC, stdlib-only, Timer 5s) liest
`MQL5/Files/overlay_levels.json` und zeichnet VWAPs (3) +
Volume-Profile (6 Perioden × 3 Levels + Value-Area-Rect) +
FVG-Rechtecke (N). `tests/viz/test_bot_overlay_logic.py` (31
neue Tests) spiegelt die File-Read-Logik in Python und prüft 20+
Edge-Cases (Null-Felder, korruptes JSON, fehlende Datei, prev_*=null
am ersten Tag einer neuen Periode, Style-Matrix für developing/locked
+ prev_*). `tools/check_mql5_syntax.py` (Brace-Balance + Function-
Whitelist + Python-Import-Ban + I-4-String-Ban) läuft OK auf
`mql5/BotOverlay.mq5`. `tools/run_simulator_against_smoke.py`
orchestriert `feature_smoke` → Simulator, produziert 154 DrawOps
(12 HLINE + 133 RECT + 9 LABEL) auf dem echten Sample-Datensatz.
Gesamt-Suite: **952 passed** (vorher 911). Coverage viz/ = 100%
(bot_overlay_simulator + overlay_writer).

## 3. Architektur-Invarianten (HART — nicht verletzen)

Diese Invarianten werden im Code UND in der Verifikation durchgesetzt. Jeder
Worker, der sie bricht, macht den Block ungültig.

### I-1: Connector-Isolation
- `import MetaTrader5` (oder `from MetaTrader5`) darf AUSSCHLIESSLICH in
  `src/xauusd_bot/connectors/live.py` und in `docker/mt5-terminal/`
  vorkommen.
- Alle anderen Module importieren `IMarketConnector` (Protocol aus
  `connectors/base.py`).
- Verifikation: `grep -rn "import MetaTrader5\|from MetaTrader5" src/ tests/ tools/`
  darf nur die erlaubten Stellen treffen.

### I-2: Schema-Parität Replay ↔ Live
- `ReplayConnector` und `LiveMT5Connector` MÜSSEN identische
  Methodensignaturen UND Rückgabe-Typen liefern.
- Erzwingt durch `tests/connectors/test_schema_parity.py` (38 Tests).
- Konkret implementiert via `inspect.signature`-Vergleich der 11
  Protocol-Methoden.

### I-3: Point-in-Time (PIT) — kein Look-ahead
- `ReplayConnector` liefert NUR Bars/Ticks mit `time <= current_t`.
- `advance_time(t)` ist monoton, time-travel backwards → `ValueError`.
- `end_time` Parameter ÜBERSCHREIBT den Cursor (siehe Caveat I-3a).
- Verifikation: `tests/connectors/test_replay.py::test_replay_never_returns_future_bars`
  + Smoke-CLI `point_in_time_ok=true` in `logs/replay_smoke.json`.

#### I-3a: Caveat — `end_time` Override
- **Stand:** In Block 1 setzt `end_time` (wenn übergeben) den cutoff direkt,
  OHNE auf `current_t` gecappt zu werden. Ein Caller, der `end_time > current_t`
  übergibt, bekommt Look-ahead.
- **Workaround in Smoke-CLI:** `end_time = current_t` setzen.
- **TODO für Block 2:** Hardening — `cutoff = min(end_time, current_t)`. VOR
  dem ersten Backtest-Fix unbedingt einbauen, sonst korrumpierte Backtest-
  Ergebnisse.

### I-4: Brain vs Hands
- Der AI-Decision-Layer (Block 6) berechnet NIEMALS Positionsgröße, SL
  oder TP. Das macht deterministisch die Execution-Engine.
- LLM-Output ist strikt JSON via Pydantic validiert. Ungültig → 1 Retry
  → `no_trade`.
- RuleBasedFallback ist sicherheitsautoritativ. LLM-Veto gewinnt nie
  gegen harte Regeln (News-Blackout, Risk-Limits, etc.).

### I-5: Tick-Volume nur relativ
- `Bar.tick_volume` ist ein Perzentil/Z-Score-Input, nie ein absolutes
  Signal. Konsumenten (Feature-Engine) sind verantwortlich für
  Normalisierung.

## 4. Hardening-Caveats (aus Block-1-Review)

Diese sind KEINE Blocker, aber VOR Block 2 (oder spätestens vor dem
ersten Backtest) zu fixen:

1. **Caveat I-3a (FIXED in Block 2):** `end_time` Override in
   `replay.py` — jetzt wird `cutoff = min(end_time, current_t)` mit
   Debug-Log verwendet. Regression-Test:
   `tests/connectors/test_replay.py::test_end_time_above_current_t_is_capped`.
2. **Pydantic-Positivität:** `Bar`/`Tick`/`AccountInfo` haben keine `gt=0`
   Constraints auf Preis/Balance/Spread-Feldern. Domain-Validation in
   Block 2 hinzufügen, wo die Felder tatsächlich verwendet werden.
3. **`.env`-Empfindlichkeit:** `test_settings_openrouter_optional` reagiert
   auf empty-string vs unset. In `.env.example` dokumentieren dass
   `OPENROUTER_API_KEY=""` (leerer String) **ungleich** "unset" ist und zu
   `SecretStr('')` führt. Pydantic-Settings-Test toleranter machen oder
   `SecretStr` in der Test-Fixture explizit setzen.

## 4c. Caveats aus Block 4

Diese Caveats sind KEINE Blocker, aber zu beachten für Block 5+:

1. **RiskManager PnL ist in-memory only.** Der `record_pnl()`-State
   wird NICHT in TimescaleDB persistiert. Nach einem Prozess-Restart
   beginnt der Tag/Woche-Counter bei Null. Block 5 (Journal) muss
   die PnL-Historie aus dem Journal-Tag-Stream rekonstruieren oder
   den State in Redis ablegen.
2. **HTF-Profile nutzen `developing`-Werte** für den Runner-TP3. Wenn
   die aktuelle Woche noch nicht abgeschlossen ist, kann der VAH/VAL
   sich noch verschieben. Der Runner-Lock akzeptiert das bewusst —
   der Executor prüft den Level alle N Bars neu.
3. **Notional / Margin-Berechnung** ist derzeit nur eine grobe
   Schätzung im PaperBroker (nicht im OrderManager). Für Live-Mode
   muss Block 5 (oder 8) die echte MT5-Margin-API anbinden.
4. **PreTradeSafetyChecker** nutzt einen Stub `get_spread_points`
   wenn kein `SpreadMonitor` angeschlossen ist. In Production
   `xauusd_bot.data.spread_monitor.SpreadMonitor` einklinken.
5. **EmergencyStop `state_file`** wird per Default relativ zum
   Report-Pfad des Smoke-CLI geschrieben. In Production sollte das
   ein absoluter Pfad sein (z.B. `/var/lib/xauusd/emergency_state.json`).

## 4d. Caveats aus Block 5a

Diese Caveats sind KEINE Blocker, aber zu beachten für Block 5b+:

1. **TimescaleJournalStore ist Stub.** Die asyncpg-Integration
   kommt in Block 5b (BacktestEngine). Bis dahin liefert
   `get_journal_store()` in `production`-Umgebung eine
   `TimescaleJournalStore`-Instanz zurück, aber
   `get_journal_store_with_fallback()` (mit Connectivity-Probe)
   fällt sofort auf `InMemoryJournalStore` zurück. In Production
   muss Block 5b die Probe implementieren, sonst riskiert man
   dass jeder Schreibvorgang in den Stub läuft und `NotImplementedError`
   wirft.
2. **PnlRealized ist in der Smoke synthetisch.** Der
   `journal_smoke`-CLI synthetisiert Close-Daten via
   `future_bars[0..close_window]`. Das ist KEIN echter
   Backtest — Block 5b nutzt denselben Engine-Stack mit echtem
   Bar-Walk und einem echten PendingOrderManager.
3. **`feature_snapshot_id` ist ein Soft-FK.** Der Store
   validiert nicht, dass die referenzierte Snapshot existiert
   (Block 5b: das wird die TimescaleDB mit echter FK-Constraint
   lösen). Der ReviewAgent (Block 5c) MUSS beim Lesen prüfen,
   dass `feature_snapshot.bar_time <= trade.timestamp_open`.
4. **Coverage bezieht sich nur auf `journal/` + Schemas.**
   Das `cli/journal_smoke.py` ist im Test-Prozess via
   `subprocess` gestartet — Coverage-Trace zeigt "never imported".
   Das ist OK weil die Smoke-Tests die volle Lifecycle
   End-to-End abdecken; eine Coverage-Zahl für die CLI ist
   ohne Mehraufwand ermittelbar (`runpy` import-Patch).
5. **`update_trade` erlaubt nur die dokumentierten Felder.**
   Jeder Versuch, ein anderes Feld (z.B. `entry_price`,
   `score`) zu mutieren, wirft `PITViolationError`. Wer einen
   neuen Close-Field hinzufügen will, muss `_ALLOWED_TRADE_UPDATES`
   in `src/xauusd_bot/journal/store.py` erweitern.

## 4e. Caveats aus Block 5b (BacktestEngine + WalkForwardEngine)

Diese Caveats sind KEINE Blocker, aber zu beachten für Block 5c
(Daily/WeeklyReview) und Block 6+:

1. **`context_window_bars` ist ein Backtest-only Knob.** Der
   :class:`BacktestEngine` füttert die Feature-Engines pro
   Decision-Bar mit den letzten ``context_window_bars`` (default
   1500) Bars. Live-Mode übergibt kumulativ alle bisherigen
   Bars. Das ist absichtlich — der Backtest bouncet O(N²) ab,
   der Live-Mode braucht keine Cap. Beim Vergleich von
   Backtest- vs. Live-KPIs beachten: ein Backtest, der mit
   ``context_window_bars=1500`` läuft, sieht weniger
   „Long-History" als die Live-Instanz, aber das macht für die
   Engines (die effektiv nur die letzten ~1500 Bars
   anschauen) keinen praktischen Unterschied.
2. **Synthetic-Data TP-Zone-Injection in `_build_bundle()`.**
   Die Sample-Daten (`xauusd_m1_sample.parquet`) haben keine
   echten Liquidity-Zonen. Die :class:`TradeQualificationEngine`
   blockt dann immer auf `no_clear_tp_target`. Der Backtest
   injiziert synthetische TP-Zonen ober- und unterhalb des
   aktuellen Close (genau wie `journal_smoke`). **Auf
   Real-Daten** (z.B. Dukascopy-CSV-Export) verschwindet dieser
   Hack automatisch, weil echte Liquidity-Engines echte
   Cluster liefern.
3. **`max_bars_per_window` cappt die inneren
   BacktestEngine.run()-Calls in WalkForward.** Ohne diesen
   Cap hängt ein WF mit 14d IS × 1d OOS × 1d step über
   25 Tage ≈ 30 Minuten (O(N²) der Engines summiert sich
   schnell). Mit `max_bars_per_window=200` läuft derselbe
   WF in ~10s. **Trade-off:** in der Smoke wird der
   Feature-Engine-Lookback effektiv auf 200 Bars beschnitten
   — für Production-Backtests mit echtem Dukascopy-Datensatz
   den Cap höher setzen (oder ganz weglassen).
4. **`compute_sortino` wurde in `journal/queries.py` ergänzt.**
   Der :class:`BacktestStats.sharpe` benutzt weiterhin
   `compute_sharpe` (volatilitäts-basiert). Der
   `sortino`-KPI nutzt nur die Downside-Deviation und ist
   daher konservativer — typisch für Block-5c-Reviews.
5. **WalkForwardEngine `_add_months` clamped Month-End.**
   `Jan 31 + 1m → Feb 28/29` (kein Schaltjahr-Edge-Case). Wer
   mit Day-basierten Windows arbeitet (`in_sample_days=…`),
   bekommt das Calendar-Bug-Problem nicht.
6. **Reuse von `ReplayConnector` zwischen WF-Windows.**
   Der :class:`BacktestEngine.run` setzt den Connector-Cursor
   am Anfang auf 1µs vor dem ersten Bar zurück (siehe
   `engine.py:425-435`). Das erlaubt es der
   :class:`WalkForwardEngine`, einen einzigen
   :class:`ReplayConnector` über mehrere Windows hinweg
   wiederzuverwenden, ohne dass `advance_time` einen
   "Time travel not allowed"-Fehler wirft. **Block 5c:** wenn
   der ReviewAgent ebenfalls multi-window-Analysen macht,
   kann er das gleiche Muster übernehmen.

## 4f. Caveats aus Block 6 (AIDecisionLayer + AIDecisionOrchestrator)

Diese Caveats sind KEINE Blocker, aber zu beachten für Block 5c
(Daily/WeeklyReview) und Block 7+ (Production):

1. **OpenRouter ZDR ist ein Body-Feld, kein HTTP-Header.** Die
   offizielle OpenRouter-API (siehe
   `https://openrouter.ai/docs/guides/routing/provider-selection`)
   steuert Zero-Data-Retention über `provider.zdr: true` im
   Request-Body, **nicht** über einen `X-Privacy-Mode`-Header. Der
   Task-Brief erwähnte einen Header — diese Annahme war falsch.
   :class:`xauusd_bot.decision.openrouter_client.OpenRouterClient`
   setzt `provider.zdr=true` UND `provider.data_collection="deny"`
   wenn `settings.ai_layer_zdr=True` (default). Falls OpenRouter
   später einen echten Header hinzufügt, muss der Client
   nachgezogen werden — bis dahin ist Body-Routing die einzige
   offiziell unterstützte Methode.

2. **Score-Threshold-Gate ist im Orchestrator, nicht im Layer.**
   :class:`AIDecisionOrchestrator` short-circuited bei
   `score.total < settings.ai_layer_score_threshold` (default 65)
   auf den RuleBasedFallback, **ohne** den LLM zu rufen. Das
   spart Kosten + Latenz auf der Mehrheit der Bars. Wer den
   Layer direkt aufruft (z.B. ein Ad-hoc-Skript), MUSS den
   Score-Check selbst machen — der Layer nimmt jeden Aufruf
   entgegen und ruft OpenRouter an.

3. **LLM darf "nein" sagen (Veto erlaubt).** Per I-4 ist der
   RuleBasedFallback sicherheitsautoritativ (kann LLM vetieren),
   aber die LLM darf auch ein "no_trade" auf ein "enter" des
   Fallbacks setzen. Konkrete Aufzeichnung: der
   :class:`xauusd_bot.common.schemas.journal.LLMFallbackDiscrepancy`
   wird mit `resolution=LlmVetoed` ins Journal geschrieben. Bei
   `score.total >= 65` und "no_trade" von beiden → AGREEMENT.

4. **Cost-Budget ist ein Smoke-Guard, kein Production-Limit.**
   `ai_smoke.py` hat `--max-budget-usd` (default 0.01) als
   Hard-Cap; `--use-ai-layer` im `decision_smoke` hat
   `--ai-budget-usd` (default 0.01) und `--ai-max-calls`
   (default 5). Im Live-Betrieb (Block 8) muss ein Production-
   Budget eingeführt werden — z.B. via Redis-Counter, der den
   Tagesverbrauch trackt und ab einem Limit auf RuleOnly
   umschaltet.

5. **1 Retry nur bei Validation/Zone-Errors, NICHT bei
   Timeout.** Der Orchestrator's `_call_with_retry` retryt
   EINMAL bei `LLMValidationError` und `LLMZoneViolation`.
   `LLMTimeoutError` / `LLMServerError` werden **nicht** retryt
   (verhindert Latenz-Stürme wenn der Provider down ist). Bei
   `LLMAuthError` sowieso kein Retry (Bug in der Config).

6. **Hard-Rule-Violation (LLMHardRuleViolation) wird nicht retryt.**
   Wenn das LLM z.B. einen Entry trotz `news_in_blackout`
   vorschlägt, ist das ein Domain-Fehler — Retry würde nur
   denselben Fehler produzieren. Orchestrator → Fallback +
   Discrepancy.

7. **System-Prompt wird beim Init geladen und gecached.** Der
   :class:`OpenRouterClient` liest `decision_agent.md` einmal
   beim `__init__` und cached das Ergebnis. Änderungen am
   Prompt erfordern einen Prozess-Neustart. Im Production
   (Block 8) muss das entweder via SIGHUP-Reload oder via
   periodischem Re-Read (z.B. alle 5 Min) gelöst werden.

8. **Account-PII wird vor dem LLM gestrippt.** Der
   :class:`AIDecisionLayer` schickt nur `current_spread_points`,
   `trade_allowed`, `server_time` an das LLM — niemals
   `balance`, `equity`, `login`, `daily_pnl`, `weekly_pnl`,
   `leverage`, `broker`, oder `raw`. Dies ist eine harte
   Privacy-Garantie; jede zukünftige Erweiterung des Payloads
   muss gegen die `_account_redacted()`-Whitelist in
   `ai_layer.py` geprüft werden.

9. **I-4-Audit deckt die neuen Module ab.** `tests/decision/test_i4_audit.py`
   parametrisiert über alle `.py`-Dateien in `decision/`
   inkl. der drei neuen — die AST-Heuristik fängt jeden
   Code-Use von `position_size`, `lot_size`, `stop_loss`,
   `take_profit`, `sl_price`, `tp_price`, `VolumeInLots`. Doc-
   string-Mentions sind erlaubt.

## 4b. Caveats aus Block 2

Diese Caveats sind KEINE Blocker, aber zu beachten:

1. **Overlay `prev_*` Levels am ersten Tag einer neuen Periode sind `null`.**
   Wenn z.B. der Bot am 1. Januar startet, gibt es kein `prev_year`-Profil
   (das Jahr 2025 existiert noch nicht im Journal). Der Overlay-Writer
   schreibt `null` in das JSON, und `BotOverlay.mq5` muss das
   gracefully handhaben (Linie weglassen, nicht crashen). Block 7
   (MT5-Viz-Bridge) muss das in der MQL5-Indikator-Logik
   berücksichtigen.
2. **Feature-Engine nutzt I-3 PIT-Garantie** — alle Module
   (`features/*.py`) MÜSSEN die `cutoff`/`current_t`-Semantik aus
`ReplayConnector` respektieren. Niemals direkt `bar.time`
    vergleichen ohne Vorab-Check `bar.time <= current_t`.

## 4g. Caveats aus Block 7 (BotOverlay.mq5 + Python-Simulator)

Diese Caveats sind KEINE Blocker, aber zu beachten für Block 8
(LiveMT5Connector) und Block 9 (Custom Web-Dashboard):

1. **MQL5-Code wird in CI NICHT compiliert/getestet** — nur der
   Best-Effort Static-Check in `tools/check_mql5_syntax.py` (Brace-
   Balance + Function-Whitelist + Python-Import-Ban + I-4-String-Ban).
   Echte MT5-Chart-Validation ist manuell in MetaEditor (kompilieren,
   auf Chart droppen, visuell prüfen). Vor jedem Block-7-Change: den
   Static-Check laufen lassen UND das Chart manuell inspizieren.

2. **Python-Simulator testet die File-Read-Logik, NICHT die
   Chart-Visual.** Wenn der MQL5-Indikator in MetaEditor compiliert
   aber visuell was Falsches zeichnet (z.B. Linie an falscher Y-Position,
   OBJ_HLINE statt OBJ_TREND auf einen Indikator-Buffer), fängt das
   der Simulator nicht. → manueller Visual-Check bei jedem MQL5-Change.
   Regression-Test für den Simulator:
   `tests/viz/test_bot_overlay_logic.py`.

3. **`prev_*` levels am ersten Tag einer neuen Periode sind `null`**
   (Caveat §4b-1) — MQL5-Indikator MUSS das gracefully handhaben
   (Linie weglassen, nicht crashen). Regression-Test:
   `tests/viz/test_bot_overlay_logic.py::test_all_prev_null_skips_all_prev_lines`
   und `::test_all_prev_null_no_draw_ops_satisfies_caveat_4b_1`.

4. **FVG-Rechteck-Farben in MQL5 sind voll opak** (`clrGreen` /
   `clrRed`) — MQL5 hat keine echte Alpha-Transparenz für
   `OBJ_RECTANGLE` ohne den `OBJPROP_BACK=true`-Hintergrund-Trick.
   Im Back-Modus füllt der Indikator die Rechtecke und schiebt sie
   hinter die Candles. Im Front-Modus würden sie Candles überdecken
   — User kann via Chart-Properties filtern (`Chart->Properties->
   Show->OHLC` deaktivieren hilft nicht, aber `Right-click->Properties
   ->Common->Charts in foreground` toggelt das Verhalten). Wir
   setzen `OBJPROP_BACK=true` als Default — wer das ändert, muss die
   FVG-Sichtbarkeit selbst prüfen.

5. **Timer alle 5 Sekunden** (Konstante `POLL_INT` in `BotOverlay.mq5`)
   ist ein Kompromiss zwischen Latency und CPU-Last. Bei hochfrequenten
   Feature-Updates (z.B. 1 Hz während NY-Session) kann die JSON-Datei
   schneller aktualisiert werden als der Indikator sie liest — visuell
   sieht man den letzten Stand, kein Real-Time-Sync. Für Real-Time-Sync
   wäre ZeroMQ-Push nötig (Plan §5.1.3, explizit als Alternative
   erwähnt). Block 8 / 9 können das ergänzen, wenn die Latency
   spürbar wird.

6. **Object-Prefix `bot_` muss im Cleanup gegriffen werden.** Der
   `OnDeinit`+`ClearAll`-Loop scannt `ObjectsTotal(0)` und löscht nur
   Objekte mit `bot_`-Prefix. Wenn ein anderer Indikator (oder eine
   User-Template) ein gleichnamiges Objekt ohne Prefix anlegt, wird es
   NICHT mit aufgeräumt — bewusst, damit Block-7-Objekte andere
   Indikatoren nicht stören. Wer das ändert, MUSS die Cleanup-Logik
   mit-tests (heute nicht testbar ohne MT5-Runtime).

7. **MQL5 stdlib-only.** Keine `<MQL5/Include>`-Header (z.B. kein
   `JsonParse`-Wrapper, kein `MqlRates`-Helper). Der Indikator
   enthält seine eigene Mini-JSON-Extraktion (substring-basiert). Das
   ist robust für das stabile Overlay-Schema, aber **nicht** generisch
   — wenn das Schema wächst (z.B. `volume_profile.daily`), MUSS der
   Indikator mit-aktualisiert werden. Eine Schema-Version-Header im
   JSON wäre eine zukünftige Verbesserung.

## 5. Live-Bug-Journal (Producer-Bugs gefunden, gefixt, regress-getestet)

Diese Bugs wurden im Block-1-Test-Coverage-Lauf ENTDECKT (vom Test-Worker,
nicht vom Producer) — sie waren in der Producer-Self-Test-Suite unsichtbar.
Falls jemand den Code "verbessert" und die Tests bricht: **diese Bugs
waren real und hätten den ersten Backtest zerschossen**.

### Live-Bug 2026-06-14-1: `on_tick` war no-op Generator
- **Datei:** `src/xauusd_bot/data/ohlc_builder.py` (pre-fix)
- **Symptom:** `on_tick` produzierte keine Bars, obwohl der Code
  "richtig" aussah.
- **Ursache:** Ein `return`-Statement vor dem `yield`-Marker stoppte den
  Generator-Body bevor er Code ausführte.
- **Fix:** Stray return entfernt. `on_tick` ist jetzt ein normaler
  Generator.
- **Regression-Test:** `tests/data/test_ohlc_builder.py::test_one_hundred_random_ticks_aggregate_into_m1_bars`
  (Pre-fix: FAIL. Post-fix: PASS.)

### Live-Bug 2026-06-14-2: `on_tick` doppelt-appendete closed M1
- **Datei:** `src/xauusd_bot/data/ohlc_builder.py` (pre-fix)
- **Symptom:** Bei jedem M1-Bar-Close wurde der Bar sowohl direkt in der
  `on_tick`-Schleife appended ALS AUCH über den `on_bar`-Cascade.
- **Ursache:** Zwei Owner für dieselbe Daten-Mutation. Klassischer
  "Pick one owner of the data write"-Anti-Pattern.
- **Fix:** Direkten Append in `on_tick` entfernt. `on_bar` ist Single
  Source of Truth.
- **Regression-Test:** `tests/data/test_ohlc_builder.py::test_zero_ticks_in_a_minute_produces_no_bar`
  + `test_one_tick_in_a_minute_ohlc_equals_tick`
  (Pre-fix: FAIL. Post-fix: PASS.)

## 6. macOS-venv-Quirk (informativ, nicht-blockierend)

Auf macOS mit Homebrew-Python 3.14 hat `site-packages/` einen inherited
`UF_HIDDEN`-Flag, der das Laden von editable-install `.pth`-Dateien
verhindert. Workarounds (in Reihenfolge der Bevorzugung):

1. `PYTHONPATH=src .venv/bin/python -m xauusd_bot.cli.replay_smoke`
2. `pip install --no-build-isolation -e ".[dev]"`
3. venv an einem Pfad neu erstellen, der den Flag nicht erbt
4. `pytest` ist NICHT betroffen weil `pyproject.toml` `pythonpath = ["src"]`
   setzt.

Im README unter "Quickstart" ist das dokumentiert.

## 7. Definition-of-Done je Block (gemappt auf die Roadmap)

Ein Block gilt als DONE, wenn:

1. Alle Producer-Self-Tests grün (`pytest -q`)
2. Test-Coverage-Worker hat zugelegt, kein Mock-Soup, ≥70% Coverage auf
   den neuen Modulen
3. Verifier hat unabhängig alle Pflicht-Checks ausgeführt und PASS
   gegeben
4. Adversarielle Probes (mind. 1) gefordert: Negative-Tests die garantiert
   fehlschlagen wenn das Subject buggy ist
5. End-to-End-Check: `docker compose config` valide, Replay-Smoke-Lauf
   exit 0 mit plausiblen Werten, Architektur-Invarianten gehalten
6. Commits auf `dev`, nicht main, nicht gepusht (kein Remote per
   Auftrag)
7. deliverable.md geschrieben, Verifier-Report referenziert
8. KEINE neuen Live-Bugs, oder vorhandene dokumentiert in §5 dieses Files

## 8. Bestätigte Defaults aus `00_FINAL_PLAN.md §11`

Stand 2026-06-16 — alle Defaults vom User abgesegnet:

1. **Wochen-Definition Volume Profile:** **ISO** (Mo 00:00 UTC).
2. **Kalender-API für News:** **TradingEconomics** + Stub-Fallback für
   Backtest/CI.
3. **Dashboard:** **ja** — als eigener **Block 9** (Custom Web-Dashboard
   mit eigenem Chart + Indikatoren-UI, webbasiert, zusätzlich zum
   MT5-`BotOverlay.mq5`).
4. **Historische Datenquelle:** **Dukascopy-Tick-Export** (externer
   XAUUSD-M1-Datensatz) + synthetischer Generator-Fallback für CI.

## 9. Kommunikations-Konventionen fürs Team

- Sprache der User-Facing-Strings: **Deutsch** (User schreibt Deutsch).
  Technische Tokens, Identifier, CLI-Befehle bleiben Englisch.
- Worker-Prompts dürfen Variablen/Platzhalter auf Deutsch enthalten.
- Architecture-Begriffe (Connector, Replay, VolumeRange, etc.) bleiben
  Englisch, da der Plan englisch ist und das die Suchbarkeit erhält.
- Commits auf `dev`, **nie** auf `main` vor expliziter User-Freigabe.
- Worker committen selbst, pushen NICHT (kein Remote).
- Verifier ist immer read-only — keine Code-Edits in Verifier-Sessions.
