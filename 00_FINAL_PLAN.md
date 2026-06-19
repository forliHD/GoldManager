# XAUUSD Bot — Finaler Umsetzungsplan (v1.0)

**Stack:** Vantage-MT5 → Python Feature-Engine → AI-Decision-Layer (OpenRouter/MiniMax BYOK) → Risk/Execution → Journal/Review
**Build-Tool:** MiniMax Code (Subagents siehe `/agents`)
**Deployment:** Full Docker. Dev lokal auf MacBook (Replay-Connector, kein MT5 nötig) → Prod auf Ubuntu-VM (MT5 unter Wine + Bridge).

---

## 0. Vorbemerkung (kurz, aber wichtig)

Ich bin kein Finanzberater, und das hier ist keine Anlageempfehlung. Der Plan ist eine **Software-Architektur**. Zwei ehrliche Realitäts-Checks, die ich in den Plan eingebaut habe statt sie wegzulächeln:

- Die Renditeerwartungen aus der **Executive Summary** (5–10 %/Woche) sind unrealistisch und als Planungsgrundlage gefährlich. Die `deep_research.md` ist mit „2–5 %/Monat, ambitioniert sind 10 %/Woche und meist unrealistisch" deutlich nüchterner — wir planen gegen die nüchterne Variante.
- **Joshuas Skepsis beim Backtesting ist berechtigt — aber nur halb.** Die Feature-Berechnung (VWAP, Volume Profile, Struktur) ist deterministisch und sauber replaybar (Abschnitt 6). Der wirklich schwer zu backtestende Teil ist die *Ausführungsrealität* (Spread, Slippage, News-Latenz). Genau dafür ist die Demo-Forward-Phase da, nicht als Ersatz fürs Backtesting, sondern als Ergänzung.

Erstes echtes Geld erst nach: deterministischem Backtest + Walk-Forward + mind. mehreren Wochen stabilem Demo-Forward. Der Plan erzwingt diese Reihenfolge.

---

## 1. Was wurde geändert und warum (Changelog ggü. `deep_research.md`)

Du wolltest, dass bei jedem Part dabeisteht, was nicht passt und überarbeitet wurde. Hier die Liste, sortiert nach Joshuas Punkten + meinen technischen Korrekturen.

### Δ1 — Volume Range Engine: feste Kalender-Perioden statt rollierendem „bis jetzt" *(Joshua, Hauptpunkt)*
**Im Doc stand (Section 5.3):** „Yearly Profile (Custom-Range z.B. 01.01.2025–jetzt)".
**Problem:** Das liest sich als *ein* rollierendes Profil von 1.1.2025 bis heute — Mitte 2026 wären das 1,5 Jahre in einem Topf.
**Joshuas Modell (korrekt):** Jedes Profil ist an **feste Kalendergrenzen** gebunden. Das *abgeschlossene* Jahr 2025 (01.01.–31.12.2025) ist eingefroren. Das *laufende* Jahr 2026 ist ein **„developing" Profil**, das sich Bar für Bar aufbaut. Genauso Monat (1.–letzter Tag, baut sich über die Wochen auf, Reset am Monatswechsel) und Woche.
**Fix:** Volume Range Engine komplett umgebaut auf `locked` vs `developing` Perioden mit Kalendergrenzen + „Previous Period Levels" für alle drei Ebenen (auch Yearly, das hatte das Doc nur für Month/Week). Vollspezifikation in **Abschnitt 4**.

### Δ2 — Sichtbarkeit im MetaTrader *(Joshua: „können wir das im Metatrader sichtbar anzeigen lassen, oder?")*
**Antwort: Ja.** Aber nicht über das `MetaTrader5`-Python-Paket — das kann nur lesen/ordern, keine Chart-Objekte zeichnen. Lösung: ein begleitender **MQL5-Indikator** (`BotOverlay.mq5`), der die von Python berechneten Levels aus einer Datei/Socket liest und als horizontale Linien (VAH/VPOC/VAL, VWAPs) + Rechtecke (FVG-Zonen, Value Areas) zeichnet. Doppelnutzen: Das ist gleichzeitig unser **QA-Werkzeug** zum Vergleich mit TradingView. Spezifikation in **Abschnitt 5**.

