"""CLI: ``python -m wattmagent.appliances --household config/households/example.toml --csv ...``."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.appliances.backtest import (
    default_planners,
    format_summary,
    run_appliance_backtest,
)
from wattmagent.appliances.household import load_household
from wattmagent.data.prices import load_csv, synthetic_prices
from wattmagent.data.smard_live import HttpxFetch
from wattmagent.data.weather import (
    fetch_archive,
    parse_archive,
    radiation_by_hour,
    solar_yield_kwh_per_kwp,
    synthetic_weather,
    temperature_by_hour,
)
from wattmagent.safety.audit import InMemoryAuditLog, JsonlAuditLog
from wattmagent.tariff.tariff import apply_tariff, load_tariff


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest appliance scheduling on a price series")
    p.add_argument("--household", type=Path, required=True, help="household TOML file")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="CSV with columns timestamp,price_eur_mwh")
    src.add_argument("--synthetic", action="store_true", help="seeded synthetic prices (dev only)")
    p.add_argument("--days", type=int, default=60, help="synthetic only")
    p.add_argument("--seed", type=int, default=0, help="synthetic only")
    p.add_argument("--start", type=str, default="2025-01-01", help="synthetic only: first day")
    p.add_argument("--tariff", type=Path, help="TOML tariff; without it spot prices are used")
    wx = p.add_mutually_exclusive_group()
    wx.add_argument(
        "--weather-json", type=Path, help="saved Open-Meteo archive reply (see docs/data.md)"
    )
    wx.add_argument(
        "--latitude", type=float, help="fetch Open-Meteo archive weather (needs --longitude)"
    )
    p.add_argument("--longitude", type=float)
    p.add_argument("--audit-log", type=Path, help="write one JSON line per optimiser decision")
    args = p.parse_args()

    household = load_household(args.household)
    tz = ZoneInfo(household.timezone)
    if args.csv:
        prices = load_csv(args.csv)
        label = f"price file: {args.csv.name} (provenance is up to you; see docs/data.md)"
    else:
        prices = synthetic_prices(
            datetime.fromisoformat(args.start).replace(tzinfo=tz), args.days, args.seed
        )
        label = f"SYNTHETIC prices (seed={args.seed}), not market data"
        tz = ZoneInfo(household.timezone)

    outdoor = None
    radiation = None
    weather_label = ""
    yield_line = ""
    if household.heatpump is not None or household.solar is not None:
        if args.weather_json:
            points = parse_archive(json.loads(args.weather_json.read_text(encoding="utf-8")))
            weather_label = f"weather file: {args.weather_json.name} (Open-Meteo, CC BY 4.0)"
        elif args.latitude is not None and args.longitude is not None:
            first = prices[0].start.astimezone(UTC).date()
            last = prices[-1].start.astimezone(UTC).date()
            points = fetch_archive(HttpxFetch(), args.latitude, args.longitude, first, last)
            weather_label = "weather: Open-Meteo archive (CC BY 4.0), fetched now"
        elif args.synthetic:
            begin = prices[0].start.astimezone(UTC) - timedelta(days=1)  # margin for time zones
            points = synthetic_weather(begin, args.days + 3, args.seed)
            weather_label = f"SYNTHETIC weather (seed={args.seed}), not measurements"
        else:
            p.error(
                "this household has a heat pump or solar: "
                "give --weather-json or --latitude/--longitude"
            )
        outdoor = temperature_by_hour(points)
        radiation = radiation_by_hour(points)
        if household.solar is not None:
            first_hour = prices[0].start.astimezone(UTC)
            last_hour = prices[-1].start.astimezone(UTC)
            used = [pt for pt in points if first_hour <= pt.start <= last_hour]
            per_kwp = solar_yield_kwh_per_kwp(used, household.solar.performance_ratio)
            days = max(1, len(used)) / 24
            sol = household.solar
            yield_line = (
                f"# solar: {sol.kwp:g} kWp, performance ratio {sol.performance_ratio:g}, "
                f"export {sol.export_price_eur_kwh:g} EUR/kWh (ASSUMED); model yield "
                f"{per_kwp:.0f} kWh/kWp over {days:.0f} days (compare with known yields only "
                "over a full year)"
            )

    banner = ""
    if args.tariff:
        tariff = load_tariff(args.tariff)
        prices = apply_tariff(prices, tariff, tz)
        basis = f"all-in household price, tariff '{tariff.name}'"
        if tariff.illustrative:
            banner = "# *** ILLUSTRATIVE TARIFF: euro figures are NOT real ***"
    else:
        basis = "spot (wholesale) energy price only: no grid fees, taxes or margin"

    print(f"# {label}\n# household: {household.name}, time zone {household.timezone}")
    catch_all = (
        f" + {household.daily_base_load_kwh:g} kWh/day catch-all"
        if household.daily_base_load_kwh
        else ""
    )
    print(
        f"# {len(household.cycles)} cycle appliances, hot-water tank: "
        f"{household.tank.key if household.tank else 'none'}, "
        f"heat pump: {household.heatpump.key if household.heatpump else 'none'}, "
        f"solar: {f'{household.solar.kwp:g} kWp' if household.solar else 'none'}, "
        f"battery: {f'{household.battery.capacity_kwh:g} kWh' if household.battery else 'none'}, "
        f"fixed loads: {len(household.fixed)} listed ({household.fixed_daily_kwh:.1f} kWh/day)"
        f"{catch_all}, "
        f"connection limit: {household.max_grid_kw or 'none'} kW"
    )
    if weather_label:
        print(f"# {weather_label}")
    if yield_line:
        print(yield_line)
    if household.solar or household.battery:
        parts = ["whole net grid bill: base load included"]
        if household.solar:
            parts.append("own solar used first, surplus exported")
        if household.battery:
            parts.append(
                f"battery wear counted at {household.battery.wear_eur_per_kwh:g} EUR/kWh "
                "discharged (ASSUMED)"
            )
        scope = ", ".join(parts)
    else:
        scope = "appliance energy only, base load excluded"
    print(f"# cost basis: {basis}; cost = {scope}")
    print(
        "# appliance figures are ILLUSTRATIVE ASSUMPTIONS; hot-water use and base load are "
        "SYNTHETIC; day-ahead prices are assumed known for the whole day"
    )
    if banner:
        print(banner)
    audit = InMemoryAuditLog()
    results = run_appliance_backtest(
        prices,
        household,
        default_planners(audit, with_battery=household.battery is not None),
        tz=tz,
        outdoor=outdoor,
        radiation=radiation,
    )
    print(format_summary(results, baseline="run_immediately"))
    fallbacks = [e for e in audit.entries if e["decision"] == "fallback"]
    reasons = Counter(r.split(":")[0] for e in fallbacks for r in e["reasons"])
    detail = ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())) or "none"
    print(
        f"# safety: {len(audit.entries)} optimiser plans verified, "
        f"{len(fallbacks)} fallbacks ({detail})"
    )
    if args.audit_log:
        sink = JsonlAuditLog(args.audit_log)
        for entry in audit.entries:
            sink.record(entry)
        print(f"# audit log: {len(audit.entries)} decisions written to {args.audit_log}")


if __name__ == "__main__":
    main()
