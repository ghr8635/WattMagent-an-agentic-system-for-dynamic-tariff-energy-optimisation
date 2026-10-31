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
- [x] Plan verifier (independent of the optimiser): target, power limits, plug-in hours, executability,
      battery end state, cost vs baseline; attacked by tests with deliberately broken plans
- [x] Audit log: one structured record per decision (JSON Lines), no personal data
- [x] Data freshness check: stale, missing or implausible prices trigger the fallback (tested)
- [x] Idempotent requests: `Idempotency-Key` replays the stored answer; the same key with another request is a 409 (in-memory store, per process; tested)

### API and operations
- [x] Authentication (API keys, constant-time), rate limiting, request IDs, input validation (tested)
- [ ] Shared state for several replicas: rate limit and idempotency store are in memory, so per process (use Redis or a database)
- [ ] Per-IP throttling of failed logins, TLS and key rotation at the proxy
- [ ] Structured logs, metrics (latency, token use, cache hits, plan cost), tracing
- [x] Health (`/healthz`) and readiness (`/readyz`: prices present, fresh, covering the window)
- [ ] Locked dependency versions (lockfile) and reproducible builds
- [ ] Deployment with smoke test and automatic rollback
- [ ] Load test; failure tests (LLM down, price feed down, slow responses)
- [ ] Runbook, backup and restore of state, alerting plan

### Quality gates
- [x] Evaluation set in CI with thresholds for the request parser (30 labelled cases; regression guard only)
- [ ] Evaluation of the real model parser and explanations (needs the Hugging Face run)
- [ ] Coverage gate raised as the code base grows (currently 70%)

### Privacy
- [ ] No personal data in logs; minimal data stored; retention documented
