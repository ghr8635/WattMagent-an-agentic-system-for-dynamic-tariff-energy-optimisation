#!/usr/bin/env bash
# Start the built image the way production runs it (non-root, read-only filesystem, no
# capabilities) and check the guarantees that matter. No internet and no real prices
# needed: the container plans on a small generated price file.
#
# Usage: bash scripts/smoke_test.sh wattmagent:ci
set -euo pipefail

IMAGE="${1:?usage: smoke_test.sh <image>}"
PORT="${SMOKE_PORT:-18000}"
BASE="http://127.0.0.1:${PORT}"
KEY="smoke-test-key-0123456789"
NAME="wattmagent-smoke-$$"
WORK="$(mktemp -d)"
FAILED=0

cleanup() {
  if [ "$FAILED" = 1 ]; then
    echo "--- container log ---" >&2
    docker logs "$NAME" >&2 2>&1 || true
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
  FAILED=1
  echo "SMOKE TEST FAILED: $*" >&2
  exit 1
}

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# Four days of hourly prices around now, so the container is "ready" whatever the time.
python3 - "$WORK/prices.csv" <<'PY'
import math
import sys
from datetime import datetime, timedelta, timezone

start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=24)
rows = ["timestamp,price_eur_mwh"]
for i in range(96):
    t = start + timedelta(hours=i)
    rows.append(f"{t.isoformat()},{100 + 60 * math.sin(i / 24 * 2 * math.pi):.2f}")
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    fh.write("\n".join(rows) + "\n")
PY
chmod 755 "$WORK"
chmod 644 "$WORK/prices.csv"

docker run -d --name "$NAME" -p "127.0.0.1:${PORT}:8000" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -e WATTMAGENT_API_KEYS="$KEY" \
  -e WATTMAGENT_PRICE_SOURCE=csv \
  -e WATTMAGENT_PRICE_CSV=/data/prices.csv \
  -v "$WORK:/data:ro" \
  "$IMAGE" >/dev/null

for _ in $(seq 1 60); do
  curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 1
done

curl -fsS "$BASE/healthz" | grep -q '"status":"ok"' || fail "/healthz did not answer ok"
[ "$(code "$BASE/readyz")" = 200 ] || fail "/readyz is not ready on fresh generated prices"
[ "$(docker exec "$NAME" id -u)" = 10001 ] || fail "container does not run as the non-root user"

BODY='{"text": "80% by 7am"}'
JSON=(-H 'Content-Type: application/json')

[ "$(code -X POST "${JSON[@]}" -d "$BODY" "$BASE/v1/plans")" = 401 ] || fail "request without a key was not refused"
[ "$(code -X POST "${JSON[@]}" -H 'X-API-Key: wrong-key-0123456789' -d "$BODY" "$BASE/v1/plans")" = 401 ] \
  || fail "wrong key was not refused"
[ "$(code -X POST "${JSON[@]}" -H "X-API-Key: $KEY" -d '{"text": ""}' "$BASE/v1/plans")" = 422 ] \
  || fail "empty request text was not rejected"

AUTH=(-H "X-API-Key: $KEY" -H 'Idempotency-Key: smoke-0001')
curl -s -D "$WORK/h1" -o "$WORK/b1" -X POST "${JSON[@]}" "${AUTH[@]}" -d "$BODY" "$BASE/v1/plans"
curl -s -D "$WORK/h2" -o "$WORK/b2" -X POST "${JSON[@]}" "${AUTH[@]}" -d "$BODY" "$BASE/v1/plans"
head -1 "$WORK/h1" | grep -q ' 200' || fail "plan request did not return 200: $(head -1 "$WORK/h1")"
grep -qi '^x-request-id:' "$WORK/h1" || fail "response has no X-Request-ID"
grep -qi '^idempotent-replay: true' "$WORK/h2" || fail "repeat with the same Idempotency-Key was not a replay"
cmp -s "$WORK/b1" "$WORK/b2" || fail "replayed answer differs from the first answer"

[ "$(docker inspect -f '{{.State.Running}}' "$NAME")" = true ] || fail "container stopped"
echo "SMOKE TEST PASSED"