### Δ3 — VWAP-Vergleichbarkeit mit TradingView *(Joshua: „bin gespannt ob die vergleichbar sind")*
**Ehrliche Antwort: Sie werden nicht exakt matchen, und das ist okay.** TradingView nutzt seine eigene Datenquelle und teils echtes Volumen; MT5/Vantage liefert nur Tick-Volume. Erwartung: gleiche *Form* und *Levels* mit kleinem Offset, hohe Korrelation (Tick-Volume korreliert laut Doc mit 0,79–0,98 zu echtem Volumen). Validierungs-Workflow (abgeschlossene Woche berechnen → in MT5 zeichnen → gegen TV Fixed-Range-VP halten) in **Abschnitt 6.3**. Große Divergenz = Bug, kleiner Offset = normal.

### Δ4 — Backtesting-Realismus *(Joshua: „wage ich zu bezweifeln, eher Livetests")*
**Fix:** Klare Trennung „was ist deterministisch backtestbar / was nicht" + Event-Replay-Backtester mit strikter Point-in-Time-Korrektheit (kein Look-ahead). Details in **Abschnitt 6**.

### Δ5 — MT5-Realität: Wine, Bridge, Mac-Dev *(mein technischer Hauptpunkt)*
**Im Doc stand:** „läuft auf einem VPS mit MT5-Terminal" — aber das `MetaTrader5`-Paket ist **Windows-only**. Auf Mac/Linux/Docker braucht es Wine + eine Bridge.
**Fix:** Connector-Abstraktion mit zwei Implementierungen (Live-Bridge vs Replay/CSV), sodass du 90 % auf dem Mac ohne MT5 entwickelst. Full-Docker-Topologie + Mac→Ubuntu-Pfad in **Abschnitt 3**.

### Δ6 — AI-Decision-Layer auf OpenRouter/MiniMax BYOK *(deine Vorgabe)*
**Fix:** AIDecisionLayer als OpenAI-kompatibler OpenRouter-Client, modell-config-driven (Default MiniMax via BYOK), strikte JSON-Validierung, RuleBasedFallback bleibt sicherheitsautoritativ. Details in **Abschnitt 7**.

### Δ7 — Renditeerwartung entschärft
**Fix:** Executive-Summary-Zahlen (5–10 %/Woche) verworfen. Planungsbasis = konservativ, Risiko-Limits aus `deep_research.md` (Tagesverlust 3–4 %, Wochenverlust 6–8 %).

---

## 2. Zielarchitektur (Full Docker)

Service-Topologie. Jeder Service = ein Container. Kommunikation über Redis Streams (Topics: `market_ticks`, `features`, `decisions`, `orders`, `journal`).

```
                    ┌─────────────────────────────────────────────┐
                    │  ubuntu-vm (prod) / macbook (dev)            │
                    │                                              │
  ┌──────────────┐  │  ┌────────────────┐   ┌──────────────────┐  │
  │ MT5 + Wine   │◄─┼──┤ mt5-bridge     │   │ redis (streams)  │  │
  │ (nur PROD)   │  │  │ (RPyC/socket)  │   └────────┬─────────┘  │
  │ + BotOverlay │  │  └───────┬────────┘            │            │
  │   .mq5       │  │          │                     │            │
  └──────────────┘  │  ┌───────▼────────┐   ┌────────▼─────────┐  │
                    │  │ data-collector │──►│ feature-engine   │  │
                    │  │ (live|replay)  │   │ (alle Engines)   │  │
                    │  └────────────────┘   └────────┬─────────┘  │
                    │                                │            │
                    │  ┌────────────────┐   ┌────────▼─────────┐  │
   OpenRouter ◄─────┼──┤ decision-engine│◄──┤ feature-aggreg.  │  │
   (MiniMax BYOK)   │  │ scoring + AI   │   │ + scoring        │  │
                    │  └───────┬────────┘   └──────────────────┘  │
                    │          │                                  │
                    │  ┌───────▼────────┐   ┌──────────────────┐  │
                    │  │ execution-eng. │──►│ timescaledb      │  │
                    │  │ risk+order      │   │ (journal+ohlc+   │  │
                    │  └────────────────┘   │  snapshots)      │  │
                    │  ┌────────────────┐   └──────────────────┘  │
                    │  │ review/backtest│                         │
                    │  │ (on-demand)    │   ┌──────────────────┐  │
                    │  └────────────────┘   │ dashboard (opt.) │  │
                    │                       │ FastAPI + UI     │  │
                    └───────────────────────┴──────────────────┘
```

