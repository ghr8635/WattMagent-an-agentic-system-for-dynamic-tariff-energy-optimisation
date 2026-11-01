"""Live price feed: parsing, merging and the caching provider, all on recorded data."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from wattmagent.api.service import LiveContextProvider, ServiceUnavailable
from wattmagent.data.smard_live import (
    PriceFeedError,
    fetch_prices,
    index_url,
    parse_index,
    parse_week,
    week_url,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "smard_hour_week.json"
WEEK = 1792969200000  # Monday 2026-10-26 00:00 German time (real feed value)
HOUR = 3_600_000


def real_week() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def week_doc(
    start_ms: int, prices: list[float | None], created_ms: int = 1793451788924
) -> dict[str, Any]:
    return {
        "meta_data": {"version": 1, "created": created_ms},
        "series": [[start_ms + i * HOUR, p] for i, p in enumerate(prices)],
    }


class FakeFeed:
    """Serves an index and weekly files; counts calls; can be told to fail."""

    def __init__(self, files: dict[int, dict[str, Any]]) -> None:
        self.files = files
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if self.fail:
            raise PriceFeedError("feed down")
        if url == index_url():
            return {"timestamps": sorted(self.files)}
        for start, doc in self.files.items():
            if url == week_url(start):
                return doc
        raise PriceFeedError(f"{url}: HTTPStatusError")


# ------------------------------------------------------------------------ parsing


def test_real_sample_parses_to_eur_per_kwh() -> None:
    points, created = parse_week(real_week())
    assert len(points) == 48
    assert points[0].start == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
    assert points[0].eur_per_kwh == pytest.approx(0.02388)  # 23.88 EUR/MWh
    assert points[-1].start - points[0].start == timedelta(hours=47)
    assert created == datetime(2026, 10, 31, 13, 3, 8, 924000, tzinfo=UTC)


def test_null_prices_are_skipped_not_filled() -> None:
    points, _ = parse_week(week_doc(WEEK, [10.0, None, 30.0]))
    assert [p.start.hour for p in points] == [23, 1]  # 23:00Z and 01:00Z; 00:00Z is missing


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"series": "x"},
        {"series": [[WEEK]]},
        {"series": [[WEEK + 1, 1.0]]},  # not on a full hour
        {"series": [[WEEK + HOUR, 1.0], [WEEK, 2.0]]},  # going backwards
        {"series": [[WEEK, True]]},
        {"series": [[WEEK, "12.5"]]},
        {"series": [[WEEK, math.nan]]},
        {"series": [[True, 1.0]]},
    ],
)
def test_unexpected_week_files_are_errors(doc: dict[str, Any]) -> None:
    with pytest.raises(PriceFeedError):
        parse_week(doc)


def test_missing_created_is_allowed() -> None:
    points, created = parse_week({"series": [[WEEK, 5.0]]})
    assert len(points) == 1 and created is None


@pytest.mark.parametrize(
    "doc", [{}, {"timestamps": []}, {"timestamps": [3, 2]}, {"timestamps": ["1"]}]
)
def test_bad_index_is_an_error(doc: dict[str, Any]) -> None:
    with pytest.raises(PriceFeedError):
        parse_index(doc)


def test_urls_match_the_observed_layout() -> None:
    assert index_url() == "https://www.smard.de/app/chart_data/4169/DE/index_hour.json"
    assert week_url(WEEK).endswith("/4169/DE/4169_DE_hour_1792969200000.json")


# ------------------------------------------------------------------------ fetching


def test_fetch_merges_the_newest_weeks_in_order() -> None:
    next_week = WEEK + 168 * HOUR
    feed = FakeFeed(
        {
            WEEK - 168 * HOUR: week_doc(WEEK - 168 * HOUR, [1.0]),
            WEEK: week_doc(WEEK, [10.0, 20.0]),
            next_week: week_doc(next_week, [30.0], created_ms=1793500000000),
        }
    )
    now = datetime(2026, 11, 1, 22, 0, tzinfo=UTC)
    got = fetch_prices(feed, weeks=2, clock=lambda: now)
    assert [p.eur_per_kwh for p in got.points] == [0.01, 0.02, 0.03]
    assert got.fetched_at == now
    assert got.feed_created == datetime.fromtimestamp(1793500000000 / 1000, UTC)
    assert week_url(WEEK - 168 * HOUR) not in feed.calls  # only the newest two weeks


def test_one_failing_week_is_skipped_but_prices_remain() -> None:
    feed = FakeFeed({WEEK: week_doc(WEEK, [10.0])})
    feed.files[WEEK + 168 * HOUR] = {"series": "broken"}
    got = fetch_prices(feed, weeks=2)
    assert len(got.points) == 1


def test_nothing_readable_is_an_error() -> None:
    feed = FakeFeed({WEEK: {"series": "broken"}})
    with pytest.raises(PriceFeedError, match="no prices"):
        fetch_prices(feed, weeks=1)
    feed.fail = True
    with pytest.raises(PriceFeedError):
        fetch_prices(feed)


def test_weeks_must_be_positive() -> None:
    with pytest.raises(ValueError):
        fetch_prices(FakeFeed({}), weeks=0)


# ----------------------------------------------------------------------- provider


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 1, 22, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def provider(feed: FakeFeed, clock: Clock, refresh_s: float = 900) -> LiveContextProvider:
    return LiveContextProvider(feed, ZoneInfo("Europe/Berlin"), refresh_s=refresh_s, clock=clock)


def test_provider_caches_between_refreshes_and_records_fetch_time() -> None:
    feed, clock = FakeFeed({WEEK: week_doc(WEEK, [10.0])}), Clock()
    p = provider(feed, clock)
    first = p.get()
    calls = len(feed.calls)
    clock.now += timedelta(minutes=5)
    assert p.get() is first and len(feed.calls) == calls  # no new network use
    clock.now += timedelta(minutes=11)
    second = p.get()
    assert second is not first and second.fetched_at == clock.now


def test_provider_keeps_old_prices_with_old_fetch_time_when_refresh_fails() -> None:
    feed, clock = FakeFeed({WEEK: week_doc(WEEK, [10.0])}), Clock()
    p = provider(feed, clock)
    first = p.get()
    feed.fail = True
    clock.now += timedelta(hours=1)
    again = p.get()
    assert again is first and again.fetched_at < clock.now  # age stays visible to the check
    calls = len(feed.calls)
    clock.now += timedelta(minutes=1)
    p.get()
    assert len(feed.calls) == calls  # does not hammer a failing feed


def test_provider_without_any_prices_is_service_unavailable() -> None:
    feed = FakeFeed({})
    feed.fail = True
    with pytest.raises(ServiceUnavailable):
        provider(feed, Clock()).get()
