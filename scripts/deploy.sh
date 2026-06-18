#!/usr/bin/env bash
# Dev/VM deploy — sync the repo to the Ubuntu VM and (re)start the live stack.
#
# The VM runs the full live stack (base + dev + mt5 layers). Python service code
# is bind-mounted (src/ -> /app/src), so a code change only needs a service
# RESTART; compose/env changes need `up -d`; Dockerfile changes need `--build`.
#
# Usage:
#   scripts/deploy.sh             # rsync + up -d + restart code services
#   scripts/deploy.sh --build     # also rebuild the service images first
#   scripts/deploy.sh --infra     # also recreate redis/timescaledb/mt5-terminal
#   GM_VM=dev@host GM_DIR=/path scripts/deploy.sh   # override target
#
# Requires SSH key auth to the VM (no password prompts).
set -euo pipefail

VM="${GM_VM:-dev@192.168.178.192}"
DIR="${GM_DIR:-/home/dev/GoldManager}"
COMPOSE="-f docker-compose.base.yml -f docker-compose.dev.yml -f docker-compose.mt5.yml"
CODE_SERVICES="xauusd-data-collector xauusd-feature-engine xauusd-decision-engine xauusd-execution-engine xauusd-journal-writer xauusd-dashboard"

BUILD=0; INFRA=0
for a in "$@"; do
  case "$a" in
    --build) BUILD=1 ;;
    --infra) INFRA=1 ;;
    *) echo "unknown flag: $a"; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

echo "==> [1/4] rsync repo -> $VM:$DIR  (preserving VM .env / data / logs)"
rsync -az \
  --exclude='.git/' --exclude='.venv/' --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.env' --exclude='logs/' --exclude='data/' --exclude='.opencode/' \
  --exclude='graphify-out/' --exclude='*.egg-info/' --exclude='node_modules/' \
  ./ "$VM:$DIR/"

if [ "$BUILD" = 1 ]; then
  echo "==> [build] rebuilding service images on the VM"
  ssh "$VM" "cd $DIR && docker compose $COMPOSE build && \
    docker build -f docker/service-mt5/Dockerfile -t xauusd-bot/service-mt5:0.1.0 ."
fi

echo "==> [2/4] compose up -d  (applies compose/env changes)"
ssh "$VM" "cd $DIR && docker compose $COMPOSE up -d"

if [ "$INFRA" = 1 ]; then
  echo "==> [infra] recreating redis / timescaledb / mt5-terminal"
  ssh "$VM" "cd $DIR && docker compose $COMPOSE up -d --force-recreate redis timescaledb mt5-terminal && \
    sleep 12 && docker exec xauusd-redis redis-cli SET runtime:emergency_stop true >/dev/null && \
    echo 'NOTE: emergency_stop re-engaged after infra recreate — clear it in the dashboard when ready.'"
fi

echo "==> [3/4] restart code-mounted services (reload bind-mounted src/)"
ssh "$VM" "docker restart $CODE_SERVICES >/dev/null"

echo "==> [4/4] health"
sleep 6
ssh "$VM" "docker ps --format '{{.Names}}  {{.Status}}' | grep xauusd"
ssh "$VM" "docker exec xauusd-mt5-terminal sh -c 'ss -tuln | grep -q :8001 && echo \"mt5 bridge: UP\" || echo \"mt5 bridge: DOWN (supervisor will revive within ~30s)\"'"
echo "==> done. Dashboard: http://192.168.178.192:8080"
