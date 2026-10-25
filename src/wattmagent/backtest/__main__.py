"""CLI: ``python -m wattmagent.backtest --csv prices.csv`` or ``--synthetic``."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.backtest.runner import format_summary, run_backtest
from wattmagent.data.prices import load_csv, synthetic_prices
from wattmagent.devices.ev import EVSpec
from wattmagent.planning.baselines import BASELINES
from wattmagent.tariff.tariff import apply_tariff, load_tariff


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest EV charging strategies on a price series")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="CSV with columns timestamp,price_eur_mwh")
    src.add_argument("--synthetic", action="store_true", help="seeded synthetic prices (dev only)")
    p.add_argument("--days", type=int, default=60, help="synthetic only")
    p.add_argument("--seed", type=int, default=0, help="synthetic only")
    p.add_argument(
        "--tz",
        default="Europe/Berlin",
        help="local time zone for plug-in/departure hours (timezone-aware CSV data only)",
    )
    p.add_argument(
        "--tariff",
        type=Path,
        help="TOML tariff (see config/tariffs/); without it, spot prices are used as-is",
    )
    p.add_argument("--target-soc", type=float, default=0.8)
    p.add_argument("--arrival-soc", type=float, default=0.3)
    args = p.parse_args()

    if args.csv:
        prices = load_csv(args.csv)
        label = f"price file: {args.csv.name} (provenance is up to you; see docs/data.md)"
    else:
        prices = synthetic_prices(datetime(2025, 1, 1), args.days, args.seed)
        label = f"SYNTHETIC prices (seed={args.seed}), not market data"

    spec = EVSpec(target_soc=args.target_soc, arrival_soc=args.arrival_soc)
    tz = ZoneInfo(args.tz) if prices[0].start.tzinfo is not None else None

    banner = ""
    if args.tariff:
        tariff = load_tariff(args.tariff)
        prices = apply_tariff(prices, tariff, tz)
        basis = f"all-in household price, tariff '{tariff.name}'"
        if tariff.illustrative:
            banner = "# *** ILLUSTRATIVE TARIFF: euro figures are NOT real ***\n"
    else:
        basis = "spot (wholesale) energy price only: no grid fees, taxes or margin"

    results = run_backtest(prices, spec, BASELINES, tz=tz)
    clock = f"local time ({args.tz})" if tz else "as stored in the timestamps (naive)"
    print(f"# {label}\n# {spec}\n# plug-in/departure hours: {clock}\n# cost basis: {basis}")
    print(banner)
    print(format_summary(results, baseline="charge_immediately"))


if __name__ == "__main__":
    main()
