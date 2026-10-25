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

## To do
### Safety and reliability
- [ ] Deterministic fallback plan when the LLM, price feed or API is unavailable
- [ ] Plan verifier: hard rules checked before any plan is accepted (target reached,
      power limits, departure time), with an audit log of every decision
- [ ] Data freshness check: stale or missing prices trigger the fallback
- [ ] Idempotent actions: repeating a request never double-books a device

### API and operations
- [ ] Authentication, rate limiting, request IDs, input validation
- [ ] Structured logs, metrics (latency, token use, cache hits, plan cost), tracing
- [ ] Health and readiness endpoints that check dependencies, not just "process is up"
- [ ] Locked dependency versions (lockfile) and reproducible builds
- [ ] Deployment with smoke test and automatic rollback
- [ ] Load test; failure tests (LLM down, price feed down, slow responses)
- [ ] Runbook, backup and restore of state, alerting plan

### Quality gates
- [ ] Agent evaluation set in CI with thresholds that fail the build on regressions
- [ ] Coverage gate raised as the code base grows (currently 70%)

### Privacy
- [ ] No personal data in logs; minimal data stored; retention documented
