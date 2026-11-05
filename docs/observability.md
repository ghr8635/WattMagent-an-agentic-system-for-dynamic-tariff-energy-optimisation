# Observability

## Logs

One line per request on stderr, so the container runtime collects them. Set
`WATTMAGENT_LOG_FORMAT=json` for one JSON object per line (use this in production) or
`text` for reading in a terminal; `WATTMAGENT_LOG_LEVEL` defaults to `INFO`.

Fields: `ts`, `level`, `logger`, `message`, and for requests `request_id`, `method`,
`path`, `status`, `duration_ms`, `key_id` (a short hash of the API key, never the key).
Only these fields are ever written from a record's extras, so request text, keys and
prices cannot leak through a careless log call. The `request_id` is the same one returned
in the `X-Request-ID` header and written to the audit record, so one id ties a client
report to its log line and its plan decision.

Before this was configured, Python dropped our `INFO` records; only warnings and uvicorn's
own access lines were visible.

## Metrics

`GET /metrics`, Prometheus text format, no extra dependency. Counters live in memory:
they reset on restart and are per process (one replica for now). Labels come only from
fixed sets (route templates, status codes, plan outcomes); request paths and content never
become labels, and each metric ignores new label combinations beyond 1000.

| Metric | Type | Meaning |
|---|---|---|
| `wattmagent_http_requests_total{method,route,status}` | counter | every request; unknown routes are `unmatched`, unknown methods `OTHER` |
| `wattmagent_http_request_duration_seconds{route}` | histogram | time to answer, buckets 5 ms to 10 s |
| `wattmagent_plan_results_total{status,decision}` | counter | `planned` / `needs_input` / `error`, and `optimised` / `fallback` / `none` |
| `wattmagent_idempotent_replays_total` | counter | answers served from the idempotency store |
| `wattmagent_rate_limited_total` | counter | requests refused with 429 |
| `wattmagent_auth_failures_total` | counter | requests refused with 401 |
| `wattmagent_price_age_hours` | gauge | age of the prices at last use, from their fetch time; absent before first use |
| `wattmagent_build_info{version}` | gauge | always 1 |

`wattmagent_price_age_hours` is read from the last time the service used prices, so it
needs traffic or readiness probes to stay current: an orchestrator that calls `/readyz`
regularly keeps it fresh.

`/metrics` has no login. The proxy (`deploy/Caddyfile`) answers 404 for it from outside;
scrape it from inside the network.

## Suggested alerts

These are recommendations to set up in a monitoring system. **No monitoring stack is
deployed yet**, so nothing sends these today.

| Alert | Rule (PromQL) | Why |
|---|---|---|
| Fallback rate high | `sum(rate(wattmagent_plan_results_total{decision="fallback"}[1h])) / sum(rate(wattmagent_plan_results_total{status="planned"}[1h])) > 0.1` | the safe plan is being used instead of the optimised one: prices stale or optimiser/verifier trouble |
| Prices stale | `wattmagent_price_age_hours > 30` | the freshness limit is 36 h; warn before the fallback starts |
| Server errors | `sum(rate(wattmagent_http_requests_total{status=~"5.."}[5m])) > 0` for 10 minutes | the API itself is failing |
| Slow plans | `histogram_quantile(0.95, sum by (le) (rate(wattmagent_http_request_duration_seconds_bucket{route="/v1/plans"}[5m]))) > 2` | the solver or price refresh is slow |
| Auth failures spike | `rate(wattmagent_auth_failures_total[5m]) > 1` | someone is guessing keys or a client has a wrong key |
| Not ready | probe `/readyz` returns 503 for 10 minutes | the price feed is down or incomplete |

The thresholds are starting points, not measured values: tune them once there is real
traffic.

## Not built yet

Token use and cache-hit metrics (they matter when the model backend is on), the plan cost
as a metric, distributed tracing, and the monitoring stack itself.
