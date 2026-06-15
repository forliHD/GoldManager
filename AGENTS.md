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
| 4 | Execution + Risk + Pending/Stop/TP + EmergencyStop | offen |
| 5 | Journal (TimescaleDB) + BacktestEngine + WalkForward + Review | offen |
| 6 | AIDecisionLayer (OpenRouter) parallel zu RuleBasedFallback | offen |
| 7 | MT5-Viz-Bridge + `BotOverlay.mq5` | offen |
| 8 | LiveMT5Connector (RPyC) + mt5-terminal-Container (Wine) | offen |
| 9 | Demo-Forward auf Ubuntu → Monitoring → (erst dann) Live | offen |

Producer-Commits landen auf `dev`. Kein Remote konfiguriert (per Auftrag).

**E2E-Integration (Stand 2026-06-15):** Replay-Connector → Feature-Engine →
Decision-Layer Pipeline-Smoke grün (`decision_smoke --n-bars 200 --start-bar
2000` → 9/200 qualified trades, exit 0). Alle Architektur-Invarianten I-1..I-5
re-verifiziert. Gesamte Test-Suite: **463 passed** (Block 1: 217, Block 2: 70
neu, Block 3: 85).

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

## 8. Bekannte offene Punkte aus `00_FINAL_PLAN.md §11`

Für Block 2 + folgende brauche ich vom User Entscheidungen (oder Defaults
sind akzeptiert):

1. **Wochen-Definition Volume Profile:** ISO (Mo 00:00 UTC) oder
   Broker-Woche (So 22:00–Fr)? **Default: ISO.**
2. **Kalender-API für News:** TradingEconomics / FXStreet / Broker-Kalender?
   **Default: TradingEconomics mit Stub-Fallback für Backtest.**
3. **Dashboard im Block-2-Wurf:** ja/nein? **Default: ja, minimaler
   FastAPI + Status-Endpoint.**
4. **Historische Datenquelle für Replay/Backtest:** Vantage-Tick-Export
   oder externer XAUUSD-M1-Datensatz? **Default: externer Datensatz
   (z.B. Dukascopy) + synthetischer Generator-Fallback für CI.**

## 9. Kommunikations-Konventionen fürs Team

- Sprache der User-Facing-Strings: **Deutsch** (User schreibt Deutsch).
  Technische Tokens, Identifier, CLI-Befehle bleiben Englisch.
- Worker-Prompts dürfen Variablen/Platzhalter auf Deutsch enthalten.
- Architecture-Begriffe (Connector, Replay, VolumeRange, etc.) bleiben
  Englisch, da der Plan englisch ist und das die Suchbarkeit erhält.
- Commits auf `dev`, **nie** auf `main` vor expliziter User-Freigabe.
- Worker committen selbst, pushen NICHT (kein Remote).
- Verifier ist immer read-only — keine Code-Edits in Verifier-Sessions.
