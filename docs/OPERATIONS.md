# Systembetreuer-Dokumentation — GoldManager (XAUUSD Bot)

> Betriebs-/Integrationshandbuch: Dienste, Ports, Endpunkte, Zugangsdaten,
> Konfiguration und Runbook. Stand: Service-Runtime + Custom-Dashboard +
> AI-Decision-Layer (MiniMax M3 via OpenRouter BYOK).
>
> **Secrets sind hier NICHT eingetragen** (sie gehören nicht ins Git). Dieses
> Dokument beschreibt *wo* Zugangsdaten liegen und *welche Struktur* sie haben.
> Die echten Werte stehen ausschließlich in `.env` auf dem jeweiligen Host.

---

## 1. Überblick

Der Bot ist eine Container-Topologie aus 5 entkoppelten Python-Stream-Services
plus Infrastruktur (Redis, TimescaleDB) und einem optionalen Web-Dashboard.
Kommunikation läuft über **Redis Streams**.

```
data-collector ──market_ticks──▶ feature-engine ──features──▶ decision-engine
  (Connector +                    (8 Feature-                   (Aggregator+Scoring+
   OHLCBuilder)                    Engines)                      Rule/AI(M3)+Qualify)
                                                                     │ decisions
                                                                     ▼
  journal-writer ◀──journal── execution-engine ──orders──▶ (Connector/PaperBroker)
   (Store-Sink)               (Risk+Stops+TP+Sizer+Order)

  dashboard (FastAPI) ── liest Streams (DB0) + Sessions (DB1), steuert Runtime-Toggles
```

**Betriebsmodi:** `dev` (Replay-Connector, kein MT5 — aktuell auf der VM) und
`prod` (LiveMT5Connector + `mt5-terminal`-Container unter Wine).

---

## 2. Hosts & Zugang

| Zweck | Wert |
|---|---|
| Test-/Dev-VM | `dev@192.168.178.192` (Ubuntu 24.04, Docker) |
| SSH | **Key-Auth** (kein Passwort). `sudo` auf der VM braucht Passwort (manuell). |
| App-Verzeichnis | `~/GoldManager` auf der VM |
| Git-Remote | `origin` = https://github.com/forliHD/GoldManager.git (Branch `dev`) |
| Dashboard | http://192.168.178.192:8080 (LAN) bzw. http://127.0.0.1:8080 (loopback) |

**Dashboard-Login (Dev/Test):** Benutzer `lucas`, Rolle `admin`.
Passwort steht als bcrypt-Hash in `.env` (`DASHBOARD_USERS`). Das Test-Passwort
ist **vor Produktivnutzung zu ändern** (siehe §8).

---

## 3. Dienste, Ports & Healthchecks

Alle Python-Dienste nutzen dasselbe Image `xauusd-bot/service:0.1.0`; die Rolle
wählt `SERVICE_ROLE` (Dispatcher `xauusd_bot.docker_entrypoint`). Das Dashboard
überschreibt den Entrypoint auf `python -m xauusd_bot.dashboard.app`.

| Container | Rolle / Inhalt | Host-Port | Healthcheck |
|---|---|---|---|
| `xauusd-redis` | Redis 7 (Streams + Runtime-Flags + Sessions) | `6379` | `redis-cli ping` |
| `xauusd-timescaledb` | TimescaleDB (PG16) | `5432` | `pg_isready` |
| `xauusd-data-collector` | Connector → Bars → `market_ticks` | — | Heartbeat `logs/data-collector.alive` |
| `xauusd-feature-engine` | 8 Feature-Engines → `features` | — | Heartbeat `logs/feature-engine.alive` |
| `xauusd-decision-engine` | Scoring + Rule/AI(M3) → `decisions` | — | Heartbeat `logs/decision-engine.alive` |
| `xauusd-execution-engine` | Risk/Stops/TP/Order → `orders`/`journal` | — | Heartbeat `logs/execution-engine.alive` |
| `xauusd-journal-writer` | `journal` → JournalStore | — | Heartbeat `logs/journal-writer.alive` |
| `xauusd-dashboard` | FastAPI + WebSocket + Charts | `8080` | `GET /api/health` |
| `xauusd-mt5-terminal` *(prod)* | Wine + MT5 + RPyC-Bridge | `18812` (intern), VNC intern | RPyC-Ping |