**Container-Liste:**

| Container | Inhalt | Dev (Mac) | Prod (Ubuntu) |
|---|---|---|---|
| `mt5-terminal` | Wine + MT5 + Windows-Python-Bridge (RPyC) | ❌ aus | ✅ an |
| `data-collector` | TickCollector, OHLCBuilder, SpreadMonitor, DataQualityMonitor | ✅ Replay-Mode | ✅ Live-Mode |
| `feature-engine` | Session, TripleVWAP, FixedVolumeRange, FVG, Structure, Candle/Momentum, Liquidity, News | ✅ | ✅ |
| `decision-engine` | FeatureAggregator, ScoringEngine, AIDecisionLayer, RuleBasedFallback, TradeQualification | ✅ | ✅ |
| `execution-engine` | RiskManager, PositionSizer, OrderManager, Pending/Stop/TP/EmergencyStop | ✅ (paper sim) | ✅ (demo→live) |
| `review-backtest` | Backtest, WalkForward, Daily/WeeklyReview, FittingProposal | ✅ | on-demand |
| `redis` | Message Queue / Streams | ✅ | ✅ |
| `timescaledb` | Journal, FeatureSnapshots, OHLC-Cache | ✅ | ✅ |
| `dashboard` | FastAPI + leichtes Frontend (optional) | ✅ | ✅ |

Steuerung über zwei Compose-Files (siehe Abschnitt 10): `docker-compose.dev.yml` (ohne `mt5-terminal`) und `docker-compose.prod.yml` (mit).

---

## 3. Die MT5-Realität: Wine, Bridge & der Mac→Ubuntu-Pfad

Das ist der Teil, an dem Projekte wie dieses normalerweise hängenbleiben, deshalb explizit:

### 3.1 Warum nicht einfach `pip install MetaTrader5` auf dem Mac?
Das Paket spricht über Windows-IPC mit dem MT5-Terminal und läuft **nur auf Windows**. Auf Mac (Apple Silicon) und Linux brauchst du MT5 unter Wine. Auf Apple Silicon ist Wine+MT5 zusätzlich x86-Emulation — machbar, aber zäh. **Deshalb entwickeln wir auf dem Mac bewusst ohne MT5.**

### 3.2 Connector-Abstraktion (der Schlüssel)
```python
class IMarketConnector(Protocol):
    def get_rates(self, symbol, timeframe, count) -> pd.DataFrame: ...
    def get_ticks(self, symbol, from_ts, to_ts) -> pd.DataFrame: ...
    def get_account(self) -> AccountInfo: ...
    def get_symbol_spec(self, symbol) -> SymbolSpec: ...
    def order_send(self, request) -> OrderResult: ...
    def positions_get(self) -> list[Position]: ...
```
Zwei Implementierungen:
- **`ReplayConnector`** — liest historische M1/Tick-Daten aus Parquet/CSV, simuliert Bar-für-Bar Vorlauf (auch für Dev + Backtest). **Keine MT5-Abhängigkeit.** → Mac.
- **`LiveMT5Connector`** — spricht über die Bridge mit dem echten MT5-Terminal. → Ubuntu/Wine.

`order_send` im ReplayConnector geht an einen **PaperBroker** (simuliert Fills, Spread, Slippage). Damit ist auf dem Mac die *komplette* Pipeline lauffähig außer dem echten Broker.

### 3.3 Die Bridge (Prod)
Bewährtes Muster (du hast es bei GoldBot schon gemacht): Im `mt5-terminal`-Container laufen Wine + MT5 + ein **Windows-Python-RPyC-Server**, der `MetaTrader5` importiert und die API über einen Netzwerk-Port exponiert. Der `LiveMT5Connector` (Linux-nativ) ist der RPyC-Client. Wichtig: **MT5-API ist nicht thread-safe** → alle Calls laufen sequenziell durch *einen* Bridge-Prozess.
Basis-Image-Optionen: auf `scottyhardy/docker-wine` aufbauen oder ein fertiges `metatrader5-vnc`-Image (VNC zum GUI-Login beim Vantage-Account) als Vorlage. VNC nur intern exponieren, nie öffentlich.

