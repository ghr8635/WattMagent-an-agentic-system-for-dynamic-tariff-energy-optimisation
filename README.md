# WattMagent: an agentic system for dynamic-tariff energy optimisation

WattMagent plans when a household should use electricity, so flexible loads (EV
charging first, then heat pump, battery, appliances) run in cheap hours. It sits
on top of existing dynamic tariffs and home automation instead of replacing them.

**Design principle:** A deterministic optimiser makes the scheduling decisions.
The LLM agent handles what optimisers are bad at: turning requests into
constraints, replanning when conditions change, explaining plans, and enforcing a
safety check before anything executes.

## Status (honest)

| Area | State |
|---|---|
| Price data: seeded synthetic generator, CSV loader (EUR/MWh, sub-hourly resampled to hourly) | done, tested |
| EV simulator (charger limit, losses, capacity) | done, tested |
| Baseline planners: charge-immediately, cheapest-hours (hindsight bound) | done, tested |
| Backtest harness (billing always uses actual prices) and CLI | done, tested |
| LLM client: HF Inference Providers, model fallback, daily token budget, cache | done, tested |
| Real market data ingestion, forecasting | not started |
| MILP optimiser, battery / heat pump models | not started |
| LangGraph agent, verifier, replanning | not started |
| API (health check only), Docker, CI/CD | scaffold, CI not yet run |

No savings figure is claimed. Results from `--synthetic` are labelled SYNTHETIC
and only exercise the code; they say nothing about real households.

## Run

```bash
uv sync --extra dev
uv run pytest
uv run python -m wattmagent.backtest --synthetic --days 60 --seed 0
uv run python -m wattmagent.backtest --csv data/prices.csv   # real data, see docs/data.md
```

## Rules
- Every number in this README comes from a script in this repo.
- External calls sit behind interfaces; unit tests use fakes and need no network.
- Simulated results are always labelled as simulated.

See `docs/architecture.md` and `docs/data.md`.