**Heartbeat:** Jeder Stream-Service schreibt alle 15 s `logs/<rolle>.alive`.
Ein hängender Event-Loop = stehender Heartbeat = unhealthy Container.

---

## 4. Netzwerk / Ports (Integration)

| Port | Dienst | Bind | Hinweis |
|---|---|---|---|
| 6379 | Redis | published `6379:6379` | DB **0** = Trading-Streams + Runtime-Flags; DB **1** = Dashboard-Sessions |
| 5432 | TimescaleDB | published `5432:5432` | User/PW/DB: `xauusd`/`xauusd`/`xauusd` |
| 8080 | Dashboard | `${DASHBOARD_BIND_HOST:-127.0.0.1}:8080` | Default loopback; `0.0.0.0` für LAN |
| 18812 | MT5-RPyC-Bridge *(prod)* | intern | nur im Compose-Netz `xauusd-net` |

Docker-Netz: `xauusd-net` (alle Container; Service-Namen sind DNS-auflösbar,
z. B. `redis`, `timescaledb`).

---

## 5. Redis-Keys & Stream-Topics

**Streams (DB 0):** `market_ticks`, `features`, `decisions`, `orders`, `journal`.
Nachrichten sind JSON-Envelopes (`schema_version`, `kind`, `produced_at`, `symbol`)
um die Domain-Schemas (`Bar`, `FeatureSnapshotBundle`, `Decision`+`Score`, …).

**Runtime-Konfig (DB 0):**
- `runtime:ai_layer_enabled` — `"true"`/`"false"`. Vom Dashboard geschrieben
  (`POST /api/ai/toggle`), von der `decision-engine` alle ~2 s gelesen.

**Dashboard (DB 1):** Session-Keys (Cookie-Sessions, bcrypt-Login).
- `dashboard:connector_mode` — vom Mode-Toggle geschrieben (Block-10-Vorbereitung).

Inspektion: `docker exec xauusd-redis redis-cli XLEN features`,
`… GET runtime:ai_layer_enabled`.

---

## 6. Dashboard-API (Endpunkte)

Basis-URL: `http://<host>:8080`. Auth via Cookie-Session nach `POST /api/auth/login`.
Rollen-Hierarchie: `viewer < operator < admin`.

| Methode & Pfad | Rolle | Zweck |
|---|---|---|
| `GET /api/health` | — | Liveness (auch bei Dashboard aus) |
| `POST /api/auth/login` | — | Login (`username`,`password` form-encoded) |
| `POST /api/auth/logout` | auth | Logout |
| `GET /api/auth/me` | auth | Aktuelle Session/Rolle |
| `GET /api/chart/candles`,`/api/chart/overlays` | viewer | Chart-Daten |
| `GET /api/journal/trades`,`/api/journal/aggregate` | viewer | Journal/KPIs |
| `GET /api/backtest/list`, `POST /api/backtest/run`, `GET /api/backtest/status` | operator | Backtests |
| `GET /api/review/daily`,`/api/review/weekly` | viewer | Reviews |
| `POST /api/fitting-proposal/{list,approve,reject,validate}` | operator | Vorschläge |
| **`GET /api/ai/state`** | viewer | AI-Layer-Status (`enabled`,`available`,`model`,`default`) |
| **`POST /api/ai/toggle`** | operator | AI-Layer an/aus (`{"enabled":bool}`) → schreibt `runtime:ai_layer_enabled` |
| `POST /api/mode/toggle` | admin | Connector-Mode (replay↔live), zusätzlich `DASHBOARD_LIVE_MODE_ENABLED` nötig |

UI: oben rechts **AI**-Pille + Toggle (operator/admin), **Mode**-Pille (admin).

---

## 7. Konfiguration (.env)

Liegt als `~/GoldManager/.env` auf der VM (nicht in Git). Vorlage: `.env.example`.

**Pflicht / Infrastruktur**
| Key | Beispiel | Bedeutung |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379/0` | Trading-Redis (DB 0) |
| `TIMESCALEDB_URL` | `postgresql+asyncpg://xauusd:xauusd@timescaledb:5432/xauusd` | Journal-DB |
| `CONNECTOR_MODE` | `replay` \| `live` | Datenquelle |
| `SYMBOL` | `XAUUSD` | Instrument |

