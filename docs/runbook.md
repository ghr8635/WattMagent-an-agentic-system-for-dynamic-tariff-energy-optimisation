# Runbook

What to check and do when something is wrong. Written from the code, not from experience of
a real incident: there has been no production deployment yet, so treat the thresholds as
starting points. Commands assume the compose setup in `docs/deploy.md`.

## First look (any problem)

1. `curl -fsS http://localhost/healthz` - the process is up.
2. `curl -sS http://localhost/readyz` - 200 means prices are present, fresh and cover the next
   24 h; 503 lists the reasons.
3. `docker compose logs --tail 200 api` - each request has an id; the same id is in the
   `X-Request-ID` header and the audit record.
4. Metrics from inside the network: `curl -s api:8000/metrics` (blocked from outside).

## Symptoms

| Symptom | Likely cause | What to do |
|---|---|---|
| `/readyz` 503, reason about stale or missing prices | SMARD down or slow, or tomorrow's prices not published yet | Nothing breaks: plans fall back to the rule-based schedule and say so (`decision: fallback`). Wait for the feed; check `WATTMAGENT_PRICE_REFRESH_S`. Do not "fix" it by disabling the freshness check. |
| Many `decision: fallback` with `verifier_rejected` | A real defect: the optimiser produced a plan the independent verifier refused | Keep serving (the fallback is safe). Take the request ids, find the audit records, reproduce with the fingerprint's inputs, open an issue. Never relax the verifier to make the number go down. |
| `planner_error` in reasons | The solver failed or timed out (limit 10 s) | Same as above; check CPU limits on the host. |
| `PlanRejected` / 5xx on plans | Even the fallback broke a hard limit (for example a grid limit smaller than the base load) | The request or household config is unreachable; fix the household file. This refusal is intended. |
| 401 on every call | Key not in `WATTMAGENT_API_KEYS`, or the env file was not reloaded | Check the key list, restart `api`. |
| 429 | Per-key rate limit (default 20/min) | Expected under bursts; raise `WATTMAGENT_RATE_LIMIT_PER_MIN` only after a load test. |
| Idempotency replay returns an old answer | Same `Idempotency-Key` reused | By design; use a new key for a new request. |
| Plans vanish after a restart, `replan` says unknown id | Plan store is in memory | Known gap; clients must send a new plan request. |
| Different answers from two replicas | Rate limit and idempotency live in memory | Run **one** replica until they move to a shared store. |
| Container exits at start | Missing `WATTMAGENT_API_KEYS`, bad household file, or csv price source in production | The log names the setting; the app refuses to start rather than run unsafe. |

## Deploy and roll back

- Roll forward: `TAG=<new> ./deploy.sh` (it records the last good tag).
- Roll back by hand: `TAG=<previous good> docker compose up -d api`, then `/healthz`.
- `deploy.sh` has never run on a real host. Rehearse it on a throwaway machine first.

## Before you trust a change

- `python scripts/load_test.py --url ... --key ... --appliances` against a staging copy with a
  raised rate limit. Record the machine and the numbers with the result.
- Run the full test suite; the hardening tests (`tests/unit/test_hardening.py`) inject solver,
  verifier and price failures and check that the answer stays safe.

## Rotating secrets

Add the new API key next to the old (comma-separated), restart, move clients, remove the old
one, restart. Never put keys in the image, the repo or the audit log.

## What this runbook does not cover

Alerting (no monitoring stack yet), backups (the audit log lives in a named volume), TLS
beyond Caddy defaults, and multi-replica operation.
