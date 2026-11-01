"""Live day-ahead prices from SMARD's chart-data feed (Bundesnetzagentur | SMARD.de, CC BY 4.0).

Layout of the feed, as observed on 2026-11-01 (see docs/data.md; not an official API
contract, so everything it returns is validated and a surprise is an error, not a guess):

- ``.../4169/DE/index_hour.json``: ``{"timestamps": [ms, ...]}``, the start (Monday
  00:00 German time) of every week for which a file exists, oldest first. The last entry
  can be the coming Monday, so tomorrow's prices can sit in a file that has just begun.
- ``.../4169/DE/4169_DE_hour_<ms>.json``: ``{"meta_data": {"created": ms, ...},
  "series": [[ms, EUR/MWh or null], ...]}``, one row per hour of that week.

Nothing here reads the network except ``HttpxFetch``; everything else takes a ``fetch``
callable, so tests run on recorded data.
"""

from __future__ import annotations

import argparse
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeGuard

from wattmagent.core.errors import WattMagentError
from wattmagent.data.prices import PricePoint

log = logging.getLogger(__name__)

BASE_URL = "https://www.smard.de/app/chart_data"
FILTER_ID = 4169  # day-ahead price, Germany/Luxembourg
REGION = "DE"
RESOLUTION = "hour"
_HOUR_MS = 3_600_000

JsonFetch = Callable[[str], dict[str, Any]]


class PriceFeedError(WattMagentError):
    """The price feed could not be read or returned something unexpected."""


@dataclass(frozen=True, slots=True)
class FetchedPrices:
    points: list[PricePoint]  # hourly, EUR/kWh, sorted, gaps left as gaps
    fetched_at: datetime  # when WE fetched (timezone-aware)
    feed_created: datetime | None  # when SMARD generated the newest file we used


def index_url() -> str:
    return f"{BASE_URL}/{FILTER_ID}/{REGION}/index_{RESOLUTION}.json"


def week_url(week_start_ms: int) -> str:
    return f"{BASE_URL}/{FILTER_ID}/{REGION}/{FILTER_ID}_{REGION}_{RESOLUTION}_{week_start_ms}.json"


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_index(doc: dict[str, Any]) -> list[int]:
    stamps = doc.get("timestamps")
    if not isinstance(stamps, list) or not stamps or not all(_is_int(s) for s in stamps):
        raise PriceFeedError("index: 'timestamps' must be a non-empty list of integers")
    if stamps != sorted(set(stamps)):
        raise PriceFeedError("index: timestamps are not strictly increasing")
    return [int(s) for s in stamps]


def parse_week(doc: dict[str, Any]) -> tuple[list[PricePoint], datetime | None]:
    """Prices of one weekly file in EUR/kWh, plus when SMARD created the file.

    Rows with a missing price (``null``) are skipped, not filled: the freshness check
    downstream treats a missing hour as missing.
    """
    series = doc.get("series")
    if not isinstance(series, list):
        raise PriceFeedError("week file: 'series' must be a list")
    points: list[PricePoint] = []
    previous = -1
    for row in series:
        if not isinstance(row, list) or len(row) != 2:
            raise PriceFeedError("week file: each row must be [timestamp, price]")
        stamp, price = row
        if not _is_int(stamp) or stamp % _HOUR_MS != 0:
            raise PriceFeedError("week file: timestamp is not on a full hour")
        if stamp <= previous:
            raise PriceFeedError("week file: timestamps are not strictly increasing")
        previous = stamp
        if price is None:
            continue
        if (
            isinstance(price, bool)
            or not isinstance(price, int | float)
            or not math.isfinite(price)
        ):
            raise PriceFeedError("week file: price must be a finite number or null")
        points.append(PricePoint(datetime.fromtimestamp(stamp / 1000, UTC), float(price) / 1000))
    meta = doc.get("meta_data")
    created_ms = meta.get("created") if isinstance(meta, dict) else None
    created = datetime.fromtimestamp(created_ms / 1000, UTC) if _is_int(created_ms) else None
    return points, created


def _utcnow() -> datetime:
    return datetime.now(UTC)


def fetch_prices(
    fetch: JsonFetch, *, weeks: int = 2, clock: Callable[[], datetime] = _utcnow
) -> FetchedPrices:
    """Fetch the newest ``weeks`` weekly files and merge them.

    A week that fails is skipped with a warning, as long as at least one price was read:
    a missing hour is caught by the freshness check, so partial data cannot be planned on
    silently. If nothing at all can be read, this raises ``PriceFeedError``.
    """
    if weeks < 1:
        raise ValueError("weeks must be at least 1")
    starts = parse_index(fetch(index_url()))[-weeks:]
    merged: dict[datetime, PricePoint] = {}
    created: datetime | None = None
    errors: list[str] = []
    for start in starts:
        try:
            points, week_created = parse_week(fetch(week_url(start)))
        except PriceFeedError as exc:
            log.warning("price week %s skipped: %s", start, exc)
            errors.append(str(exc))
            continue
        merged.update({p.start: p for p in points})  # later weeks win
        if week_created is not None and (created is None or week_created > created):
            created = week_created
    if not merged:
        detail = errors[0] if errors else "no prices in the files"
        raise PriceFeedError(f"no prices could be read: {detail}")
    return FetchedPrices([merged[k] for k in sorted(merged)], clock(), created)


class HttpxFetch:
    """The real network access. httpx is imported lazily so tests do not need it."""

    def __init__(self, timeout_s: float = 10.0) -> None:
        import httpx

        from wattmagent import __version__

        self._client = httpx.Client(
            timeout=timeout_s, headers={"User-Agent": f"wattmagent/{__version__}"}
        )

    def __call__(self, url: str) -> dict[str, Any]:
        import httpx

        try:
            response = self._client.get(url)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PriceFeedError(f"{url}: {type(exc).__name__}") from exc
        if not isinstance(data, dict):
            raise PriceFeedError(f"{url}: JSON object expected")
        return data


def main() -> None:
    p = argparse.ArgumentParser(description="Fetch live SMARD day-ahead prices and summarise")
    p.add_argument("--weeks", type=int, default=2)
    args = p.parse_args()
    got = fetch_prices(HttpxFetch(), weeks=args.weeks)
    now = datetime.now(UTC)
    ahead = sum(1 for pt in got.points if pt.start >= now)
    print(f"fetched {len(got.points)} hourly prices at {got.fetched_at.isoformat()}")
    print(f"  first: {got.points[0].start.isoformat()}")
    print(f"  last:  {got.points[-1].start.isoformat()}  ({ahead} hours from now onwards)")
    if got.feed_created:
        print(f"  newest file created by SMARD at {got.feed_created.isoformat()}")
    print("Source: Bundesnetzagentur | SMARD.de (CC BY 4.0)")


if __name__ == "__main__":
    main()
