# Agent 05 — Execution & Risk

> Baut die „Hands": RiskManager, PositionSizer, OrderManager, Pending/Stop/TP/EmergencyStop. Alles deterministisch — hier wird Geld bewegt.

## Ownership
`src/xauusd_bot/execution/`

## Deliverables
1. **RiskManager** — Risk/Trade nach Score-Band: A+ (≥85) 2 % / Good (75–84) 1 % / Scout (65–74) 0,5 %. Limits: Tagesverlust 3–4 %, Wochenverlust 6–8 %, max. Trades/Session, max. parallele Exposure (2–3), keine gegensätzlichen Positionen ohne explizite Hedge-Strategie. Bei Limit erreicht → Block + Pause.
2. **PositionSizer** — Lotgröße aus Kontostand, erlaubtem Risiko, SL-Distanz, ContractSize. Deterministisch, getestet gegen Referenzwerte.
3. **OrderManager** — Market/Limit/Stop-Orders via `IMarketConnector.order_send`. Idempotenz, Order-Tagging (Setup-ID), Fill-Tracking.
4. **PendingOrderManager** — laufende Prüfung offener Pendings gegen neue Struktur/VWAP/Value/News; löscht Orders, die nicht mehr zur Marktstruktur passen.
5. **StopManager** — Initial-SL hinter Struktur + ATR-Puffer; nach TP1 Break-Even/enger; Trailing hinter neuer M5-BOS.
6. **TakeProfitManager** — TP1 erste Liquidität/1R, TP2 M5/H1-Ziele, TP3/Runner Richtung Weekly/Monthly/Yearly VAH/VPOC/VAL; Runner-Verhalten an HTF-Level-Akzeptanz/-Rejection.
7. **EmergencyStopManager** — bei System-/Brokerfehler oder extremer Volatilität/Slippage: sofort flatten + Bot-Pause. **Höchste Priorität.**

## Constraints
- **Alles deterministisch.** Kein LLM-Call hier drin. Inputs sind die qualifizierte Entscheidung + Symbol-Spec + Kontostand.
- Gesamtrisiko aller Teil-Entries ≤ Setup-Risikogrenze.
- Jede Order durchläuft die Pre-Trade-Safety-Checks (aus Agent 02) **bevor** sie rausgeht.
- Im Replay-Mode gehen alle Orders an den PaperBroker; identische Code-Pfade wie live.
- Vollständiges Logging jeder Order-Entscheidung (für Journal + Slippage-Analyse: Orderpreis vs Fillpreis).

## Definition of Done
Komplette Trade-Lifecycle (Entry → Teil-TP → Trailing → Exit/Invalidation) läuft im Replay gegen PaperBroker durch; EmergencyStop flattet zuverlässig; Risiko-Limits greifen nachweisbar (Test: simulierte Verlustserie löst Tages-/Wochen-Pause aus).

## System Prompt (für MiniMax)
```
Du baust den Execution- und Risk-Layer des XAUUSD-Bots - die deterministischen 'Hands'. KEIN LLM-
Call in diesem Layer. Implementiere RiskManager (Risk/Trade nach Score-Band, Tages-/Wochen-Limits,
Exposure-Grenzen, Pause bei Limit), PositionSizer (deterministisch, getestet), OrderManager,
PendingOrderManager (löscht obsolete Orders bei neuer Struktur/News), StopManager (BE nach TP1,
Trailing hinter M5-BOS), TakeProfitManager (TP1/TP2/Runner zu HTF-Volume-Leveln) und
EmergencyStopManager (sofort flatten + pause, höchste Priorität). Jede Order durchläuft die Pre-
Trade-Safety-Checks bevor sie rausgeht. Gesamtrisiko aller Teil-Entries <= Setup-Limit. Im Replay
gehen Orders an den PaperBroker über identische Code-Pfade wie live. Logge Orderpreis vs Fillpreis
für Slippage-Analyse. Schreibe einen Test, der beweist, dass eine simulierte Verlustserie die
Tages-/Wochen-Pause auslöst.
```
