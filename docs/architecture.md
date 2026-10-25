# Architecture

## Layers

| Layer | Responsibility | State |
|---|---|---|
| `data` | Price series: types, synthetic generator, CSV loader | done |
| `devices` | Physics of each device; the simulator is the source of truth | EV done |
| `tariff` | Spot price to all-in household price; every component sourced and dated | done |
| `planning` | Planners: (prices seen, device spec) -> kW schedule | baselines done |
| `backtest` | Replay a price series; bill schedules on *actual* prices | done |
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
5. **Infeasible requests are reported**, not silently "best effort" (`target_met`).

## Known limitations
- Windows are defined in the series' own clock; DST handling for local time is
  not implemented. Use UTC data or handle it before trusting results.
- The cheapest-hours bound is optimal only for one EV with linear cost and a
  power cap. With a battery or thermal storage it needs the MILP optimiser.
