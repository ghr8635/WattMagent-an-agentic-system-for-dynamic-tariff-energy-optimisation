# WattMagent: an agentic system for dynamic-tariff energy optimisation

WattMagent plans when a household should use electricity, so flexible loads run in cheap
hours. Today that means an EV and a home battery, plus washing machine, dryer, dishwasher and
hot-water tank (separately planned), on German day-ahead prices; a heat pump, solar and
forecasting are not built yet. It sits on top of existing dynamic tariffs and home
automation instead of replacing them.

You tell it what you need in plain language ("80% by 7am, not before 22:00"). It returns a
schedule, checks that schedule independently, and explains it in a few sentences.

## Design principle

**A deterministic optimiser decides. Language is only the interface. A verifier gates everything.**

```
 "80% by 7am"                                           explanation + plan_id
      |                                                         ^
      v                                                         |
  parse -> validate -> plan (MILP optimiser) -> verify -> explain
  (rules,   (bounds,       |                     |  fail
   or LLM)   rounding)     |                     v
                           |              safe fallback plan
                           v              (charge as early as possible)
                       audit record
```

- **Optimiser:** a mixed-integer linear programme (SciPy / HiGHS) over one planning day, EV
  plus battery. It never calls a model.
- **Language layer:** parsing the request and wording the explanation. By default this is
  deterministic (rules and templates). A language model can replace it, but only as a
  proposal: everything it returns is validated, and numbers it adds are rejected.
- **Verifier and fallback:** every plan is re-checked by separate code (target reached, power
  limits, plug-in hours, battery state, cost against the baseline). If a plan fails, or prices
  are stale or missing, the safe fallback is used. Every decision is written to an audit log.

## Status (honest)

| Area | State |
|---|---|
| Real price data: SMARD day-ahead (Bundesnetzagentur), converter for exports, **live feed** | done; the live feed was checked against the real service on 2026-11-01. Its layout is read from real responses, not an official contract, so the parser rejects anything unexpected |
| Tariff model (taxes, fees, time-of-use) | done; the shipped example is illustrative only |
| EV and home-battery simulators, baseline planners, backtest harness | done, tested |
| MILP optimiser (EV + battery), whole-household backtest on real prices | done; matches the cheapest-hours bound to the cent on the EV-only case (see `docs/results.md`) |
| Safety layer (verifier, freshness check, fallback, audit log) | done, tested, attacked with deliberately broken plans |
| Agent: request -> validated constraints -> safe plan -> explanation | done; built-in runner and LangGraph runner give identical results (parity test in CI) |
| Deterministic language backend (default), 40 labelled parsing cases | done; graded by `python -m wattmagent.graph --eval` and a CI gate. A regression guard, not a measure of accuracy on real users' wording |
| Replanning from the car's real charge, the time left and fresh prices | done, on request: a client has to call it, nothing triggers it by itself |
| HTTP API: API keys, rate limit, request IDs, idempotency, health and readiness | done, tested |
| Observability: Prometheus metrics, JSON logs, suggested alert rules | done; **no monitoring stack is deployed**, so nothing sends alerts yet |
| CI: lint, types, tests, secret scan, dependency audit, image build, container smoke test, image scan | green on GitHub Actions |
| Docker image (non-root, read-only, no capabilities) | built and smoke-tested in CI; **never run on a real host** |
| Language model backend (Hugging Face / local Ollama) | code and tests done with fakes; **no real model has produced a verified result**. A local 1.5B model answered with garbage, and the free Hugging Face tier has no inference credits |
| Appliances, slice 1: shiftable cycles (washer, dryer, dishwasher) and a hot-water tank, optimiser + independent verifier + safe fallback, TOML households, extensible catalogue | done, tested, **synthetic-price results only so far**; not yet connected to the agent or API; all appliance figures are assumptions (`docs/appliances.md`) |
| Heat pump, solar, weather, fixed-load profiles, forecasting | not started |
| Deployment to a host, rollback, load and failure tests, runbook | `deploy/deploy.sh` is written but untested; the rest not started |

