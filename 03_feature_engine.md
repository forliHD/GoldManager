# Agent 03 — Feature Engine

> Baut alle Feature-Module. Enthält die **korrigierte** Higher-Timeframe Volume Range Engine (Joshuas Hauptpunkt).

## Ownership
`src/xauusd_bot/features/` (session, vwap, volume_range, fvg, structure, momentum, liquidity, news), `src/xauusd_bot/viz/`

## Deliverables (in dieser Reihenfolge)
1. **SessionEngine** — Asia/London/NY (UTC), Session-High/Low/Open, Sweeps, Equal Highs/Lows, Session-Risikofaktor.
2. **TripleVWAPEngine** — anchored VWAP ab 00:00/07:00/12:00 UTC, M1 + Tick-Volume-Gewichtung, Vortags-VWAP weiterführen bis Anker erreicht. Features: Abstand (Punkte/ATR/Perzentil), Cross/Reclaim/Loss, Cluster.
3. **FixedVolumeRangeEngine** — **siehe Detailspec unten**.
4. **FVGEngine** — H1/M5/(M1) Fair Value Gaps: Typ, Größe, Alter, Mitigation-Status, Entstehungsimpuls, Ranking, Clustering mit VP/VWAP-Leveln.
5. **MarketStructureEngine** — Swing H/L (Fraktal), BOS/CHOCH close-basiert mit Mindest-Distanz/Zeit-Filter, intern vs extern, Liquidity Pools, Sweep vs Breakout.
6. **CandleMomentumEngine** — Body/ATR, Wick/Body, Close-Position, Displacement, impulsive Folge, Vol-Anstieg, Tick-Vol relativ. Momentum-Score je TF. **Keine Pattern-Namen.**
7. **LiquidityEngine** — Liquidity-Zonen, TP-Targets, SL-Schutzbereiche.
8. **NewsContextEngine** — Kalender-API (config-driven), NewsEvent-Objekte, Countdown, Impact-Flags, Surprise-Score, Blackout-Regeln.
9. **Overlay-Writer** (`viz/`) — schreibt `overlay_levels.json` ins MT5-Files-Verzeichnis für `BotOverlay.mq5` (siehe Plan §5).

## FixedVolumeRangeEngine — Detailspec (KRITISCH, korrigiert)
Periodendefinition mit **festen Kalendergrenzen** (UTC), nicht rollierend:
- **Yearly:** `[01.01. 00:00, 01.01. Folgejahr 00:00)`
- **Monthly:** `[1. 00:00, 1. Folgemonat 00:00)`
- **Weekly:** `[Mo 00:00, Mo Folgewoche 00:00)` — konfigurierbar (ISO vs Broker-Woche)

Zustand pro Profil:
- **`locked`** (`period_end < now`): VAH/VPOC/VAL final, unveränderlich, in DB persistiert. = „Previous Year/Month/Week Levels".
- **`developing`** (`period_start <= now < period_end`): akkumuliert Volumen jeder neu geschlossenen M1-Kerze, VAH/VPOC/VAL wandern. (Joshuas „baut sich Woche für Woche auf".)
- **Rollover:** laufendes Profil einfrieren+persistieren → neues developing-Profil starten.

Volumenverteilung innerhalb M1: `tick_based > ohlc_weighted > uniform_hl > close_only` (konfigurierbar). Bins: Weekly 0,5–1,0 / Monthly 1,0–2,0 / Yearly 2,0–5,0 Goldpunkte. Value Area 70 % (Test 68/75 %).

Features: Status (below/within/above value), Abstände zu VAH/VPOC/VAL (Punkte+ATR), Acceptance/Rejection, Rotation vs Breakout, Clustering developing↔locked.

**Point-in-Time-Pflicht:** Zum Zeitpunkt `t` nur Bars mit `close_time <= t`. Derselbe Code läuft in Live und Backtest. Schreibe einen Test, der Look-ahead-Freiheit beweist (Profil bei `t` ändert sich nicht, wenn man spätere Bars hinzufügt).

## Constraints
- Jedes Modul: klar definierte Inputs/Outputs als Pydantic-Schema, deterministisch, unit-getestet mit fixierten Mini-Datensätzen.
- Module sind **stateless bzgl. Zukunft**: nur vergangene/aktuelle Bars.

## System Prompt (für MiniMax)
```
Du baust die Feature-Engine des XAUUSD-Bots. Implementiere die Module in der Reihenfolge: Session,
TripleVWAP, FixedVolumeRange, FVG, MarketStructure, CandleMomentum, Liquidity, News, plus Overlay-
Writer. KRITISCH bei FixedVolumeRange: feste KALENDER-Perioden (Yearly/Monthly/Weekly), nicht
rollierend. Jedes Profil ist 'locked' (abgeschlossen, final, persistiert) oder 'developing' (laufend,
akkumuliert pro neu geschlossener M1-Kerze, Levels wandern). Bei Periodenwechsel: einfrieren +
neues developing starten. STRIKTE Point-in-Time-Korrektheit: zum Zeitpunkt t nur Bars mit
close_time <= t, kein Look-ahead - schreibe einen Test, der das beweist. Derselbe Feature-Code
läuft in Live und Backtest. Tick-Volume nur relativ. Keine Candlestick-Pattern-Namen, nur
quantitative Kennzahlen. Jedes Modul mit Pydantic-I/O und Unit-Tests.
```
