"""Price data freshness: stale, missing or implausible prices must not be planned on."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from wattmagent.data.prices import PricePoint

DEFAULT_MAX_AGE = timedelta(hours=36)
MAX_ABS_EUR_PER_KWH = 3.0  # catches EUR/MWh vs EUR/kWh unit mistakes (real range is ~ -0.5..1)
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    """Prices as fetched, with the moment they were fetched (timezone-aware)."""

    points: Sequence[PricePoint]
    fetched_at: datetime


def check_price_snapshot(
    snapshot: PriceSnapshot,
    *,
    now: datetime,
    window_start: datetime,
    slots: int,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> list[str]:
    """Return the reasons the snapshot cannot be used; an empty list means it is fine."""
    for name, value in (
        ("now", now),
        ("window_start", window_start),
        ("fetched_at", snapshot.fetched_at),
    ):
        if value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
    if not snapshot.points:
        return ["no_prices: the price feed returned nothing"]

    reasons: list[str] = []
    age = now - snapshot.fetched_at
    if age < -_CLOCK_SKEW:
        reasons.append("fetched_in_future: clock or timestamp problem")
    elif age > max_age:
        hours = age.total_seconds() / 3600
        reasons.append(
            f"stale_prices: fetched {hours:.1f} h ago, limit {max_age.total_seconds() / 3600:g} h"
        )

    have = {p.start for p in snapshot.points}
    missing = [
        window_start + timedelta(hours=i)
        for i in range(slots)
        if window_start + timedelta(hours=i) not in have
    ]
    if missing:
        reasons.append(
            f"missing_prices: {len(missing)} of {slots} slots, first at {missing[0].isoformat()}"
        )

    if any(
        not math.isfinite(p.eur_per_kwh) or abs(p.eur_per_kwh) > MAX_ABS_EUR_PER_KWH
        for p in snapshot.points
    ):
        reasons.append("implausible_price: non-finite or beyond +/-3 EUR/kWh (unit mistake?)")
    return reasons