**AI-Decision-Layer (MiniMax M3 via OpenRouter BYOK)**
| Key | Wert | Bedeutung |
|---|---|---|
| `OPENROUTER_API_KEY` | *(Secret)* | BYOK-Key — **rotierbar**, nur in `.env` |
| `OPENROUTER_MODEL` | `minimax/minimax-m3` | Festes Modell |
| `OPENROUTER_PROVIDER_ORDER` | `minimax/fp8` | **Provider-Pin** → BYOK erreicht MiniMax direkt (statt Reseller) |
| `OPENROUTER_ALLOW_FALLBACKS` | `false` | Kein Ausweichen auf andere Provider |
| `AI_LAYER_ENABLED` | `true` | Default-Zustand (Dashboard-Toggle überschreibt zur Laufzeit) |
| `AI_LAYER_SCORE_THRESHOLD` | `65` | LLM erst ab Score ≥ Schwelle (Kosten/Latenz) |
| `AI_LAYER_ZDR` | `false` | Zero-Data-Retention. **Inkompatibel mit dem MiniMax-Pin** (s.u.) |

> **BYOK-Hinweis:** Der Provider-Pin sorgt dafür, dass jeder Call an MiniMax
> (`minimax/fp8`) geht. Damit OpenRouter dabei *deinen* Key nutzt, muss der
> MiniMax-Key zusätzlich in OpenRouter → **Settings → Integrations** hinterlegt
> sein. Ohne Pin verteilt OpenRouter per Load-Balancing auf Novita/Parasail/etc.
>
> **ZDR-Konflikt:** `AI_LAYER_ZDR=true` schränkt auf ZDR-zertifizierte Endpoints
> ein — MiniMax's `minimax/fp8` ist **keiner**, also `zdr=true` + Pin → `404 No
> endpoints found`. Daher ZDR **aus** lassen, solange MiniMax-BYOK genutzt wird.
> `provider.data_collection="deny"` wird trotzdem immer gesendet (Datenschutz,
> MiniMax-kompatibel).

**Service-Runtime**
| Key | Default | Bedeutung |
|---|---|---|
| `REPLAY_SOURCE` | `data/sample/xauusd_m1_sample.parquet` | Replay-Quelle |
| `REPLAY_SPEED_SECONDS` | `0` | 0 = so schnell wie möglich |
| `REPLAY_LOOP` | `false` | Endlos-Replay (Achtung: feature-engine-Puffer wächst, O(history)) |
| `WARMUP_BARS` / `MAX_HISTORY_BARS` | `500` / `200000` | Bar-Puffer (live-Warmup / Obergrenze) |
| `STREAM_BLOCK_MS` / `STREAM_BATCH_SIZE` | `1000` / `64` | Consumer-Tuning |

**Dashboard**
| Key | Default | Bedeutung |
|---|---|---|
| `DASHBOARD_ENABLED` | `false` | Master-Schalter (dev-Compose erzwingt `true`) |
| `DASHBOARD_BIND_HOST` | `127.0.0.1` | Host-Interface des Ports; `0.0.0.0` = LAN |
| `DASHBOARD_USERS` | *(JSON)* | `{"user":{"password_hash":"<bcrypt>","role":"viewer\|operator\|admin"}}` |
| `DASHBOARD_REDIS_URL` | `redis://redis:6379/1` | Sessions (DB 1) |
| `DASHBOARD_REDIS_STREAMS_URL` | `redis://redis:6379/0` | Stream-/Runtime-Reads (DB 0) |
| `DASHBOARD_LIVE_MODE_ENABLED` | `false` | Gate für Live-Mode-Toggle |

> ⚠️ **bcrypt-Hash in `.env`:** bcrypt-Hashes enthalten `$`. Docker Compose
> interpoliert `$` → der Hash MUSS mit `$$` escaped werden
> (`$2b$12$…` → `$$2b$$12$$…`), sonst schlägt der Login fehl.

**Prod-only (MT5):** `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`,
`MT5_BRIDGE_HOST` (=`mt5-terminal`), `MT5_BRIDGE_PORT` (`18812`), `MT5_BRIDGE_AUTH_KEY`.

---

