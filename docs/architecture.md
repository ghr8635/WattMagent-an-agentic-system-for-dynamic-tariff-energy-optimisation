# Architecture

## Layers

| Layer | Responsibility | State |
|---|---|---|
| `data` | Price series: types, synthetic generator, CSV loader | done |
| `devices` | Physics of each device; the simulator is the source of truth | EV, battery done |
| `tariff` | Spot price to all-in household price; every component sourced and dated | done |
| `planning` | Planners: (prices seen, load, device specs) -> kW schedules; plan status | EV baselines, household rule planners done |
| `optimiser` | MILP (HiGHS via SciPy): EV + battery, one planning day | done |
| `backtest` | Replay a price series; bill schedules on *actual* prices (EV-only and whole-household) | done |
| `llm` | HF Inference Providers client, fallback chain, budget, cache | done |
| `graph` | LangGraph agent: parse, plan, verify, execute, monitor, replan | planned |
| `api` | FastAPI: auth, rate limiting, streaming | health check only |

## Key decisions
1. **Optimiser decides, agent orchestrates.** Schedules come from deterministic
   code; the LLM never picks charging hours directly.
2. **Planners see a view, billing uses reality.** `run_day` bills against actual
   prices even if the planner saw a forecast (tested).
3. **Hourly grid for now.** Sub-hourly market data is averaged to hourly on load.
   Moving to the native resolution is a later, explicit change.
4. **Fail loudly on bad data.** Gaps, duplicate hours and partial hours raise.
5. **Infeasible requests are reported**, not silently "best effort": the plan has status `infeasible`, no schedule, and says what is reachable. The backtest counts those days and runs a rule-based fallback.
6. **The optimiser's cost must equal the simulator's bill.** Tests compare the two on random and negative-price days, which catches model/physics mismatches.
7. **SciPy/HiGHS is a core dependency**, not an optional extra: the optimiser is the product's core, and an extra would complicate CI and the image.

## Known limitations
- Hourly resolution only. Native 15-minute resolution is a later, explicit change.
- A planning day is plug-in hour to the next plug-in hour, solved with the next day's prices
  assumed known at plug-in (day-ahead publication time is unverified). Base load is synthetic
  and assumed known. No solar, no grid export, no heat pump yet.
- The battery starts every day at its initial charge and must end no lower, so days are
  independent. This ignores value carried between days (conservative for the battery).
- The battery wear cost is an assumption that changes the result a lot; see `docs/optimiser.md`.
- Local time and daylight-saving days (23 h and 25 h) are handled in the household backtest
  and covered by tests.
