"""CLI: ``python -m wattmagent.backtest --csv prices.csv`` or ``--synthetic``."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from wattmagent.backtest.runner import format_summary, run_backtest
from wattmagent.data.prices import load_csv, synthetic_prices
from wattmagent.devices.ev import EVSpec
from wattmagent.planning.baselines import BASELINES


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest EV charging strategies on a price series")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", type=Path, help="CSV with columns timestamp,price_eur_mwh")
    src.add_argument("--synthetic", action="store_true", help="seeded synthetic prices (dev only)")
    p.add_argument("--days", type=int, default=60, help="synthetic only")
    p.add_argument("--seed", type=int, default=0, help="synthetic only")
    p.add_argument("--target-soc", type=float, default=0.8)
    p.add_argument("--arrival-soc", type=float, default=0.3)
    args = p.parse_args()

    if args.csv:
        prices = load_csv(args.csv)
        label = f"real data: {args.csv.name}"
    else:
        prices = synthetic_prices(datetime(2025, 1, 1), args.days, args.seed)
        label = f"SYNTHETIC prices (seed={args.seed}), not market data"

    spec = EVSpec(target_soc=args.target_soc, arrival_soc=args.arrival_soc)
    results = run_backtest(prices, spec, BASELINES)
    print(f"# {label}\n# {spec}\n")
    print(format_summary(results, baseline="charge_immediately"))


if __name__ == "__main__":
    main()
