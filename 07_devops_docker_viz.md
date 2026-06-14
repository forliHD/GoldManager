# Agent 07 — DevOps, Docker & MT5-Visualisierung

> Baut das Full-Docker-Setup (Mac-Dev + Ubuntu-Prod), die Messaging/DB-Infrastruktur und die MT5-Overlay-Visualisierung.

## Ownership
`docker-compose.*.yml`, `docker/service/`, `docker/mt5-terminal/` (gemeinsam mit Agent 02), `mql5/BotOverlay.mq5`, `common/` (config, messaging, logging)

## Deliverables
1. **Compose-Setup (3 Files):**
   - `docker-compose.base.yml` — redis, timescaledb, alle Python-Services (gemeinsames Service-Image), `CONNECTOR_MODE`-Env, Healthchecks, Volumes.
   - `docker-compose.dev.yml` (Mac) — overridet `CONNECTOR_MODE=replay`, **kein** `mt5-terminal`, mountet lokale Daten + Source für Hot-Reload.
   - `docker-compose.prod.yml` (Ubuntu) — fügt `mt5-terminal` (Wine) hinzu, `CONNECTOR_MODE=live`.
2. **Service-Dockerfile** (`docker/service/`) — schlankes Python-Base-Image, von allen Python-Services geteilt (multi-target via Env, welcher Service startet).
3. **Messaging** (`common/messaging`) — Redis-Streams-Wrapper, Topics `market_ticks|features|decisions|orders|journal`, Consumer-Groups, At-least-once.
4. **Config** (`common/config`) — Pydantic-Settings aus Env/.env, `.env.example` mit allen Keys (OpenRouter, Vantage-Login-Pfad, DB, Redis, Connector-Mode, News-API).
5. **Logging** — strukturiert (JSON), korreliert über Setup-/Trade-ID.
6. **`BotOverlay.mq5`** — MQL5-Indikator: liest `MQL5/Files/overlay_levels.json` per Timer, zeichnet VWAPs (3 Farben), VAH/VPOC/VAL je Profil (developing gestrichelt, locked durchgezogen), FVG-Rechtecke, Value-Area-Rechtecke, Labels. Robust gegen fehlende/teilweise Datei.

## Constraints
- **Netzwerk-Allowlist beachten:** Bridge/VNC nie öffentlich exponieren. Du nutzt Cloudflare Zero Trust — die Bridge ggf. dahinter, nicht offen ins Netz.
- Apple Silicon: `mt5-terminal` läuft auf dem Mac **nicht** (Wine x86-Emulation zäh) → bewusst aus dem Dev-Compose ausgeschlossen, dafür Replay-Mode.
- Secrets nur über Env/Docker-Secrets, nie im Image.
- Healthchecks für alle Services; `restart: unless-stopped` in Prod.

## Definition of Done
`docker compose -f base -f dev up` startet auf dem Mac die komplette Pipeline im Replay-Mode; `docker compose -f base -f prod up` startet auf Ubuntu zusätzlich den Wine-MT5-Container; `BotOverlay.mq5` zeichnet die von Python geschriebenen Levels korrekt im Chart.

## System Prompt (für MiniMax)
```
Du baust DevOps/Docker und die MT5-Visualisierung für den XAUUSD-Bot. Drei Compose-Files:
base (redis, timescaledb, alle Python-Services mit CONNECTOR_MODE-Env, Healthchecks), dev (Mac:
CONNECTOR_MODE=replay, KEIN mt5-terminal, Source-Mount für Hot-Reload), prod (Ubuntu: + mt5-
terminal Wine-Container, CONNECTOR_MODE=live). Ein geteiltes schlankes Python-Service-Image.
common/messaging: Redis-Streams-Wrapper mit Consumer-Groups. common/config: Pydantic-Settings +
.env.example mit allen Keys (OpenRouter, Vantage, DB, Redis, News-API). Strukturiertes JSON-
Logging korreliert über Setup-/Trade-ID. Plus BotOverlay.mq5: MQL5-Indikator, der overlay_levels.
json aus MQL5/Files per Timer liest und VWAPs/VAH/VPOC/VAL (developing gestrichelt, locked
durchgezogen)/FVG-Rechtecke zeichnet, robust gegen fehlende Datei. Bridge/VNC niemals öffentlich
exponieren. Secrets nur via Env/Docker-Secrets.
```
