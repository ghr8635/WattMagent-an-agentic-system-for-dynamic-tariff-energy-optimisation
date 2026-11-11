"""What is perfect foresight of the sun worth?

Replays a household with solar over many days three ways per forecast method: the planner sees
the true sunshine (perfect foresight, as in every other result here), or only a forecast, while
the bill and the safety check always use the true sunshine. The difference in the optimiser's
cost is what the forecast error costs. Defaults to SYNTHETIC prices and weather; give --csv and
--weather-json for real data. Forecasts are deliberately simple (built from past days only);
the real numerical-weather forecasts a product would use are not tested here. Prices stay
known in advance, and outdoor temperature is not forecast. A model output, not a household saving.

    python scripts/forecast_value.py --days 120 --seed 7
"""

from __future__ import annotations

import argparse
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.household import load_household
from wattmagent.data.forecast import forecast_skill, mean_of_last_days, persistence
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
        "--household", type=Path, default=ROOT / "config/households/example_detailed.toml"
    )
    p.add_argument("--csv", type=Path)
    p.add_argument("--weather-json", type=Path)
    p.add_argument("--days", type=int, default=120)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--start", default="2025-01-01")
    args = p.parse_args()

    household = load_household(args.household)
    raw = tomllib.loads(args.household.read_text(encoding="utf-8"))
    tz = ZoneInfo(str(raw.get("timezone", "Europe/Berlin")))
    if household.solar is None:
        raise SystemExit("this household has no solar: there is no sun to forecast")
    prices: list[PricePoint]
    if args.csv and args.weather_json:
        prices = load_csv(args.csv)
        points = parse_archive(json.loads(args.weather_json.read_text(encoding="utf-8")))
        label = f"real prices {args.csv.name}, weather {args.weather_json.name} (Open-Meteo)"
    else:
        first = datetime.fromisoformat(args.start).replace(tzinfo=tz)
        prices = synthetic_prices(first, args.days, args.seed)
        begin = prices[0].start.astimezone(UTC) - timedelta(days=4)
        points = synthetic_weather(begin, args.days + 8, args.seed)
        label = f"SYNTHETIC prices and weather (seed={args.seed}), not market or weather data"
    outdoor, radiation = temperature_by_hour(points), radiation_by_hour(points)

    methods = {
        "persistence (yesterday)": persistence(radiation, 24),
        "mean of last 3 days": mean_of_last_days(radiation, 3),
        "blind (assumes no sun)": {t: 0.0 for t in radiation},
    }
    planners = default_planners(with_battery=household.battery is not None)

    print(f"# {label}")
    print(f"# household: {household.name}; cost = whole net grid bill, spot prices only")
    print("# planner sees a sun forecast; bill and safety check use the true sun; prices known")
    print("# 'regret' = optimiser cost with the forecast minus with perfect foresight, same days")
    print(
        f"\n{'forecast':<26}{'MAE W/m2':>9}{'bias':>7}{'days':>6}"
        f"{'optimiser':>11}{'perfect':>9}{'regret':>8}{'regret %':>9}{'valid':>9}"
    )
    for name, forecast in methods.items():
        oracle = {t: radiation[t] for t in forecast if t in radiation}
        kwargs = {"tz": tz, "outdoor": outdoor, "radiation": radiation}
        perfect = {
            r.name: r
            for r in run_appliance_backtest(
                prices, household, planners, radiation_forecast=oracle, **kwargs
            )
        }
        seen = {
            r.name: r
            for r in run_appliance_backtest(
                prices, household, planners, radiation_forecast=forecast, **kwargs
            )
        }
        skill = forecast_skill(radiation, forecast)
        opt, ref = seen["milp_optimiser"], perfect["milp_optimiser"]
        regret = opt.mean_cost_eur - ref.mean_cost_eur
        print(
            f"{name:<26}{skill.mae:>9.0f}{skill.bias:>7.0f}{opt.days:>6}"
            f"{opt.mean_cost_eur:>11.4f}{ref.mean_cost_eur:>9.4f}{regret:>8.4f}"
            f"{regret / ref.mean_cost_eur * 100:>8.1f}%{opt.days_valid:>5}/{opt.days:<3}"
        )
        imm = seen["run_immediately"].mean_cost_eur
        cheap = seen["cheapest_start"]
        print(
            f"{'':<26}  run_immediately {imm:.4f}; cheapest_start {cheap.mean_cost_eur:.4f}; "
            f"optimiser {opt.mean_cost_eur / imm * 100 - 100:+.1f}% vs run_immediately"
        )


if __name__ == "__main__":
    main()
