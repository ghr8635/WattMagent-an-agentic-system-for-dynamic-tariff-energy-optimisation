# Appliances: shiftable cycles and the hot-water tank

Status: **slice 1**. Two behaviour classes, one optimiser, one independent verifier, a TOML
household file and a small extensible catalogue. It is **not yet connected to the agent or the
API**, and it is planned separately from the EV and battery (see "Limits").

## Behaviour classes

| Class | Examples | Model | In this slice |
|---|---|---|---|
| Interruptible, power-flexible | EV, home battery | MILP in `optimiser/household.py` | done earlier |
| **Shiftable cycle** | washing machine, dryer, dishwasher | fixed hourly power profile, runs once, no pause, inside a window | **done** |
| **Thermal store with a comfort band** | electric water heater | one-node tank, temperature band, on/off or modulating element | **done** |
| Thermal store, building mass | heat pump, electric heating, AC | same family, larger and weather-driven | not started |
| Generation | rooftop PV | needs a weather source (not yet verified) | not started |
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
```

`docs/results/appliances_synthetic.txt` is a **synthetic-price** run (labelled in its header). A
run on the real SMARD series is done on your machine and is not recorded here yet.

## What the tests establish

- The optimiser equals a brute-force search over every start combination on 40 random price
  days, and its cost equals the independent simulation's.
- A tank plan stays in the band, ends no colder than it began, and never costs more than the
  thermostat, on 25 random days; a modulating element never costs more than an on/off one.
- The verifier flags each of 18 deliberately broken plans: shifted, split, scaled, doubled or
  missing cycles, wrong shapes, NaN, overheated or frozen tank, partial power on an on/off
  element, a breached connection limit, a plan dearer than the baseline.
- The pipeline falls back on a crashing or lying planner, reports an unreachable comfort band,
  and refuses when even the fallback is unsafe.
- Daylight-saving days of 23 and 25 hours map windows to the right slots.

## Limits (honest)

- **Every appliance figure is an assumption**, not a measurement of any product.
- **Hourly slots.** A two-hour wash is two averaged hours; real programmes vary within the hour.
- **Planned separately from the EV and battery.** The base load only enters through the
  connection limit; a battery could shift appliance energy further, and that is not modelled.
- **One-node tank.** No stratification, no weekly high-temperature (legionella) cycle, no
  user-specific draw; the draw is a fixed daily shape scaled to the household figure.
- **Little flexibility in a small band.** A 200 l tank with a 15 K band stores about 3.5 kWh, so
  heating can move by an hour or two, not by half a day. The numbers show that honestly.
- **Prices are assumed known for the whole day**, as in the EV backtest; the publication time of
  the next day's prices is unverified.
- **Windows are the user's.** Nothing here learns when a machine is actually loaded.
- No device control: the output is a plan, never a command to a machine.