### 3.4 Migrationspfad
1. **Mac:** Alles außer `mt5-terminal` per `docker-compose.dev.yml`. Connector = Replay/Paper. Du baust + testest Features, Scoring, Execution-Logik, Journal, Backtest komplett durch.
2. **Ubuntu-VM:** `mt5-terminal`-Container dazu, VNC-Login zum Vantage-**Demo**-Account, Connector-Flag auf `live`. Erst Demo-Forward.
3. **Live:** Erst nach grünem Demo-Forward, mit minimalem Volumen, EmergencyStop scharf.

---

## 4. Korrigierte Higher-Timeframe Volume Range Engine *(Δ1)*

### 4.1 Periodendefinition (feste Kalendergrenzen, UTC)
| Profil | Periode | Beispiel |
|---|---|---|
| Yearly | `[01.01. 00:00, 01.01. nächstes Jahr 00:00)` | 2025: 01.01.2025–31.12.2025 (locked), 2026: developing |
| Monthly | `[1. des Monats 00:00, 1. nächster Monat 00:00)` | Januar locked, aktueller Monat developing |
| Weekly | `[Montag 00:00, nächster Montag 00:00)` (konfigurierbar; Broker-Woche So 22:00–Fr alternativ) | Vorwoche locked, aktuelle Woche developing |

### 4.2 Zustandsautomat pro Profil
- **`locked`** (abgeschlossen): `period_end < now`. VAH/VPOC/VAL sind **final**, ändern sich nie mehr, werden in `timescaledb` persistiert. → Das sind die „Previous Year/Month/Week Levels".
- **`developing`** (laufend): `period_start <= now < period_end`. Profil akkumuliert das Volumen jeder **neu geschlossenen** M1-Kerze. VAH/VPOC/VAL werden inkrementell neu berechnet → **sie wandern, während die Periode sich füllt**. Genau das ist Joshuas „baut sich Woche für Woche auf".
- **Rollover:** Bei Periodenwechsel → laufendes Profil einfrieren (`locked` + persistieren) → neues `developing`-Profil für die neue Periode starten.

> **Joshuas Modell ist damit 1:1 abgebildet:** 2025 = eingefroren, 2026 = baut sich auf; Monat baut sich über ~4 Wochen auf und resettet am Monatswechsel.

### 4.3 Volumenverteilung innerhalb M1-Kerzen (konfigurierbar)
Reihenfolge der Präferenz: `tick_based` (wenn Tickdaten da) > `ohlc_weighted` > `uniform_hl` > `close_only`. Für den ersten produktiven Bot: `uniform_hl` oder `close_only`, in Backtests gegen `tick_based` validieren.

### 4.4 Bin-Größen (Startwerte) & Value Area
- Weekly: 0,5–1,0 Goldpunkte/Bin · Monthly: 1,0–2,0 · Yearly: 2,0–5,0
- Value Area: 70 % Standard; 68 % und 75 % als Backtest-Varianten.

### 4.5 Features pro Profil (unverändert sinnvoll, aus Doc übernommen)
- Status `below_value | within_value | above_value`
- Abstände zu VAH/VPOC/VAL (Punkte + ATR)
- Acceptance vs Rejection (Verweildauer, Anzahl Closes jenseits Level)
- Value Rotation vs Value Breakout
- **Neu:** Clustering der developing- mit den locked-Leveln (z.B. aktueller WVPOC trifft Previous-Month-VAL = starke Konfluenz)

### 4.6 Point-in-Time-Garantie (für Backtest, Δ4)
Die Engine darf zum Zeitpunkt `t` ausschließlich Bars mit `close_time <= t` verwenden. Im Replay heißt das: developing-Profil enthält nur Bars bis `t` innerhalb der aktuellen Kalenderperiode; locked-Profile sind aus ihren vollständigen historischen Perioden vorab berechnet. → **Kein Look-ahead**, deterministisch reproduzierbar.

---

## 5. MT5-Visualisierungs-Bridge *(Δ2)*

**Ziel:** Levels, die Python berechnet, im MT5-Chart sehen — als Anzeige *und* als QA gegen TradingView.

