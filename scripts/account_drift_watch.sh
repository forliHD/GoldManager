#!/usr/bin/env bash
# account_drift_watch.sh — detect & self-heal connector-mode drift on the live VM.
#
# The live stack must run CONNECTOR_MODE=live (base+dev+mt5). An ad-hoc
# `docker compose up` with a partial -f set silently reverts the connector-using
# engines to replay/paper — the execution-engine then publishes a fake $10000
# paper account to Redis `state:account` and never touches the real MT5 demo
# account. The bridge self-heal supervisor only checks port 8001, so it does NOT
# catch this. This watch does: it compares the running CONNECTOR_MODE + the
# published account against "live", and on drift re-applies the full live compose
# set (COMPOSE_FILE in .env pins base+dev+mt5, so a bare `up -d` is correct).
#
# Install (cron, every 5 min):
#   */5 * * * * /home/dev/GoldManager/scripts/account_drift_watch.sh
# Pause healing during intentional maintenance:
#   touch /home/dev/GoldManager/.drift_watch_pause
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin:${PATH:-}

DIR="${GM_DIR:-/home/dev/GoldManager}"
cd "$DIR" 2>/dev/null || exit 1
mkdir -p logs
LOG="logs/drift_watch.log"
ts() { date -u +%FT%TZ; }

mode() {
  docker inspect "xauusd-$1" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | grep '^CONNECTOR_MODE=' | cut -d= -f2
}

exec_mode="$(mode execution-engine)"
dec_mode="$(mode decision-engine)"
acct="$(docker exec xauusd-redis redis-cli GET state:account 2>/dev/null || true)"

drift=0; reason=""
[ "$exec_mode" != "live" ] && { drift=1; reason="$reason exec_mode=${exec_mode:-MISSING}"; }
[ "$dec_mode"  != "live" ] && { drift=1; reason="$reason dec_mode=${dec_mode:-MISSING}"; }
# Paper-broker signature (replay default): USD + balance 10000. The live demo
# account is EUR, so requiring both avoids any false positive on the live book.
if echo "$acct" | grep -q '"currency": "USD"' && echo "$acct" | grep -q '"balance": 10000'; then
  drift=1; reason="$reason paper_account"
fi

if [ "$drift" = 0 ]; then
  echo "$(ts) ok (exec=$exec_mode dec=$dec_mode, account live)" >> "$LOG"
  exit 0
fi

if [ -f "$DIR/.drift_watch_pause" ]; then
  echo "$(ts) DRIFT detected:$reason — HEAL PAUSED (.drift_watch_pause present)" >> "$LOG"
  exit 0
fi

echo "$(ts) DRIFT detected:$reason — self-healing (compose up -d, live set)" >> "$LOG"
# COMPOSE_FILE in .env = base+dev+mt5 → bare compose is the full live set.
docker compose up -d data-collector feature-engine decision-engine execution-engine >> "$LOG" 2>&1
echo "$(ts) self-heal done; exec_mode now=$(mode execution-engine)" >> "$LOG"
