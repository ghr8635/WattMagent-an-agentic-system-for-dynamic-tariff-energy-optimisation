# Appliances: shiftable cycles and the hot-water tank

Status: **slice 3**. Four behaviour classes, one optimiser, one independent verifier, a TOML
household file and a small extensible catalogue. It is **not yet connected to the agent or the
API**, and it is planned separately from the EV and battery (see "Limits").

## Behaviour classes

| Class | Examples | Model | In this slice |
|---|---|---|---|
| Interruptible, power-flexible | EV, home battery | MILP in `optimiser/household.py` | done earlier |
| **Shiftable cycle** | washing machine, dryer, dishwasher | fixed hourly power profile, runs once, no pause, inside a window | **done** |
| **Thermal store with a comfort band** | electric water heater | one-node tank, temperature band, on/off or modulating element | **done** |
| **Thermal store, building mass** | air heat pump | one-node building, comfort band, COP from the outdoor temperature | **done** (heat pump; resistive heating and AC not started) |
| **Generation** | rooftop PV | radiation x kWp x performance ratio; own use first, export or curtail | **done** (the model; yield not yet checked on a full real year) |
| Fixed loads | fridge, cooking, lighting, TV, standby | demand profile, never scheduled | not started |

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
- Daylight-saving days of 23 and 25 hours map windows to the right slots.

## Limits (honest)

- **Every appliance figure is an assumption**, not a measurement of any product.
- **Hourly slots.** A two-hour wash is two averaged hours; real programmes vary within the hour.
- **Planned separately from the EV and battery.** The EV and battery are not part of
  `DayInputs`, so nothing here shares a connection limit with them, and a battery could shift
  appliance energy further. The base load enters only through the connection limit.
- **Solar is a deterministic estimate.** Radiation is the day's actual value (perfect foresight);
  no shading, snow, panel temperature or inverter clipping, and no battery to store the surplus.
- **One-node building.** No separate rooms, walls or floor heating, no solar or internal gains,
  no defrost or minimum run time, a straight-line COP, and heating only (no cooling).
- **One-node tank.** No stratification, no weekly high-temperature (legionella) cycle, no
  user-specific draw; the draw is a fixed daily shape scaled to the household figure.
- **Little flexibility in a small band.** A 200 l tank with a 15 K band stores about 3.5 kWh, so
  heating can move by an hour or two, not by half a day. The numbers show that honestly.
- **Prices are assumed known for the whole day**, as in the EV backtest; the publication time of
  the next day's prices is unverified.
- **Windows are the user's.** Nothing here learns when a machine is actually loaded.
- No device control: the output is a plan, never a command to a machine.