## 8. Runbook

Alle Befehle in `~/GoldManager` auf der VM.

**Start (Dev/Replay, inkl. Dashboard):**
```bash
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml up -d --build
```
**Status / Logs:**
```bash
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml ps
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml logs -f decision-engine
docker exec xauusd-redis redis-cli XLEN orders
```
**Update (neuer Code aus Git):**
```bash
git -C ~/GoldManager pull            # oder: rsync vom Dev-Rechner
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml up -d --build \
  --force-recreate decision-engine dashboard   # betroffene Dienste neu
```
**Stop:**
```bash
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml down
```
**Prod (Ubuntu, MT5):** `-f docker-compose.prod.yml` statt `-dev` + MT5-Vars in `.env`.

**Dev MIT echtem MT5 (Demo-Account testen):** Layer `docker-compose.mt5.yml`
auf base+dev — `data-collector`, `feature-engine`, `execution-engine` schalten
auf den Live-Connector (MT5-Bridge), Rest bleibt dev (LAN-Dashboard etc.).
Nur auf x86_64 (die VM), nicht auf Apple-Silicon.
```bash
# 1. .env: MT5_LOGIN / MT5_PASSWORD / MT5_SERVER (z.B. VantageInternational-Demo)
#    + VNC_PASSWORD setzen.
# 2. mt5-terminal-Image bauen (~10-15 Min, einmalig):
docker build -f docker/mt5-terminal/Dockerfile -t xauusd-bot/mt5-terminal:0.8.0 .
# 3. Stack mit MT5 starten:
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml \
               -f docker-compose.mt5.yml up -d
# 4. EINMALIGER MT5-Login per noVNC (Terminal verlangt interaktive Anmeldung):
#    ssh -L 6080:127.0.0.1:6080 dev@192.168.178.192   # Tunnel
#    Browser: http://localhost:6080/vnc.html  → im MT5-Terminal mit dem
#    Demo-Account einloggen. (Oder MT5_VNC_BIND_HOST=0.0.0.0 für LAN.)
# 5. Bridge-Health prüfen:
docker compose -f docker-compose.base.yml -f docker-compose.dev.yml \
               -f docker-compose.mt5.yml ps mt5-terminal      # healthy = Port 18812 lauscht
docker logs xauusd-data-collector 2>&1 | grep -i connector    # connector_factory_live
```
Zurück auf Replay: einfach ohne `-f docker-compose.mt5.yml` neu starten.

**Dashboard-User anlegen (bcrypt):**
```bash
docker run --rm --entrypoint python xauusd-bot/service:0.1.0 \
  -c "import bcrypt;print(bcrypt.hashpw(b'PASSWORT', bcrypt.gensalt()).decode())"
# Ausgabe in DASHBOARD_USERS, $ → $$ escapen!
```

**AI-Layer zur Laufzeit schalten:** Dashboard-Toggle, oder direkt:
```bash
docker exec xauusd-redis redis-cli SET runtime:ai_layer_enabled false
```

---

## 9. Sicherheit & bekannte Grenzen

**Sicherheit**
- `OPENROUTER_API_KEY` und Dashboard-Passwörter nur in `.env` (nicht in Git). Regelmäßig rotieren.
- Dashboard standardmäßig loopback; LAN-/Remote-Zugriff nur über vertrauenswürdiges
  Netz bzw. SSH-Tunnel: `ssh -L 8080:127.0.0.1:8080 dev@192.168.178.192`.
- Redis/TimescaleDB-Ports sind published — in Prod via Firewall/Netz absichern.
- Invariante I-1: `import MetaTrader5` nur im Bridge-Server, nie in den Services.

**Bekannte Grenzen**
- **TimescaleJournalStore ist ein Stub** (`NotImplementedError`) — `journal-writer`
  nutzt aktuell `InMemoryJournalStore` (prozess-lokal, nicht durable). Echte
  DB-Persistenz ist offen.
- **feature-engine ist O(history)** pro Bar — bei `REPLAY_LOOP=true` wächst der
  Puffer und der Durchsatz sinkt mit der Zeit.
- **Demo-Forward → Live (Block 10)** noch offen; LiveMT5Connector ist code-complete,
  aber nie gegen ein echtes Terminal gelaufen.
