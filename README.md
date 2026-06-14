# XAUUSD Bot — Planungspaket

Finaler Umsetzungsplan + Build-Agents (MiniMax Code) + Runtime-Prompts (OpenRouter/MiniMax BYOK).

## Inhalt

- **`00_FINAL_PLAN.md`** — Der finale Plan. Start hier. Enthält den Changelog (was ggü. der Research geändert wurde + warum, inkl. Joshuas Korrekturen), die Full-Docker-Architektur, den Mac→Ubuntu/Wine-Pfad, die korrigierte Volume Range Engine, die MT5-Viz-Bridge, die Backtesting-Realität und die Build-Roadmap.

- **`agents/`** — Build-Subagents für MiniMax Code. Jede Datei hat Ownership, Deliverables, Constraints, Definition-of-Done und einen fertigen System-Prompt zum Reinkopieren.
  - `01_orchestrator.md` — Lead, hält Architektur-Invarianten
  - `02_data_layer_mt5_bridge.md` — Connector-Abstraktion, Replay/Paper, Wine-Bridge
  - `03_feature_engine.md` — alle Features inkl. korrigierter Volume Range Engine
  - `04_decision_scoring.md` — Scoring + AI-Layer (OpenRouter)
  - `05_execution_risk.md` — deterministische „Hands"
  - `06_journal_backtest_review.md` — Journal, Backtest, WalkForward, Review
  - `07_devops_docker_viz.md` — Docker, Messaging/DB, `BotOverlay.mq5`

- **`runtime_prompts/`** — Prompts, die der Bot zur Laufzeit über OpenRouter/MiniMax lädt (nicht hardcoden).
  - `decision_agent.md` — Live-Trade-Entscheidung
  - `review_agent.md` — tägliches/wöchentliches Review + Fitting-Vorschläge
  - `news_context_agent.md` — optionale News-Relevanz-Klassifikation

## Reihenfolge
1. `00_FINAL_PLAN.md` lesen, die 4 offenen Punkte (§11) klären.
2. Orchestrator-Agent (01) als Lead in MiniMax aufsetzen.
3. Build-Roadmap (Plan §9) Schritt für Schritt durch die Subagents.
4. Alles im Replay-Mode auf dem Mac → dann Wine/MT5 auf Ubuntu → Demo-Forward → (erst dann) Live.