### 5.1 Mechanik
1. `feature-engine` schreibt bei jedem neuen Bar eine `overlay_levels.json` in das MT5-Files-Sandbox-Verzeichnis (`MQL5/Files/`), z.B.:
```json
{
  "ts": "2026-06-14T13:00:00Z",
  "vwap": {"utc00": 2370.4, "utc07": 2374.8, "utc12": 2378.2},
  "volume_profile": {
    "weekly":  {"vah": 2368, "vpoc": 2358, "val": 2346, "state": "developing"},
    "monthly": {"vah": 2390, "vpoc": 2372, "val": 2355, "state": "developing"},
    "yearly":  {"vah": 2450, "vpoc": 2380, "val": 2320, "state": "developing"},
    "prev_week": {"vah": 2360, "vpoc": 2351, "val": 2340}
  },
  "fvg_zones": [{"tf":"H1","type":"bullish","top":2373.0,"bottom":2371.5}]
}
```
2. Der MQL5-Indikator **`BotOverlay.mq5`** liest die Datei auf einem Timer (z.B. alle 5 s) und zeichnet:
   - Horizontale Linien für VWAPs (drei Farben) und VAH/VPOC/VAL je Profil (Linienstil: developing gestrichelt, locked durchgezogen).
   - Rechtecke (`OBJ_RECTANGLE`) für FVG-Zonen und Value Areas.
   - Labels mit Level-Namen.
3. Alternativ statt Datei: ZeroMQ-Push (geringere Latenz). Datei reicht für den Anfang und ist robuster.

### 5.2 Warum Datei und nicht Python-zeichnet-direkt
Das `MetaTrader5`-Paket hat keine `ObjectCreate`-Funktion — Chart-Objekte gehen nur über MQL5. Die Datei-Bridge entkoppelt sauber: Python rechnet, MQL5 zeichnet.

---

## 6. Backtesting-Realität *(Δ3 + Δ4)*

### 6.1 Was deterministisch backtestbar ist (Joshua hier zu pessimistisch)
Alles, was sich aus historischen M1+Tick-Volume rekonstruieren lässt, ist **sauber replaybar**, wenn man Bar-für-Bar vorgeht: Triple-VWAP (anchored), Volume Profile (locked + developing per Point-in-Time), Market Structure (BOS/CHOCH), FVG-Erkennung, Sessions, Momentum/Candle-Scores, Scoring. → Wir haben einen *echten* Event-Replay-Backtest, nicht nur Livetests.

### 6.2 Was NICHT sauber backtestbar ist (Joshua hier zu Recht skeptisch)
- **Intra-M1-Tick-Verteilung** → beeinflusst die exakte VP-Form leicht; wir approximieren und vergleichen Verteilungsvarianten.
- **Spread & Slippage** → variabel, News-abhängig. Konservativ modellieren (variabler Spread + Slippage-Verteilung je Volatilitätsregime).
- **News-Latenz & Requotes** → nur näherungsweise. Daher die News-Blackout-Regeln als harter Filter.
- **Order-Fill-Reihenfolge bei Pending Orders** → im PaperBroker konservativ (pessimistischer Fill).

→ **Konsequenz:** Backtest validiert die *Logik* (Features, Scoring, Struktur). Demo-Forward validiert die *Ausführung* (Spread, Slippage, Latenz, Broker-Verhalten). Beides ist nötig, keins ersetzt das andere. Genau so ist die Roadmap gebaut.

### 6.3 TradingView-Validierungs-Workflow (für VWAP & VP)
1. Abgeschlossene Periode wählen (z.B. letzte Woche, jetzt `locked`).
2. WVAH/WVPOC/WVAL + anchored VWAP aus Vantage-M1 berechnen.
3. Per `BotOverlay.mq5` im MT5-Chart zeichnen.
4. Daneben in TradingView Fixed-Range-Volume-Profile + Anchored VWAP über denselben Zeitraum legen.
5. **Bewerten:** kleiner Offset (andere Datenquelle/Volumen) = normal. Große Abweichung der Level-Positionen = Bug → Verteilungsmethode/Bin-Größe prüfen.

