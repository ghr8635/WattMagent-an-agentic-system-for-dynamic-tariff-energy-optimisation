"""Electricity price data: types, a seeded synthetic generator, and a CSV loader.

The synthetic generator exists for tests and development only. Anything produced
from it must be labelled SYNTHETIC. Real evaluations use ``load_csv`` on data you
downloaded from a market-data source whose terms you have checked.

Design decision: the core works on an hourly grid. Sub-hourly input (for example
15-minute day-ahead products) is averaged to hourly by ``load_csv``.
"""

from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PricePoint:
    start: datetime  # start of the hour, in the series' own timezone convention
    eur_per_kwh: float


def synthetic_prices(start: datetime, days: int, seed: int) -> list[PricePoint]:
    """Plausible hourly prices: morning and evening peaks, cheap nights, noise.

    Deterministic for a given (start, days, seed). NOT real market data.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    rng = random.Random(seed)  # noqa: S311  (simulation, not crypto)
    base = 0.12  # EUR/kWh
    out: list[PricePoint] = []
    for i in range(days * 24):
        t = start + timedelta(hours=i)
        h = t.hour
        shape = (
            0.05 * math.exp(-((h - 8) ** 2) / 4.0)  # morning peak
            + 0.08 * math.exp(-((h - 19) ** 2) / 5.0)  # evening peak
            - 0.04 * math.exp(-((h - 3) ** 2) / 8.0)  # night trough
        )
        weekend = -0.02 if t.weekday() >= 5 else 0.0
        price = base + shape + weekend + rng.gauss(0.0, 0.015)
        out.append(PricePoint(t, round(max(price, -0.05), 5)))
    return out


def _floor_hour(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def load_csv(path: Path) -> list[PricePoint]:
    """Load ``timestamp,price_eur_mwh`` rows (ISO 8601 timestamps).

    Converts EUR/MWh to EUR/kWh. Sub-hourly rows are averaged per hour; every
    hour must contain the same number of rows, so partial hours fail loudly
    instead of silently skewing the average. The result must be a gap-free hourly
    series.
    """
    buckets: dict[datetime, list[float]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not {"timestamp", "price_eur_mwh"} <= set(reader.fieldnames):
            raise ValueError("CSV needs columns: timestamp, price_eur_mwh")
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp"].strip())
            buckets[_floor_hour(ts)].append(float(row["price_eur_mwh"]) / 1000.0)
    if not buckets:
        raise ValueError("no rows in CSV")

    counts = {len(v) for v in buckets.values()}
    if len(counts) != 1:
        raise ValueError(f"irregular number of rows per hour: {sorted(counts)}")

    hours = sorted(buckets)
    for prev, cur in zip(hours, hours[1:], strict=False):
        if cur - prev != timedelta(hours=1):
            raise ValueError(f"gap or duplicate between {prev} and {cur}")
    return [PricePoint(h, sum(buckets[h]) / len(buckets[h])) for h in hours]
