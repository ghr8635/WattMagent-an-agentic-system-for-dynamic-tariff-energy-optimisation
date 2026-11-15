# Results on real prices (and what they do not show)

Three backtests on **real SMARD day-ahead prices**, 1,004 complete planning days
(plug-in 18:00 to the next 18:00, local time, Germany/Luxembourg, Jan 2024 to Oct 2026).
Everything else is an assumption. **These are not household savings.**

Raw outputs are in `docs/results/`; each file starts with the settings it used.

| Run | Cost basis | Battery wear | `ev_immediately` EUR/day | EV optimised EUR/day | + battery EUR/day (net) | Battery gain EUR/day | Battery energy kWh/day |
|---|---|---|---|---|---|---|---|
| A | spot only | 0 ct/kWh | 5.213 | 3.499 | 3.049 | 0.450 | 6.62 |
| B | spot only | 8 ct/kWh | 5.213 | 3.499 | 3.346 | 0.153 | 2.10 |
| C | illustrative all-in tariff | 3 ct/kWh | 15.780 | 13.740 | 13.478 | 0.262 | 3.27 |

"Net" = grid bill + battery wear. "Battery gain" = EV-optimised minus + battery (net).
Run C used a different cost basis from A and B, so compare its gain with theirs only loosely.

## What was measured
1. **The optimiser is correct on the EV-only case.** `milp_ev_only` equals the
   `ev_cheapest_hours` bound to the cent on all 1,004 real days in all three runs.
2. **Percentages shrink on an all-in price.** The EV saving is -32.9% of the spot bill but
   -12.9% of the all-in bill, because about 19 ct/kWh of fees and taxes does not change by hour.
   In euros the saving is the spot saving times 1.19 (VAT): 1.714 x 1.19 = 2.040. That matches,
   which is a consistency check of the tariff model.
3. **The battery's value depends strongly on its wear cost.** Net gain falls from
   0.45 EUR/day at 0 ct to 0.15 EUR/day at 8 ct, but does not vanish: the cycles that
   remain are the ones on days with large price spreads. (An earlier note here guessed that
   little would be left above about 7 ct; that was wrong, it confused the average gain per
   kWh with the gain of the best cycles.)
4. **Safety:** every optimiser plan in all runs (2,008 per run) passed the verifier; no
   fallback was needed.

## What this does not show
- **Not household savings.** The tariff in run C is illustrative; run A and B ignore fees.
- **The base load is synthetic** (9 kWh/day, a fixed daily shape) and assumed known in advance.
- **Battery wear and efficiency are assumptions**; the wear cost is the biggest lever.
- **Perfect knowledge of the next 24 h of prices** from plug-in time. Day-ahead prices are
  published in the afternoon, but the exact publication time is unverified.
- **No purchase price, financing or payback is considered.** No claim is made that a
  battery would pay for itself.
- No solar, no grid export, no heat pump, hourly resolution, a single EV at a fixed schedule.
- Results are not annualised on purpose.

## Reproduce
```
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household --battery-wear 0
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household --battery-wear 0.08
python -m wattmagent.backtest --csv data/prices_hourly_utc.csv --household \
    --tariff config/tariffs/illustrative_de_household.toml
```
Data: Bundesnetzagentur | SMARD.de, CC BY 4.0 (see `docs/data.md`).
