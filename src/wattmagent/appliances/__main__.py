"""CLI: ``python -m wattmagent.appliances --household config/households/example.toml --csv ...``."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from wattmagent.appliances.backtest import (
    default_planners,
    format_summary,
    run_appliance_backtest,
)
from wattmagent.appliances.household import load_household
from wattmagent.data.prices import load_csv, synthetic_prices
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
    p.add_argument("--tariff", type=Path, help="TOML tariff; without it spot prices are used")
    p.add_argument("--audit-log", type=Path, help="write one JSON line per optimiser decision")
    args = p.parse_args()

    household = load_household(args.household)
    tz = ZoneInfo(household.timezone)
    if args.csv:
        prices = load_csv(args.csv)
        label = f"price file: {args.csv.name} (provenance is up to you; see docs/data.md)"
    else:
        prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=tz), args.days, args.seed)
        label = f"SYNTHETIC prices (seed={args.seed}), not market data"
        tz = ZoneInfo(household.timezone)

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
    print(
        f"# {len(household.cycles)} cycle appliances, hot-water tank: "
        f"{household.tank.key if household.tank else 'none'}, "
        f"connection limit: {household.max_grid_kw or 'none'} kW"
    )
    print(f"# cost basis: {basis}; cost = appliance energy only, base load excluded")
    print(
        "# appliance figures are ILLUSTRATIVE ASSUMPTIONS; hot-water use and base load are "
        "SYNTHETIC; day-ahead prices are assumed known for the whole day"
    )
    if banner:
        print(banner)
    audit = InMemoryAuditLog()
    results = run_appliance_backtest(prices, household, default_planners(audit), tz=tz)
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
