# Appliances: cycles, tank, heat pump, solar, battery and fixed loads

Status: **slice 5**. Six behaviour classes, one optimiser, one independent verifier, a TOML
household file and a small extensible catalogue. It is **not yet connected to the agent or the
API**, and it is planned separately from the EV and battery (see "Limits").

## Behaviour classes

| Class | Examples | Model | In this slice |
|---|---|---|---|
| Interruptible, power-flexible | EV | MILP in `optimiser/household.py` | done earlier (separate planner) |
| **Stationary battery** | home battery | stored energy, charge/discharge limits and losses, wear cost, no grid export | **done** (inside the appliance planner; see below) |
| **Shiftable cycle** | washing machine, dryer, dishwasher | fixed hourly power profile, runs once, no pause, inside a window | **done** |
| **Thermal store with a comfort band** | electric water heater | one-node tank, temperature band, on/off or modulating element | **done** |
| **Thermal store, building mass** | air heat pump | one-node building, comfort band, COP from the outdoor temperature | **done** (heat pump; resistive heating and AC not started) |
| **Generation** | rooftop PV | radiation x kWp x performance ratio; own use first, export or curtail | **done** (the model; yield not yet checked on a full real year) |
| **Fixed loads** | fridge, freezer, cooking, lighting, TV, standby, home office | average kW for each local clock hour, never scheduled | **done** (illustrative profiles) |

## How it works

- `models.py`: `CycleSpec`, `CycleJob` (earliest start, deadline), `TankSpec`, `DayInputs`,
  `AppliancePlan`, and `tank_step`, the one physical model every checker uses.
- `optimiser.py`: a mixed-integer linear programme (HiGHS). One binary per (cycle, start slot)
  with exactly one chosen, so a cycle cannot be paused or split; a binary (or 0..1) duty per
  slot for the tank element; temperature in the comfort band in every slot and not colder at
  the end than at the start; an optional connection limit on base load plus appliances.
- `baselines.py`: `run_immediately` and `cheapest_start` (each cycle alone at its cheapest
  window), both with the same hourly thermostat for the tank.
- `verifier.py`: re-derives the plan from its power series and `tank_step`. Rules: cycle shape
  (exactly the profile once, no pause), cycle window, tank power, tank comfort band, tank end
  temperature, connection limit, cost not above the `run_immediately` baseline.
- `pipeline.py`: `plan_appliances_safely` = plan, verify, fall back to `run_immediately`, or
  refuse (`PlanRejected`) if even that breaks a hard limit; one audit record per call. The audit
  record holds counts and a fingerprint only, never appliance names.
- `household.py`, `catalogue.py`, `catalogue.toml`: the household file and the catalogue.

## The heat pump

The building is one heat store: `C dT/dt = Q - UA (T - T_out)`, with the pump delivering heat `Q`
between 0 and its maximum and drawing `Q / COP` of electricity. The COP is a straight line in the
outdoor temperature (`cop_at_7c`, `cop_slope_per_k`, clipped), and the outdoor temperature is
known in advance (a weather file or the Open-Meteo archive, see `docs/data.md`), so the whole
thing stays linear: the heat-pump part of the optimiser is a plain LP.

- The room must stay inside a comfort band (20-22 C in the catalogue) and end the day no colder
  than it started.
- The model **cannot cool**. A day whose mean outdoor temperature is above 15 C is treated as
  having no heating need and the pump is left out of that day's plan.
- **Two rule baselines.** `run_immediately` holds the start temperature. `cheapest_start`
  (named for its cycles) holds the lower quarter of the band, a *thrifty* thermostat. A colder
  house loses less heat, so an optimiser that lets the house drift down wins partly by running
  cooler. Judge the gain against the thrifty baseline.
- A heat pump needs weather, so `build_day_inputs` takes `outdoor_c` and the backtest needs an
  `outdoor` map; `python -m wattmagent.appliances` takes `--weather-json` (a saved Open-Meteo reply)
  or `--latitude/--longitude` (fetches the archive), or uses clearly labelled synthetic weather
  with `--synthetic`.

### How much it depends on the building

The building's thermal mass is the biggest unknown and moves the answer a lot.
`docs/results/heatpump_sensitivity_synthetic.txt` (made by `scripts/heatpump_sensitivity.py`,
**synthetic prices and weather**) shows the pump's *own* cost per day, so cycle and tank savings
do not flatter it:

| C (kWh/K) | immediate | thrifty | optimiser | optimiser vs thrifty |
|---|---|---|---|---|
| 3 | 2.215 | 2.187 | 1.860 | -15.0% |
| 5 | 2.215 | 2.204 | 1.770 | -19.7% |
| 10 | 2.215 | 2.246 | 1.720 | -23.4% |
| 20 | 2.215 | 2.393 | 1.668 | -30.3% |

Read it as "the gain grows with the assumed thermal mass", not as a forecast. It is also
helped by perfect foresight of prices and weather, which a real system would not have.

## Rooftop solar

PV generation per hour is `kWp x (radiation / 1000 W/m2) x performance ratio`, from the same
Open-Meteo reply as the temperature (global horizontal radiation). The performance ratio (0.8)
folds in tilt, orientation, temperature and inverter losses into one assumed number.

- **The cost becomes the whole net grid bill** when a household has solar: base load included,
  own solar used first, surplus exported. Without solar the cost stays "appliance energy only".
  The two are not comparable with each other.
- **Export value** = the export price, but never above that hour's import price, and negative
  when the import price is negative. This stops the optimiser from "profiting" by importing
  and exporting at the same time. When exporting would cost money, the surplus is curtailed.
- **The export price is an assumption** (0.08 EUR/kWh in the example, a round figure for a
  fixed feed-in rate). Check the rate that applies to a real installation; it is not verified here.
- The grid limit applies to **grid import** (base load plus appliances minus own solar).
- The independent verifier recomputes the net bill from the plan's power series; the optimiser's
  own cost matched it on 40 brute-force days and 10 days with everything switched on. (An early
  version double-counted appliance energy; the independent check caught it.)
- `cheapest_start` judges each cycle on the net bill, so it already moves a cycle into sunny
  hours; the optimiser's remaining gain comes from coordination, the tank and the heat pump.
- A model yield is printed per run (kWh per kWp over the period). Compare it with known yields
  (Germany: roughly 900-1,100 kWh/kWp a year on a good roof) **only over a full year**; a winter
  run says nothing about the annual figure.

## Fixed loads

Some loads cannot be moved: the fridge, cooking, lighting, the TV, standby, a home office.
They are not planned; they add to the household's demand, which matters in two places: how
much of the solar the house uses itself (and so what the net bill is), and the connection limit.

- A fixed load is a catalogue entry with `hourly_kw`: the average power for each **local clock
  hour** 0-23 (so it follows daylight-saving days correctly). The catalogue has a fridge-freezer,
  a freezer, induction cooking, LED lighting, TV, standby and network, and a home office; all
  figures are **illustrative assumptions** for a small household, not measurements.
- A household lists them with `[[fixed]]`, optionally `count = 2` (two people working from home)
  or its own `hourly_kw`; a new load is a table in the file, no code. Repeated appliances need
  a `name`.
