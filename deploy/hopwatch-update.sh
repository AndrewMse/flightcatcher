#!/usr/bin/env bash
# Pull the latest Hopwatch image and restart onto it -- but only when the
# booker says nothing is in flight.
#
# A restart kills whatever the booker holds in memory: a prefilled booking
# waiting for approval, or worse, a multi-leg trip halfway through confirming.
# So the rule is "restart only on a clear yes": a booker that is running but
# cannot be asked is left alone until it can.
#
# Run by hopwatch-update.timer every 15 minutes.
set -euo pipefail

COMPOSE_DIR="${COMPOSE_DIR:-/opt/hopwatch/deploy}"
SERVICE="${HOPWATCH_SERVICE:-hopwatch}"
STATUS_URL="${HOPWATCH_STATUS_URL:-http://127.0.0.1:8765/api/status}"

cd "$COMPOSE_DIR"

docker compose pull --quiet "$SERVICE"

image="$(docker compose config --images "$SERVICE" | head -n 1)"
latest="$(docker image inspect --format '{{.Id}}' "$image")"
container="$(docker compose ps -q "$SERVICE" || true)"

if [ -z "$container" ]; then
  docker compose up -d "$SERVICE"
  echo "hopwatch: was not running; started $image"
  exit 0
fi

running="$(docker inspect --format '{{.Image}}' "$container")"
if [ "$running" = "$latest" ]; then
  echo "hopwatch: up to date"
  exit 0
fi

if ! status="$(curl -fsS --max-time 10 "$STATUS_URL")"; then
  echo "hopwatch: deferred: update ready but the booker is not answering at $STATUS_URL"
  exit 0
fi

verdict="$(printf '%s' "$status" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except ValueError:
    data = {}
print("yes" if (data.get("watcher") or {}).get("safe_to_restart") is True else "no")
')"

if [ "$verdict" != "yes" ]; then
  echo "hopwatch: deferred: a booking is in progress"
  exit 0
fi

docker compose up -d "$SERVICE"
docker image prune -f >/dev/null || true
echo "hopwatch: updated to $image"