No household savings figure is claimed. See "Results" below and `docs/results.md`.

## Run it

Python 3.12 or newer.

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m pytest                                      # no network needed
```

**Plan from the command line** (needs a price file, see "Data"):

```bash
python -m wattmagent.graph "80% by 7am, not before 22:00" --csv data/prices_hourly_utc.csv
python -m wattmagent.graph --eval                     # grade the parser on the labelled cases
```

**Run the API on live prices:**

```bash
export WATTMAGENT_API_KEYS="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export WATTMAGENT_PRICE_SOURCE=smard
python -m uvicorn --factory wattmagent.api.app:create_app --port 8000

curl -s localhost:8000/readyz                         # ready only if prices cover the next 24 h
curl -s -X POST localhost:8000/v1/plans \
  -H "X-API-Key: $WATTMAGENT_API_KEYS" -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-0001" -d '{"text": "80% by 7am"}'
# later, with the car's real charge:
curl -s -X POST localhost:8000/v1/plans/<plan_id>/replan \
  -H "X-API-Key: $WATTMAGENT_API_KEYS" -H "Content-Type: application/json" \
  -d '{"current_percent": 55}'
```

Settings are environment variables prefixed `WATTMAGENT_`; `.env.example` lists the main ones.
Details: `docs/api.md`, `docs/replanning.md`, `docs/deploy.md`.

**Appliances (cycles + hot-water tank), described in a TOML file:**

```bash
python -m wattmagent.appliances --household config/households/example.toml --synthetic --days 60
```

**Backtest on historical prices:**

```bash
python -m wattmagent.data.smard data/raw_smard_prices.csv data/prices_hourly_utc.csv
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household \
    --tariff config/tariffs/illustrative_de_household.toml
python -m wattmagent.backtest --synthetic --days 60 --seed 0      # labelled SYNTHETIC
```

## Results

Backtest on **real SMARD day-ahead prices**, 1,004 complete planning days (plug-in 18:00 to the
next 18:00, local time). On spot prices alone, the optimiser's EV charging cost was 32.9% below
charging immediately, and adding the home battery brought it 41.5% below, with the battery wear
cost set to zero. **These are not household savings:** the base load is synthetic, fees and
taxes are ignored in that run, prices for the next 24 hours are assumed known at plug-in, and
the battery's wear cost is an assumption that changes the battery's value a lot (at 8 ct/kWh the
battery's extra gain falls from 0.45 to 0.15 EUR/day). All runs, settings and caveats are in
`docs/results.md`; the raw outputs are in `docs/results/`.

## Safety and reliability, in practice

- The optimiser's plan is never executed unverified; a failed check gives the fallback plan.
- Prices older than 36 hours, with missing hours or with implausible values, force the fallback.
- A request over 500 characters is refused; a model's reply must be valid JSON whose numbers
  appear in the user's text, or it is discarded.
- A repeated request with the same `Idempotency-Key` returns the stored answer instead of
  planning twice; another API key can never replay or replan your plan.
- The audit log holds numbers and rule names only: no keys, no request text.

## Known limits

- One process only: the rate limit, idempotency store and plan store are in memory.
- Base load is synthetic; cost figures use wholesale prices unless a tariff file is given.
- Image and dependency versions are not locked (no lockfile, no base-image digest yet).
- No licence file has been chosen yet.

## Documentation

`docs/architecture.md` (modules and decisions), `docs/optimiser.md`, `docs/safety.md`,
`docs/agent.md` (language backends), `docs/replanning.md`, `docs/api.md`,
`docs/observability.md`, `docs/deploy.md`, `docs/data.md`, `docs/tariff.md`,
`docs/results.md`, `docs/appliances.md`, `docs/local-models.md`, and the open work in `docs/production-checklist.md`.

## Data credit

Price data: Bundesnetzagentur | SMARD.de, licensed CC BY 4.0.

## Rules this repository follows

- Every number in this README comes from a script in this repository.
- External calls sit behind interfaces; unit tests use fakes and need no network.
- Simulated and synthetic results are always labelled as such.
