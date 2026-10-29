# WattMagent: an agentic system for dynamic-tariff energy optimisation

WattMagent plans when a household should use electricity, so flexible loads (EV
charging first, then heat pump, battery, appliances) run in cheap hours. It sits
on top of existing dynamic tariffs and home automation instead of replacing them.

**Design principle:** a deterministic optimiser makes the scheduling decisions.
The LLM agent handles what optimisers are bad at: turning requests into
constraints, replanning when conditions change, explaining plans, and enforcing a
safety check before anything executes.

## Status

| Area | State |
|---|---|
| Price data: seeded synthetic generator, CSV loader (EUR/MWh, sub-hourly resampled to hourly) | done, tested |
| SMARD export converter (local time to UTC, DST-safe, fails loudly on bad data) | done; validated on a real SMARD export (24,119 hourly rows, Jan 2024 to Oct 2026) |
| Tariff model: sourced components, VAT, time-of-use fees, provenance checks | done, tested; shipped example is illustrative only |
| EV and home-battery simulators (power limits, losses, capacity, reserve) | done, tested |
| Baseline planners: charge-immediately, cheapest-hours (hindsight bound) | done, tested |
| Backtest harness (billing always uses actual prices; plug-in/departure in local time) and CLI | done, tested |
| LLM client: HF Inference Providers, model fallback, daily token budget, cache | done, tested |
| Live market data ingestion, forecasting | not started |
| MILP optimiser: EV + battery over one planning day, whole-household backtest | done, tested; backtested on real prices, see docs/results.md (not household savings) |
| Safety layer: independent plan verifier, price-freshness check, verified fallback, audit log | done, tested |
| Heat pump model, solar | not started |
| Agent: plain-language request -> validated constraints -> safe plan -> checked explanation (model with rule fallback; LangGraph or built-in runner) | done, tested with a scripted fake model; real-model run not yet verified |
| Replanning, API around the agent | not started |
| CI (lint, types, tests, security, image scan) | green on GitHub Actions |
| API (health check only), Docker deployment | scaffold only |

No household savings figure is claimed. Measured backtest results on real prices, with every
assumption stated, are in `docs/results.md`. Results from `--synthetic` are labelled SYNTHETIC
and only exercise the code; they say nothing about real households.

## Run

```bash
uv sync --extra dev
uv run pytest
uv run python -m wattmagent.backtest --synthetic --days 60 --seed 0
uv run python -m wattmagent.data.smard data/raw_smard_prices.csv data/prices_hourly_utc.csv
uv run python -m wattmagent.backtest --csv data/prices_hourly_utc.csv   # see docs/data.md
uv run python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household \
    --tariff config/tariffs/illustrative_de_household.toml   # EV + battery, see docs/optimiser.md
```

## Data credit
Price data: Bundesnetzagentur | SMARD.de, licensed CC BY 4.0.

## Rules
- Every number in this README comes from a script in this repo.
- External calls sit behind interfaces; unit tests use fakes and need no network.
- Simulated results are always labelled as simulated.

See `docs/architecture.md`, `docs/optimiser.md`, `docs/safety.md`, `docs/agent.md`, `docs/local-models.md`, `docs/results.md`, `docs/data.md`, `docs/tariff.md` and `docs/production-checklist.md`.
