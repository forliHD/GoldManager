# Agent 02 — Data Layer & MT5 Bridge

> Baut die Datenanbindung: Connector-Abstraktion, ReplayConnector + PaperBroker (Mac), LiveMT5Connector + Wine-Bridge (Ubuntu), und den Data Layer.

## Ownership
`src/xauusd_bot/connectors/`, `src/xauusd_bot/data/`, `docker/mt5-terminal/`

## Deliverables
1. **`IMarketConnector`** (Protocol): `get_rates`, `get_ticks`, `get_account`, `get_symbol_spec`, `order_send`, `positions_get`, `pending_get`, `order_modify`, `order_cancel`.
2. **`ReplayConnector`** — liest Parquet/CSV (M1 + optional Ticks), simuliert Bar-für-Bar-Vorlauf mit strikt monotoner Zeit. Liefert nur Daten bis `now_t`. **Keine MT5-Abhängigkeit.**
3. **`PaperBroker`** — simuliert Order-Fills: variabler Spread, Slippage-Verteilung je Volatilitätsregime, pessimistische Pending-Fills. Führt simuliertes Konto (Equity, Margin).
4. **`LiveMT5Connector`** — RPyC-Client zur Wine-Bridge. Reconnect-Logik, Health-Check.
5. **Wine-Bridge** (`docker/mt5-terminal/`) — Dockerfile auf Wine-Basis (z.B. `scottyhardy/docker-wine`) + MT5 + Windows-Python-RPyC-Server, der `MetaTrader5` importiert und sequenziell bedient. VNC nur intern. Autologin-Doku für Vantage-Demo.
6. **Data Layer:** `OHLCBuilder` (Ticks→M1/M5/H1), `SpreadMonitor` (Spread-Zeitreihe + Ausreißer-Flags), `DataQualityMonitor` (Lücken, Spikes, OHLC-Inkonsistenz, Spec-Drift), `SymbolSpecLoader`.
7. **Pre-Trade-Safety-Checks** als wiederverwendbares Modul (Feed online, Spread < Schwelle, kein Broker-Fehler, Konto stabil).

## Constraints
- MT5-API ist **nicht thread-safe** → genau ein Bridge-Prozess, alle Calls sequenziell.
- ReplayConnector und LiveMT5Connector müssen **identische** Schemas zurückgeben (gleiche DataFrames, gleiche `SymbolSpec`). Tests: gleicher Feature-Output bei gleichem Input.
- Tick-Volume nur als **relatives** Maß weitergeben (Perzentil/Z-Score), nie als absolutes Signal.

## Definition of Done
Replay-Pipeline füttert die Feature-Engine auf dem Mac end-to-end; PaperBroker führt simulierte Trades aus; LiveMT5Connector verbindet sich gegen einen Wine-MT5-Demo-Account auf Ubuntu und liefert dieselben Schemas.

## System Prompt (für MiniMax)
```
Du baust den Data Layer und die MT5-Anbindung für den XAUUSD-Bot. Implementiere IMarketConnector
als Protocol und ZWEI Implementierungen: ReplayConnector (liest historische Parquet/CSV, Bar-für-
Bar, kein MetaTrader5-Import, läuft auf macOS) und LiveMT5Connector (RPyC-Client zur Wine-Bridge,
nur Ubuntu). Baue dazu den PaperBroker (simuliert Fills mit variablem Spread + Slippage) und den
Data Layer (OHLCBuilder, SpreadMonitor, DataQualityMonitor, SymbolSpecLoader). Beide Connectoren
MÜSSEN identische Daten-Schemas liefern - schreibe einen Test, der das beweist. Importiere
MetaTrader5 ausschließlich im LiveMT5Connector und im Bridge-Server. MT5-Calls strikt sequenziell
(API nicht thread-safe). Tick-Volume nur relativ behandeln.
```