### 6.4 Walk-Forward
Rollierend: z.B. 12 Monate In-Sample (Scoring-Gewichte + Filter kalibrieren) → 3 Monate Out-of-Sample → verschieben. Ziel: Regelset, das über mehrere Marktregime robust ist, nicht auf ein Zeitfenster overfittet.

---

## 7. AI-Decision-Layer über OpenRouter / MiniMax BYOK *(Δ6)*

### 7.1 Client
- OpenAI-kompatibel gegen `https://openrouter.ai/api/v1/chat/completions`.
- `OPENROUTER_API_KEY` aus Env (BYOK), `OPENROUTER_MODEL` aus Env (Default: aktuelles MiniMax-Modell auf OpenRouter — exakten Modell-String beim Anlegen prüfen, da sich Verfügbarkeit ändert).
- Optionale Header `HTTP-Referer` / `X-Title`. **ZDR-Routing** aktivieren (du nutzt das schon), damit keine Prompt-Daten geloggt werden.

### 7.2 Harte Sicherheitsregeln (Brain vs Hands)
- Das LLM operiert **ausschließlich** auf vorberechneten Features. Es berechnet **niemals** Positionsgröße, SL- oder TP-Level selbst — das macht deterministisch die Risk/Execution-Engine.
- Output **strikt JSON**, mit Pydantic validiert. Ungültiges JSON → 1 Retry → bei erneutem Fehler `no_trade`.
- **`RuleBasedFallback` ist sicherheitsautoritativ:** Ist das LLM nicht erreichbar, zu langsam (Timeout) oder widerspricht es harten Regeln (z.B. Entry trotz News-Blackout), gewinnt die Regel — im Zweifel `no_trade`.
- LLM wird **nicht bei jedem Bar** aufgerufen, sondern erst ab Score-Schwelle (z.B. ≥65 „prepare"). Spart Kosten + Latenz.

### 7.3 Output-Schema (Pydantic)
Siehe `runtime_prompts/decision_agent.md` für den vollen Prompt + Schema. Kurzform: `decision`, `entry_type`, `entry_side`, `entry_zone{min,max}`, `invalidations[]`, `management{tp1_rr,tp2_rr,runner_to,protect_before_news_min}`, `comment`.

---

## 8. Übrige Module (Deltas/Bestätigungen — Rest gilt wie `deep_research.md`)

Die folgenden Module übernehme ich inhaltlich aus der Research, nur mit kleinen Schärfungen:

- **Session Engine** — unverändert. Asia reduziert, London/NY normal, Newsfenster reduziert.
- **Triple VWAP** — unverändert; M1 + Tick-Volume, Vortags-VWAP weiterführen bis Anker erreicht. *(Vergleichbarkeit zu TV: siehe 6.3.)*
- **FVG Engine** — unverändert. H1 Hauptzonen, M5 Verfeinerung, M1 Trigger. **Neu:** FVG-Zonen werden auch an die Viz-Bridge gemeldet.
- **Market Structure** — unverändert. Close-basiert mit Mindest-Distanz/Zeit-Filter gegen Noise.
- **Candle/Momentum** — unverändert. Keine Pattern-Namen, nur quantitative Kennzahlen.
- **News/Makro** — unverändert. ±15 min Blackout vor / 5–15 min nach High-Impact. Kalender-API config-driven.
- **Risk Engine** — Limits aus Research: Risk/Trade 0,5/1/2 % je Score-Band (Scout/Good/A+), Tagesverlust 3–4 %, Wochenverlust 6–8 %. *(Executive-Summary-Limits ignoriert, die waren laxer.)*
- **Scoring (0–100)** — Gewichtung aus Research: H1-Zone 20, M5 15, VWAP 15, **HTF Volume Profile 20**, Session/Liq 10, News 10, Momentum 10. Schwellen: <55 kein Trade, 55–64 beobachten, 65–74 vorbereiten, 75–84 reduziert, ≥85 voll.
- **Journal/Review** — unverändert. Alles persistieren, KI darf Hypothesen vorschlagen, aber keine Live-Regeln ohne manuelle Freigabe ändern.

---

## 9. Build-Roadmap (gemappt auf die Build-Agents)

Reihenfolge für MiniMax Code. In Klammern der zuständige Agent (siehe `/agents`).

1. **Repo-Skeleton + Docker-Grundgerüst + Connector-Interface** (`07_devops_docker_viz`, `02_data_layer_mt5_bridge`)
2. **ReplayConnector + PaperBroker + Beispiel-Datensatz laden** (`02_data_layer_mt5_bridge`)
3. **Data Layer:** OHLCBuilder, SpreadMonitor, DataQualityMonitor (`02`)
4. **Basis-Features:** Session, TripleVWAP, MarketStructure, Candle/Momentum (`03_feature_engine`)
5. **FixedVolumeRangeEngine** (Weekly → Monthly → Yearly, locked/developing) (`03`)
6. **FVG + Liquidity Engine** (`03`)
7. **NewsContextEngine** + Kalender-API (`03`)
8. **FeatureAggregator + ScoringEngine** (`04_decision_scoring`)
9. **Execution MVP:** RiskManager, PositionSizer, OrderManager, SL/TP (`05_execution_risk`)
10. **TradeJournalDB + FeatureSnapshotStore** (TimescaleDB) (`06_journal_backtest_review`)
11. **AIDecisionLayer** (OpenRouter) parallel zu RuleBasedFallback (`04`)
12. **MT5-Viz-Bridge + `BotOverlay.mq5`** (`07`)
13. **BacktestEngine + WalkForwardEngine** (`06`)
14. **Daily/WeeklyReview + FittingProposal** (`06`)
15. **LiveMT5Connector + mt5-terminal-Container** (Wine-Bridge) (`02`, `07`)
16. **Demo-Forward auf Ubuntu** → Monitoring → (erst dann) Live mit Mini-Volumen.

---

## 10. Repo-Struktur & Docker-Compose-Skizze

```
xauusd-bot/
├── docker-compose.base.yml
├── docker-compose.dev.yml      # Mac: kein mt5-terminal, Connector=replay
├── docker-compose.prod.yml     # Ubuntu: + mt5-terminal (Wine)
├── .env.example
├── pyproject.toml
├── src/xauusd_bot/
│   ├── connectors/   # IMarketConnector, ReplayConnector, LiveMT5Connector, PaperBroker
│   ├── data/         # OHLCBuilder, SpreadMonitor, DataQualityMonitor
│   ├── features/     # session, vwap, volume_range, fvg, structure, momentum, liquidity, news
│   ├── decision/     # aggregator, scoring, ai_layer (openrouter), rule_fallback, qualification
│   ├── execution/    # risk, sizer, order, pending, stop, tp, emergency
│   ├── journal/      # db models (timescale), snapshot store
│   ├── review/       # backtest, walkforward, daily/weekly review, fitting
│   ├── viz/          # overlay file writer
│   └── common/       # config, schemas (pydantic), messaging (redis streams), logging
├── mql5/BotOverlay.mq5
├── docker/
│   ├── mt5-terminal/ # Dockerfile (Wine+MT5+RPyC-Server), VNC
│   └── service/      # Dockerfile (Python services, shared base)
└── tests/
```

**Compose-Steuerung:** `base` definiert redis, timescaledb, alle Python-Services mit `CONNECTOR_MODE`-Env. `dev` overridet `CONNECTOR_MODE=replay` und lässt `mt5-terminal` weg. `prod` fügt `mt5-terminal` hinzu und setzt `CONNECTOR_MODE=live`.

```bash
# Mac
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml up
# Ubuntu
docker compose -f docker-compose.base.yml -f docker-compose.prod.yml up
```

---

## 11. Offene Punkte / Entscheidungen für dich

1. **Wochen-Definition Volume Profile:** ISO-Woche (Mo 00:00 UTC) oder Broker-Woche (So 22:00–Fr)? Default im Plan: ISO Mo–So UTC, konfigurierbar.
2. **Kalender-API für News:** TradingEconomics / FXStreet / Broker-Kalender? (config-driven, du kannst später wechseln.)
3. **Dashboard ja/nein** im ersten Wurf, oder reicht dir der MT5-Overlay + Journal-DB-Queries für den Anfang?
4. **Historische Datenquelle für Backtest/Replay:** Vantage-Tick-Export, oder ein externer XAUUSD-M1-Datensatz für die erste Entwicklung auf dem Mac?

Keine davon blockiert den Start — die Agents sind so gebaut, dass sie mit den Defaults loslegen können.
