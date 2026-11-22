# Production checklist

A box is ticked only when there is evidence: a test, a CI run, or a log. "Production"
here means built, tested, deployed and operated like a real service; there are no
real customers or devices.

## Done (evidence)
- [x] CI on every push: format, lint, strict types, tests, secret scan, dependency audit,
      container scan (green on GitHub Actions)
- [x] Typed configuration, no secrets in the repo, non-root container image
- [x] LLM client: model fallback, daily token budget, cache, structural validation of
      provider replies (unit tests)
- [x] Bad input fails loudly: price files (gaps, repeats, partial hours), SMARD exports,
      tariff files (missing or stale sources, implausible values)
- [x] Time handled in local time with daylight-saving nights tested (12 h and 14 h windows)
- [x] Results labelled by provenance: synthetic, illustrative tariff, spot-only vs all-in
- [x] Optimiser verified against the physics simulator (planned cost equals simulated bill + wear)
      on random and negative-price days; infeasible requests reported with a reason
- [x] Linter version pinned (`ruff~=0.16.9`) so local runs and CI agree

## To do
### Safety and reliability
- [ ] Deterministic fallback plan when the LLM or API is unavailable (done for optimiser failure,
      verifier rejection and stale prices; LLM and API cases arrive with the agent and API)
- [x] Home battery planned and verified independently (stored energy, limits, no overlap, no grid export, end level); each safety constraint was removed in turn to confirm a test fails (`tests/unit/test_battery.py`)
- [x] Plan verifier (independent of the optimiser): target, power limits, plug-in hours, executability,
      battery end state, cost vs baseline; attacked by tests with deliberately broken plans
- [x] Audit log: one structured record per decision (JSON Lines), no personal data
- [x] Data freshness check: stale, missing or implausible prices trigger the fallback (tested)
- [x] Hourly replanning of the household plan: elapsed hours pinned, same information changes nothing (time consistency tested), failed replans keep the previous plan, executed day verified on actual weather; each pinning rule was removed in turn to confirm a test fails (`tests/unit/test_replan.py`). Synthetic data only; not reachable from the API
- [ ] Hourly replanning on real forecasts: needs a verified forecast source (the Historical Forecast API layout and terms are unconfirmed)
- [x] Licence: MIT (`LICENSE`), copied into the image build
- [x] Replanning from the car's real charge and fresh prices; owner-scoped plan ids, time-limited, idempotent (tested; in-memory store, one replica)
- [x] Idempotent requests: `Idempotency-Key` replays the stored answer; the same key with another request is a 409 (in-memory store, per process; tested)

### API and operations
- [x] Authentication (API keys, constant-time), rate limiting, request IDs, input validation (tested)
- [x] State that survives restarts and is shared by several worker processes on one host: SQLite file (`WATTMAGENT_STATE_FILE`) for the rate limit, idempotency keys and plans; tested with four processes racing, and the same behaviour as the in-memory versions is checked step by step (`tests/unit/test_sqlite_state.py`). The race tests are probabilistic: on correct code they never fail, but a transaction bug is only caught on some runs
- [ ] Shared state for several **hosts** or replicas: SQLite is single-host only (use Redis or a database). Not yet verified inside the image on a real host: the CI smoke test now writes the file on a writable tmpfs, which is the closest check
- [ ] Per-IP throttling of failed logins, TLS and key rotation at the proxy
- [x] Live day-ahead price ingestion with real fetch time, stale-data fallback and strict parsing (tested on a recorded real sample)
- [x] Structured logs (JSON) and Prometheus metrics: requests, latency, plan outcomes and fallbacks, replays, rate limits, auth failures, price age (tested)
- [ ] Metrics not built yet: token use and cache hits (they matter once the model backend is on), plan cost; distributed tracing
- [x] Prometheus scrapes `/metrics` and evaluates six alert rules (optional compose profile `monitoring`; config and rule logic checked by promtool in CI, which has not run yet on a real host)
- [ ] Alerts delivered to someone: Alertmanager with a real route (e-mail, chat), a dashboard, a blackbox probe of `/readyz`, and unit tests for the other three alerts
- [x] Health (`/healthz`) and readiness (`/readyz`: prices present, fresh, covering the window)
- [ ] Locked dependency versions (lockfile) and reproducible builds
- [x] Container smoke test in CI: non-root user, read-only filesystem, no capabilities, 401 without a key, replay on repeated `Idempotency-Key` (`scripts/smoke_test.sh`)
- [ ] Deployment with automatic rollback: `deploy/deploy.sh` is written but untested, no host yet
- [ ] Locked image inputs: base image digest and a dependency lockfile (the image currently installs the newest allowed versions). The manual `Refresh pins` workflow produces both; they have not been run or committed yet
- [x] Failure tests: solver failure, nonsense or unreadable plans, price problems, planner crashes, junk input, 48 concurrent callers (`tests/unit/test_hardening.py`)
- [ ] Load test: `scripts/load_test.py` is written but has not been run against a real deployment, so there are no throughput numbers yet
- [ ] Failure tests still missing: language model down and slow responses end to end
- [x] Runbook (`docs/runbook.md`, written from the code, not from a real incident)
- [ ] Backup and restore of state, alerting plan

### Quality gates
- [x] Evaluation set in CI with thresholds for the request parser (30 labelled cases; regression guard only)
- [ ] Evaluation of the real model parser and explanations (needs the Hugging Face run)
- [ ] Coverage gate raised as the code base grows (currently 70%)

### Privacy
- [ ] No personal data in logs; minimal data stored; retention documented
- [x] Home Assistant advice mode: plans published as advice only (no service call, own entities and events only, dry run by default, expiry on every item, token never shown), tested against a fake server (`docs/home-assistant.md`)
- [ ] Verified against a real Home Assistant; a scheduled publisher; automatic control (not built on purpose, needs opt-in, per-device limits and a kill switch)

## Web page

- [x] Served by the API, strict CSP, key in memory only, data routes behind key and rate limit.
- [ ] Checked on a real host behind TLS, with real prices.
- [ ] Real consumption (Home Assistant history or a meter); the energy view is planned only.
- [ ] Heat pump and solar in the page's plan (needs a verified weather forecast).
- [ ] Cross-browser and screen-reader check.
