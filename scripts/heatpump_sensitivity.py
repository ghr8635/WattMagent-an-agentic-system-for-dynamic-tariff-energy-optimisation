"""How much does the heat-pump result depend on the building's thermal mass?

Runs the appliance backtest for several values of the building's heat capacity and prints one
table. Defaults to SYNTHETIC prices and weather; give --csv and --weather-json for real data.
Every figure is a model output under the stated assumptions, not a household saving.

    python scripts/heatpump_sensitivity.py --days 60 --seed 0
"""

from __future__ import annotations

import argparse
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.household import Household, parse_household
from wattmagent.data.prices import PricePoint, load_csv, synthetic_prices
from wattmagent.data.weather import parse_archive, synthetic_weather, temperature_by_hour

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--household", type=Path, default=ROOT / "config/households/example_heat_pump.toml"
    )
    p.add_argument("--csv", type=Path)
    p.add_argument("--weather-json", type=Path)
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--capacities", type=float, nargs="+", default=[3.0, 5.0, 10.0, 20.0])
    args = p.parse_args()

    raw = tomllib.loads(args.household.read_text(encoding="utf-8"))
    tz = ZoneInfo(str(raw.get("timezone", "Europe/Berlin")))
    prices: list[PricePoint]
    if args.csv and args.weather_json:
        prices = load_csv(args.csv)
        points = parse_archive(json.loads(args.weather_json.read_text(encoding="utf-8")))
        label = f"real prices {args.csv.name}, weather {args.weather_json.name} (Open-Meteo)"
    else:
        prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=tz), args.days, args.seed)
        begin = prices[0].start.astimezone(UTC) - timedelta(days=1)
        points = synthetic_weather(begin, args.days + 3, args.seed)
        label = f"SYNTHETIC prices and weather (seed={args.seed}), not market or weather data"
    outdoor = temperature_by_hour(points)

    print(f"# {label}")
    print("# heat pump only matters on heating days (daily mean outdoor <= 15 C)")
    print("# thermal mass is an ASSUMPTION; spot prices; cost = appliance energy only")
    print("# columns show the HEAT PUMP'S OWN cost per day: household with pump minus the same")
    print("# household without it, so cycle and tank savings do not flatter the pump")
    print(
        f"{'C kWh/K':>8}{'immediate':>11}{'thrifty':>9}{'optimiser':>11}"
        f"{'vs imm':>8}{'vs thrifty':>12}"
    )
    names = ("run_immediately", "cheapest_start", "milp_optimiser")

    def costs(household: Household) -> list[float]:
        res = {
            r.name: r.mean_cost_eur
            for r in run_appliance_backtest(
                prices, household, default_planners(), tz=tz, outdoor=outdoor
            )
        }
        return [res[k] for k in names]

    without = costs(parse_household({k: v for k, v in raw.items() if k != "heatpump"}))
    for cap in args.capacities:
        table = dict(raw["heatpump"])
        table["heat_capacity_kwh_per_k"] = cap
        with_pump = costs(parse_household({**raw, "heatpump": table}))
        imm, thr, opt = (a - b for a, b in zip(with_pump, without, strict=True))
        print(
            f"{cap:>8.1f}{imm:>11.3f}{thr:>9.3f}{opt:>11.3f}"
            f"{(opt / imm - 1) * 100:>+7.1f}%{(opt / thr - 1) * 100:>+11.1f}%"
        )


if __name__ == "__main__":
    main()
