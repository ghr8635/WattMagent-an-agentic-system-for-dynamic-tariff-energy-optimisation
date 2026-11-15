"""What is hourly replanning worth?

Replays a household with solar and a battery over many days. The optimiser plans on a simple
sun forecast (built from past days only) in four ways, always billed and verified on the true
sun: plan once at midnight; replan every hour but keep the original forecast (this must change
nothing, and is a check); replan every hour with a nowcast that rescales the rest of the day to
the sun seen so far; and, as the ceiling, perfect foresight. 'Regret closed' is the share of the
forecast-error cost that replanning wins back. Defaults to SYNTHETIC prices and weather; give
--csv and --weather-json for real data. Prices stay known in advance. A model output, not a
household saving.

    python scripts/replanning_value.py --days 120 --seed 7
"""

from __future__ import annotations

import argparse
import json
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.appliances.backtest import (
    ApplianceSummary,
    default_planners,
    run_appliance_backtest,
)
from wattmagent.appliances.household import load_household
from wattmagent.data.forecast import mean_of_last_days, persistence
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
    print("# optimiser only; planner sees a sun forecast, bill and safety check use the true sun")
    print("# regret = cost minus perfect foresight; closed = share of regret won back")
    print(
        f"\n{'forecast':<26}{'days':>5}{'perfect':>9}{'once':>9}{'replan':>9}{'+nowcast':>9}"
        f"{'regret':>8}{'closed':>8}{'solves/d':>9}{'failed':>7}{'valid':>9}"
    )
    for name, forecast in methods.items():
        oracle = {t: radiation[t] for t in forecast if t in radiation}

        def run(fc: dict[datetime, float], **extra: bool) -> ApplianceSummary:
            rows = run_appliance_backtest(
                prices,
                household,
                planners,
                radiation_forecast=fc,
                tz=tz,
                outdoor=outdoor,
                radiation=radiation,
                **extra,
            )
            return next(r for r in rows if r.name == "milp_optimiser")

        perfect = run(oracle)
        once = run(forecast)
        plain = run(forecast, replan_hourly=True, nowcast=False)
        now = run(forecast, replan_hourly=True, nowcast=True)
        regret = once.mean_cost_eur - perfect.mean_cost_eur
        won = once.mean_cost_eur - now.mean_cost_eur
        closed = f"{won / regret * 100:.0f}%" if regret > 1e-9 else "n/a"
        print(
            f"{name:<26}{now.days:>5}{perfect.mean_cost_eur:>9.4f}{once.mean_cost_eur:>9.4f}"
            f"{plain.mean_cost_eur:>9.4f}{now.mean_cost_eur:>9.4f}{regret:>8.4f}{closed:>8}"
            f"{now.replan_solves / now.days:>9.1f}{now.replan_failed:>7}"
            f"{now.days_valid:>5}/{now.days:<3}"
        )


if __name__ == "__main__":
    main()
