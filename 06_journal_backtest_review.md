# Agent 06 — Journal, Backtest & Review

> Baut TradeJournalDB, FeatureSnapshotStore, den Event-Replay-Backtester, WalkForward und die Review-/Fitting-Engines.

## Ownership
`src/xauusd_bot/journal/`, `src/xauusd_bot/review/`

## Deliverables
1. **TradeJournalDB** (TimescaleDB) — pro Trade: alle numerischen Features, H1/M5/M1-Zonen, Triple-VWAP-Kontext, Y/M/W-Volume-Profile-Kontext, Session/News, Spread/Slippage/Fill-Details, Score + Teilscores, KI-Begründung, Entry-Grund, SL/TP, Exit-Grund, R-Ergebnis, Run-up/Drawdown, Pullback-Order-Verhalten, **LLM↔Fallback-Diskrepanz**.
2. **FeatureSnapshotStore** — `feature_snapshot` zu jedem Entscheidungszeitpunkt (nicht nur bei Trades), für Replay/Review.
3. **BacktestEngine** — **Event-Replay** über den ReplayConnector: speist denselben Feature-/Decision-/Execution-Code mit historischen Bars in chronologischer Reihenfolge. Strikte Point-in-Time-Korrektheit. Output: Equity-Kurve, Sharpe, Max-DD, Winrate, R-Verteilung pro Setup-Typ/Score-Band/Session.
4. **WalkForwardEngine** — rollierende In-/Out-of-Sample-Segmente (z.B. 12M In / 3M Out), Robustheitsmatrix.
5. **Daily/WeeklyReviewEngine** — aggregiert Journal+Snapshots, ruft den Review-Agent (`runtime_prompts/review_agent.md`) auf, erzeugt KPI-Reports.
6. **FittingProposalEngine** — sammelt Verbesserungs-Hypothesen. **Keine automatische Live-Änderung** — Vorschläge gehen in einen Backtest-Zyklus + manuelle Freigabe.

## Constraints
- **Backtest und Live teilen sich Feature- und Decision-Code.** Der Backtester unterscheidet sich von Live nur im Connector (Replay statt Live) und Broker (Paper statt MT5). Keine Backtest-Sonderlogik in den Features.
- Spread/Slippage realistisch modellieren (variabel, News-abhängig); ehrlich kennzeichnen, dass Ausführungsrealismus die größte Unsicherheit ist (Plan §6.2).
- Review-Agent darf Hypothesen liefern; Aktivierung nur nach Backtest + explizitem Human-OK.

## Definition of Done
Ein historischer Zeitraum läuft als Backtest durch und liefert eine Equity-Kurve + KPIs; derselbe Code erzeugt im Live-Mode identische Features; WeeklyReview produziert einen lesbaren Report mit nummerierten, testbaren Vorschlägen.

## System Prompt (für MiniMax)
```
Du baust Journal, Backtest und Review des XAUUSD-Bots. TradeJournalDB + FeatureSnapshotStore auf
TimescaleDB (alle Features + Trade-Ergebnisse + LLM-vs-Fallback-Diskrepanz persistieren).
BacktestEngine als EVENT-REPLAY: speise denselben Feature-/Decision-/Execution-Code über den
ReplayConnector mit historischen Bars chronologisch, strikt point-in-time, KEINE Backtest-
Sonderlogik in den Features - der einzige Unterschied zu Live ist Connector+Broker. Output: Equity,
Sharpe, Max-DD, Winrate, R-Verteilung pro Setup/Score-Band/Session. WalkForwardEngine mit
rollierenden In/Out-of-Sample-Segmenten. Daily/WeeklyReviewEngine ruft den Review-Agent
(runtime_prompts/review_agent.md) auf. FittingProposalEngine liefert nur Vorschläge - NIEMALS
automatische Live-Regeländerung, immer erst Backtest + manuelle Freigabe. Modelliere Spread/
Slippage realistisch und kennzeichne Ausführungsrealismus ehrlich als größte Unsicherheit.
```
