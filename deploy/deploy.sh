#!/usr/bin/env bash
# Deploy an image tag with a health check and automatic rollback.
# Usage (on the host, from this folder):  TAG=0.2.0 ./deploy.sh
#
# NOT YET TESTED: there is no host. Try it on a throw-away machine before trusting it.
# Rollback criterion is liveness (/healthz) only: /readyz can fail for a legitimate
# reason (the price feed), and rolling back would not fix that.
set -euo pipefail
cd "$(dirname "$0")"

STATE=".last_good_tag"
NEW="${TAG:?set TAG to the image tag to deploy}"
PREVIOUS="$(cat "$STATE" 2>/dev/null || true)"

healthy() {
  for _ in $(seq 1 30); do
    if docker compose exec -T api python -c \
      "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

export TAG="$NEW"
docker compose pull api
docker compose up -d api caddy

if healthy; then
  echo "$NEW" >"$STATE"
  echo "deployed $NEW"
  exit 0
fi

echo "health check failed for $NEW" >&2
if [ -n "$PREVIOUS" ]; then
  export TAG="$PREVIOUS"
  docker compose up -d api
  echo "rolled back to $PREVIOUS" >&2
else
  echo "no previous tag recorded: nothing to roll back to" >&2
fi
exit 1
