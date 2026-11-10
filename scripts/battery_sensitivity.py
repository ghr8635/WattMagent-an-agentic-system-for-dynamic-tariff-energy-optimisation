"""How much does the battery result depend on its wear cost and its size?

Runs the appliance backtest with the battery removed and with it present for several wear
costs (EUR per kWh discharged) and capacities, and prints the battery's own gain per day: the
household without the battery minus the same household with it, for the optimiser and for the
price-blind "self consumption" rule. Defaults to SYNTHETIC prices and weather; give --csv and
--weather-json for real data. A model output under stated assumptions, not a household saving.

    python scripts/battery_sensitivity.py --days 365 --seed 7
"""

from __future__ import annotations

import argparse
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.household import Household, parse_household
from wattmagent.data.prices import PricePoint, load_csv, synthetic_prices
from wattmagent.data.weather import (
    parse_archive,
    radiation_by_hour,
    synthetic_weather,
    temperature_by_hour,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--household", type=Path, default=ROOT / "config/households/example_battery.toml"
    )
    p.add_argument("--csv", type=Path)
    p.add_argument("--weather-json", type=Path)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--start", default="2025-01-01")
    p.add_argument("--wear", type=float, nargs="+", default=[0.0, 0.02, 0.05, 0.10, 0.15, 0.20])
    p.add_argument("--capacities", type=float, nargs="+", default=[5.0, 10.0, 15.0])
    args = p.parse_args()

    raw = tomllib.loads(args.household.read_text(encoding="utf-8"))
    if "battery" not in raw:
        raise SystemExit("this household file has no [battery] table")
    tz = ZoneInfo(str(raw.get("timezone", "Europe/Berlin")))
    prices: list[PricePoint]
    if args.csv and args.weather_json:
        prices = load_csv(args.csv)
        points = parse_archive(json.loads(args.weather_json.read_text(encoding="utf-8")))
        label = f"real prices {args.csv.name}, weather {args.weather_json.name} (Open-Meteo)"
    else:
        first = datetime.fromisoformat(args.start).replace(tzinfo=tz)
        prices = synthetic_prices(first, args.days, args.seed)
        begin = prices[0].start.astimezone(UTC) - timedelta(days=1)
        points = synthetic_weather(begin, args.days + 3, args.seed)
        label = f"SYNTHETIC prices and weather (seed={args.seed}), not market or weather data"
    outdoor, radiation = temperature_by_hour(points), radiation_by_hour(points)

    print(f"# {label}")
    print("# spot prices; cost = whole net grid bill; perfect foresight of prices and weather")
    print("# wear cost and battery size are ASSUMPTIONS; the gain is the battery's own, i.e.")
    print("# household without battery minus the same household with it, EUR per day")

    def costs(household: Household) -> dict[str, float]:
        planners = default_planners(with_battery=household.battery is not None)
        results = run_appliance_backtest(
            prices, household, planners, tz=tz, outdoor=outdoor, radiation=radiation
        )
        return {r.name: r.mean_cost_eur for r in results}

    without = costs(parse_household({k: v for k, v in raw.items() if k != "battery"}))

    def row(table: dict[str, Any]) -> tuple[float, float, float]:
        with_battery = costs(parse_household({**raw, "battery": table}))
        opt = without["milp_optimiser"] - with_battery["milp_optimiser"]
        rule = without["run_immediately"] - with_battery["self_consumption"]
        return opt, rule, with_battery["milp_optimiser"]

    base = dict(raw["battery"])
    print(
        f"\n{'wear EUR/kWh':>13}{'optimiser gain':>16}{'self-cons gain':>16}{'optimiser cost':>16}"
    )
    for wear in args.wear:
        opt, rule, cost = row({**base, "wear_eur_per_kwh": wear})
        print(f"{wear:>13.2f}{opt:>16.4f}{rule:>16.4f}{cost:>16.4f}")
    print(
        f"\n{'capacity kWh':>13}{'optimiser gain':>16}{'self-cons gain':>16}{'optimiser cost':>16}"
    )
    for cap in args.capacities:
        opt, rule, cost = row(
            {
                **base,
                "capacity_kwh": cap,
                "max_charge_kw": cap / 2,
                "max_discharge_kw": cap / 2,
            }
        )
        print(f"{cap:>13.1f}{opt:>16.4f}{rule:>16.4f}{cost:>16.4f}")
    print(f"\n# household without battery: optimiser {without['milp_optimiser']:.4f} EUR/day")
    print("# capacity rows keep power at half the capacity in kW and the wear cost of the file")


if __name__ == "__main__":
    main()
