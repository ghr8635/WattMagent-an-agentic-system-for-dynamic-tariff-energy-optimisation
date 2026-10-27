# Optimiser

One planning day, one household: an EV and an optional home battery behind one meter.
Solved as a mixed-integer linear programme with HiGHS (via `scipy.optimize.milp`).
Typical solve time on a laptop is a few milliseconds per day.

## What it decides, per hour
Grid import, battery charge/discharge, EV charging power.

## Model (see the module docstring in `src/wattmagent/optimiser/household.py`)
```
minimise    sum_t price_t * grid_t  +  wear * sum_t battery_discharge_t
balance     grid + discharge = base_load + charge + ev            (no export modelled)
battery     energy_t = energy_{t-1} + eta_c*charge - discharge/eta_d, within [min, max]
            end of day >= start of day;  charge and discharge never in the same hour (binary)
EV          sum(eta * ev_kw) = energy needed to reach the target; only while plugged in
```
The EV is charged to the target and no further. Prices can be negative; a pre-check
reports an unreachable EV target (with the SoC that *is* reachable) instead of
approximating it.

## Assumptions that change the answer
| Assumption | Default | Why it matters |
|---|---|---|
| Battery wear cost | 0.03 EUR per kWh delivered | A real figure depends on price, cycle life and depth of discharge. Run with `--battery-wear 0` and with a higher value to see the sensitivity. |
| Base load | synthetic, 9 kWh/day | Without load the battery has nothing to supply. A real meter export should replace it. |
| Foresight | next day's prices known at plug-in | Day-ahead prices are published in the afternoon (time unverified). Load is also assumed known. |
| Battery start/end | 50% start, no lower at the end | Days are independent. |
| Efficiency | 95% charge, 95% discharge | Typical order of magnitude, not a product datasheet. |

## Run
```
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household \
    --tariff config/tariffs/illustrative_de_household.toml
```
Strategies, in order: `ev_immediately` (baseline), `ev_cheapest_hours`, `milp_ev_only`,
`milp_ev_and_battery`. `milp_ev_only` must equal `ev_cheapest_hours` (tested): if it does
not, something is wrong. `bill/day` is what the grid charges; `net/day` adds battery wear.
`saved/day` and `vs base` compare `net/day` with the baseline.

Results from `--synthetic` or the illustrative tariff only exercise the code.
