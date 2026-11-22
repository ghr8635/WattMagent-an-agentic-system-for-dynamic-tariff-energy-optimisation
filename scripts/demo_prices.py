"""Write SYNTHETIC day-ahead prices so the web page can be tried without live data.

    python scripts/demo_prices.py data/demo_prices.csv
    WATTMAGENT_PRICE_CSV=data/demo_prices.csv \
    WATTMAGENT_HOUSEHOLD_FILE=config/households/example_battery.toml \
    WATTMAGENT_UI_BANNER="SYNTHETIC DEMO DATA, not real prices" \
    WATTMAGENT_API_KEYS=<a long key> uvicorn wattmagent.api.app:create_app --factory

The curve is invented: a cheap night, a morning and an evening peak, and a midday dip. It
covers yesterday, today and tomorrow in the local time zone, so every view has prices.
"""

from __future__ import annotations

import argparse
import math
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def synthetic_eur_mwh(local_hour: int, day_offset: int) -> float:
    morning = 55 * math.exp(-(((local_hour - 8) / 2.0) ** 2))
    evening = 85 * math.exp(-(((local_hour - 19) / 2.2) ** 2))
    midday_dip = -35 * math.exp(-(((local_hour - 13) / 2.5) ** 2))
    wobble = 6 * math.sin(day_offset * 1.7 + local_hour / 3)
    return round(70 + morning + evening + midday_dip + wobble, 2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--tz", default="Europe/Berlin")
    args = ap.parse_args()
    tz = ZoneInfo(args.tz)
    today = datetime.now(tz).date()
    start = datetime.combine(today - timedelta(days=1), time(0), tzinfo=tz).astimezone(UTC)
    rows = ["timestamp,price_eur_mwh"]
    for i in range(24 * 3 + 2):
        t = start + timedelta(hours=i)
        local = t.astimezone(tz)
        offset = (local.date() - today).days
        rows.append(f"{t.isoformat()},{synthetic_eur_mwh(local.hour, offset)}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote {len(rows) - 1} synthetic hourly prices to {args.out}")


if __name__ == "__main__":
    main()