- `daily_base_load_kwh` still exists as a catch-all for everything not listed; the two add up.
- Example: `config/households/example_detailed.toml` (7 listed loads, 9.7 kWh/day), run in
  `docs/results/appliances_detailed_synthetic.txt` (synthetic prices and weather; the optimiser
  is 8.7% below running immediately on that house's whole net bill).
- **Not a forecast.** The profile is the same every day (no weekdays, seasons or occupancy), so
  the optimiser plans against a load shape that a real house will not follow exactly.

## Home battery

A stationary battery stores energy and gives it back later: it buys in cheap hours (or keeps
surplus solar) and covers the house in dear hours.

- **Model.** Charge and discharge power at the meter (AC), losses on both (0.96 each, about 92%
  round trip), stored energy between a floor (10%) and the capacity, starting at 50%.
  `stored[t] = stored[t-1] + eta_c * charge[t] - discharge[t] / eta_d`.
- **Rules the plan must obey.** Never charge and discharge in the same hour (a binary variable;
  without it, a very negative price would "pay" the optimiser to burn energy through the losses),
  never end the day emptier than it started, never discharge more than the house uses (the
  battery does not export; only solar surplus is exported), and charging counts against the
  connection limit.
- **Wear cost.** Every kWh discharged carries `wear_eur_per_kwh` (0.05 in the catalogue). This is
  a stand-in for the price of the cycles used and **it decides whether the battery pays**: see the
  sensitivity table. It is an assumption, not a measured figure for any product.
- **Cost basis.** With a battery (or solar) the cost is the household's whole net grid bill,
  base load and wear included. Such results are not comparable with "appliance energy only".
- **Works without solar** (price arbitrage on the day-ahead prices); with solar it also stores
  the midday surplus for the evening.
- **Baseline.** `self_consumption` is `run_immediately` plus the usual rule: store solar surplus,
  cover the house when short, ignore prices, never dip below the starting level (so every day
  ends no emptier than it began, as the optimiser must). The optimiser's gain over it is the
  value of looking at prices; its gain over `run_immediately` is the whole battery.
- **Not planned from a request yet.** The agent plans only the cycles a request names, so the
  battery is part of the backtest and the library, not of the API.

Synthetic run (`docs/results/appliances_battery_synthetic.txt`, 365 days, seed 7, full house with
a 10 kWh battery at 0.05 EUR/kWh wear, perfect foresight):

| Strategy | Mean net cost per day | vs run immediately |
|---|---|---|
| run_immediately | 2.700 EUR | |
| cheapest_start | 2.609 EUR | -3.4% |
| self_consumption | 2.633 EUR | -2.5% |
| milp_optimiser | 1.957 EUR | -27.5% |

The same house **without** the battery: optimiser 2.249 EUR/day. So the battery's own gain is
0.29 EUR/day here, and it depends strongly on its wear cost
(`docs/results/battery_sensitivity_synthetic.txt`, `scripts/battery_sensitivity.py`):

| Wear cost (EUR per kWh discharged) | Optimiser gain (EUR/day) | Price-blind rule gain (EUR/day) |
|---|---|---|
| 0.00 | 0.72 | 0.15 |
| 0.02 | 0.52 | 0.12 |
| 0.05 | 0.29 | 0.07 |
| 0.10 | 0.06 | **-0.02** |
| 0.15 | 0.00 | -0.11 |
| 0.20 | 0.00 | -0.20 |

Size matters too (power set to half the capacity in kW, wear 0.05): 5 kWh gains 0.18 EUR/day,
10 kWh 0.29 and 15 kWh 0.35, so each extra kWh adds less than the one before.

Read it this way: on these **synthetic** prices the battery only pays if its wear cost is low,
and from about 0.10 EUR/kWh the optimiser simply leaves it idle while the price-blind rule loses
money. A realistic wear cost (device price divided by lifetime throughput) can easily be in that
range, so **this result may not carry over to a real battery**. Run the script on real prices
(`--csv` and `--weather-json`) before drawing any conclusion; none is recorded here yet.

### Real-data run (prices and weather)

`docs/results/appliances_full_real_prices.txt`: the full-house example on real SMARD prices and
real Open-Meteo weather, 1,035 complete days (about 2.8 years), run on the owner's machine.

| Strategy | Mean net cost per day | vs run immediately |
|---|---|---|
| run_immediately | 1.896 EUR | |
| cheapest_start (price- and solar-aware rule) | 1.756 EUR | -7.4% |
| milp_optimiser | 1.460 EUR | -23.0% (-16.9% vs cheapest_start) |

All 1,035 optimiser plans passed the independent verifier; none needed the fallback. The model
yield was 2,860 kWh/kWp over 1,036 days, about 1,008 kWh/kWp a year: inside the rough
900-1,100 range for Germany. That is a plausibility check on the radiation and performance-ratio
assumptions, not a calibration (the period is not a whole number of years).

**Not household savings.** Spot prices only (no fees, taxes or margin), a synthetic base load and
hot-water use, assumed appliance and building figures, an assumed export price, and perfect
foresight of the day's prices and weather.

## Asking in plain language (API)

`wattmagent.appliances.agent.run_appliance_agent` and `POST /v1/plans` (see `docs/api.md`) turn
"run the dishwasher before 8pm" into a verified plan. The parser (`appliances/request.py`) is
deterministic and reuses the charging parser's time rules; 18 labelled phrasings are tested.
The appliance names come from the household file and the catalogue, so adding a catalogue
entry teaches the parser its name without code (everyday aliases for washer, dishwasher and
dryer are built in). Limits: cycles only, English only, no exact start times ("start at 6pm" is
read as a deadline), no replanning.

## Describing a household

See `config/households/example.toml`. Appliances are catalogue keys plus a local-clock window.
Any catalogue value can be overridden in the household entry, and **a new appliance is a table
in the file**, no code:

```toml
[catalogue.cycle.my_washer]
description = "My washing machine"
profile_kw = [1.0, 0.6]

[[cycle]]
appliance = "my_washer"
earliest_hour = 6
deadline_hour = 12
```

Unknown keys, unknown appliances, windows shorter than the cycle and bad hours are errors with
the file name in the message, so a typo cannot silently change a household.

## Run it

```bash
python -m wattmagent.appliances --household config/households/example.toml \
    --csv data/prices_hourly_utc.csv --tariff config/tariffs/illustrative_de_household.toml
python -m wattmagent.appliances --household config/households/example.toml --synthetic --days 60
python -m wattmagent.appliances --household config/households/example_full.toml --synthetic --start 2025-06-01
python -m wattmagent.appliances --household config/households/example_battery.toml --synthetic --days 365 --seed 7
python scripts/battery_sensitivity.py --days 365 --seed 7     # how much the wear cost matters
python -m wattmagent.appliances --household config/households/example_detailed.toml --synthetic --days 365 --seed 7
```

`docs/results/appliances_*.txt` (cycles+tank, heat pump, full house in winter and summer) are **synthetic** runs (labelled in its header). A
run on the real SMARD series is done on your machine and is not recorded here yet.

## What the tests establish

- The optimiser equals a brute-force search over every start combination on 40 random price
  days, and its cost equals the independent simulation's.
- A tank plan stays in the band, ends no colder than it began, and never costs more than the
  thermostat, on 25 random days; a modulating element never costs more than an on/off one.
- Heat pump: the optimiser's plan is valid, its cost equals the independent simulation, and no
  random feasible schedule (300 per day, several days) is cheaper; a pump with too little power
  and a hot day are reported infeasible; the verifier flags an over-limit, frozen, overheated or
  end-cold house and a pump that breaks the connection limit.
- The verifier flags each of 18 deliberately broken plans: shifted, split, scaled, doubled or
  missing cycles, wrong shapes, NaN, overheated or frozen tank, partial power on an on/off
  element, a breached connection limit, a plan dearer than the baseline.
- The pipeline falls back on a crashing or lying planner, reports an unreachable comfort band,
  and refuses when even the fallback is unsafe.
- Battery: the optimiser's cost equals the independent recomputation on random price days;
  stored energy stays in range and ends no lower; charging and discharging never overlap (even
  when paid to import); battery energy is never exported; charging counts against the connection
  limit; a battery never makes the bill worse than none; the verifier flags overpowered, overlapping,
  overfull, over-drained, end-empty and exporting plans. Each safety constraint was removed in turn
  to check that a test fails.
- Fixed loads: bad definitions (wrong length, negative, NaN, unknown keys), bad counts and
  duplicate names are refused; `count` multiplies; profiles follow the local clock hour of each
  slot; more household load never lowers the whole bill; a cooking spike that uses up the
  connection limit moves a cycle out of its cheapest hour. Ignoring the loads, or ignoring
  `count`, makes tests fail. A regression test pins the default catalogue's contents.
- Daylight-saving days of 23 and 25 hours map windows to the right slots.

## Limits (honest)

- **Every appliance figure is an assumption**, not a measurement of any product.
- **Hourly slots.** A two-hour wash is two averaged hours; real programmes vary within the hour.
- **Planned separately from the EV.** The EV is not part of `DayInputs`, so nothing here shares
  a connection limit with it. The base load enters the cost only with solar or a battery, and
  otherwise only through the connection limit.
- **Solar is a deterministic estimate.** Radiation is the day's actual value (perfect foresight);
  no shading, snow, panel temperature or inverter clipping.
- **One-node building.** No separate rooms, walls or floor heating, no solar or internal gains,
  no defrost or minimum run time, a straight-line COP, and heating only (no cooling).
- **One-node tank.** No stratification, no weekly high-temperature (legionella) cycle, no
  user-specific draw; the draw is a fixed daily shape scaled to the household figure.
- **Fixed loads are one fixed daily shape**, not a measured or forecast demand: no weekday or
  season effects, no random behaviour, no link to the heat pump or the tank.
- **Little flexibility in a small band.** A 200 l tank with a 15 K band stores about 3.5 kWh, so
  heating can move by an hour or two, not by half a day. The numbers show that honestly.
- **Prices are assumed known for the whole day**, as in the EV backtest; the publication time of
  the next day's prices is unverified.
- **Windows are the user's.** Nothing here learns when a machine is actually loaded.
- **Battery:** one stored-energy number (no cell, temperature or ageing model beyond the wear
  cost), constant efficiencies, no battery-to-grid trading, a day-by-day horizon (it must end
  no emptier than it began, so it cannot carry value across days), and no real device figures.
- No device control: the output is a plan, never a command to a machine.
