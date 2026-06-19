# Deploy-Anleitung — Dev/VM (Ubuntu)

Der Live-Stack läuft auf der VM `dev@192.168.178.192` unter `/home/dev/GoldManager`
mit drei Compose-Layern: **base + dev + mt5**. Service-Code ist als Volume
gemountet (`src/ → /app/src`), daher reicht für Code-Änderungen ein **Restart**.

## Voraussetzungen
- SSH-**Key-Auth** zur VM (kein Passwort-Prompt). Test: `ssh dev@192.168.178.192 echo ok`.
- `.env` existiert auf der VM (wird vom Deploy **nicht** überschrieben). Enthält
  u. a. `OPENROUTER_API_KEY`, `MT5_VNC_USER/PASSWORD`, `SYMBOL=XAUUSD+`,
  `NEWS_API_PROVIDER=forexfactory`, `CONNECTOR_MODE`, `DASHBOARD_USERS`.
- Docker + Compose v2 auf der VM.

## Schnell-Deploy (vom Mac)
```bash
scripts/deploy.sh            # rsync + up -d + Code-Services neu starten
scripts/deploy.sh --build    # zusätzlich Service-Images neu bauen (Dockerfile-Änderung)
scripts/deploy.sh --infra    # zusätzlich redis/timescaledb/mt5-terminal neu erstellen
```
Das Skript: rsync't das Repo (ohne `.env`/`data`/`logs`), fährt den Stack hoch,
startet die code-gemounteten Services neu (lädt `src/` neu) und zeigt Health +
Bridge-Status.

## Manuell (auf der VM)
```bash
cd /home/dev/GoldManager
CF="-f docker-compose.base.yml -f docker-compose.dev.yml -f docker-compose.mt5.yml"
docker compose $CF up -d                          # Stack starten/aktualisieren
docker restart xauusd-decision-engine xauusd-dashboard   # einzelne Services neu (Code)
docker compose $CF ps                             # Status
docker compose $CF logs -f execution-engine       # Logs
```

## MT5-Bridge (selbstheilend)
Die `mt5linux`-Bridge (Port 8001) wird vom **Supervisor** (`docker/mt5-terminal/
custom-cont-init.d/`) bei Container-Start automatisch hochgefahren und nach einem
Absturz innerhalb ~30 s neu gestartet — **kein manueller Helper mehr nötig**.
Notfalls manuell: `docker exec -u abc xauusd-mt5-terminal sh /usr/local/bin/mt5_bridge_up.sh`.
Browser-Zugang (MT5-Desktop): `http://192.168.178.192:3000` (User/PW aus `.env`).

## Persistenz & Sicherheit (Stand der Härtung)
- **Redis AOF aktiv** (mt5-Layer) → Management-Pläne (`mgmt:pos:*`) + Runtime-Flags
  überleben einen Redis-Neustart. Volume `redis_data`.
- **Redis (6379) + TimescaleDB (5432) nur auf `127.0.0.1`** gebunden (raus aus dem LAN).
- **Noch offen / empfohlen vor echtem Geld:** Redis `--requirepass` setzen (dann
  `REDIS_URL=redis://:<pw>@redis:6379/0` in `.env`), Dashboard (8080) + KasmVNC (3000)
  hinter SSH-Tunnel/TLS statt LAN (`DASHBOARD_BIND_HOST=127.0.0.1`,
  `MT5_VNC_BIND_HOST=127.0.0.1` + Tunnel), `OPENROUTER_API_KEY` + Dashboard-Passwort rotieren.

## Alerts (Telegram)
Live-Push bei Order (Entry/Reject), Management-Aktionen (TP/Trailing/Runner) und
Emergency-Stop. Einrichten:
1. Bot via **@BotFather** anlegen → Token. Bot anschreiben, dann
   `https://api.telegram.org/bot<token>/getUpdates` → `chat.id` ablesen.
2. In `.env`: `TELEGRAM_BOT_TOKEN=...` und `TELEGRAM_CHAT_ID=...`
   (optional `TELEGRAM_ALERTS_ENABLED=false` zum Stummschalten).
3. execution-engine neu starten: `docker restart xauusd-execution-engine`.
4. Im Dashboard (Live-Tab → **Alerts → „Telegram testen"**) prüfen, oder
   `curl -XPOST .../api/alerts/test` (admin). Ohne Token sind Alerts inaktiv.

## Runtime-Schalter (ohne Redeploy)
```bash
docker exec xauusd-redis redis-cli SET runtime:emergency_stop true|false   # Kill-Switch
docker exec xauusd-redis redis-cli SET runtime:ai_layer_enabled true|false # M3 an/aus
```
(oder über das Dashboard.)

## Rollback
Code: vorige Git-Version auschecken/rsyncen + Services neu starten.
Compose/Infra: vorige Compose-Datei rsyncen + `docker compose $CF up -d`.

## Smoke nach Deploy
```bash
docker exec xauusd-redis redis-cli XREVRANGE market_ticks + - COUNT 1   # Live-XAUUSD+-Bar
docker exec xauusd-redis redis-cli GET state:account                     # Account-Snapshot
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.178.192:8080/api/health
```
