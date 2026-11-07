"""Weather feed parsing on the real recorded Open-Meteo reply (Berlin, 2025-01-01..03)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from wattmagent.data.smard_live import PriceFeedError
from wattmagent.data.weather import (
    WeatherFeedError,
    archive_url,
    fetch_archive,
    parse_archive,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "open_meteo_archive_berlin.json"


def real() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data


def test_real_sample_parses_in_order_with_the_right_values() -> None:
    pts = parse_archive(real())
    assert len(pts) == 72
    assert pts[0].start == datetime(2025, 1, 1, 0, tzinfo=UTC)
    assert pts[0].temperature_c == 0.8 and pts[0].shortwave_w_m2 == 0.0
    noon = pts[12]  # 2025-01-01 12:00 UTC
    assert noon.temperature_c == 6.3 and noon.shortwave_w_m2 == 136.0
    assert max(p.shortwave_w_m2 for p in pts) == 224.0
    assert min(p.temperature_c for p in pts) == -1.5
    assert all(p.shortwave_w_m2 == 0 for p in pts if p.start.hour in (0, 1, 2, 3, 22, 23))


def test_nulls_are_skipped_not_filled() -> None:
    doc = real()
    doc["hourly"]["temperature_2m"][5] = None
    pts = parse_archive(doc)
    assert len(pts) == 71
    assert datetime(2025, 1, 1, 5, tzinfo=UTC) not in {p.start for p in pts}


def mutate(path: tuple[str, ...], value: object) -> dict[str, Any]:
    doc = copy.deepcopy(real())
    node: Any = doc
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return doc


@pytest.mark.parametrize(
    "doc",
    [
        mutate(("utc_offset_seconds",), 3600),
        mutate(("hourly_units",), None),
        mutate(("hourly_units", "temperature_2m"), "°F"),
        mutate(("hourly_units", "shortwave_radiation"), "kW/m²"),
        mutate(("hourly",), None),
        mutate(("hourly", "time"), "nope"),
        mutate(("hourly", "temperature_2m"), [1.0]),
        mutate(("hourly", "time"), ["2025-01-01 00:00", *real()["hourly"]["time"][1:]]),
        mutate(
            ("hourly", "time"),
            [*real()["hourly"]["time"][:5], "2025-01-01T09:00", *real()["hourly"]["time"][6:]],
        ),
        mutate(("hourly", "temperature_2m"), ["warm", *real()["hourly"]["temperature_2m"][1:]]),
        mutate(("hourly", "temperature_2m"), [True, *real()["hourly"]["temperature_2m"][1:]]),
        mutate(("hourly", "temperature_2m"), [99.0, *real()["hourly"]["temperature_2m"][1:]]),
        mutate(
            ("hourly", "shortwave_radiation"), [-5.0, *real()["hourly"]["shortwave_radiation"][1:]]
        ),
        mutate(
            ("hourly", "shortwave_radiation"),
            [9000.0, *real()["hourly"]["shortwave_radiation"][1:]],
        ),
        mutate(("hourly", "temperature_2m"), [None] * 72),
    ],
)
def test_unexpected_replies_are_errors(doc: dict[str, Any]) -> None:
    with pytest.raises(WeatherFeedError):
        parse_archive(doc)


def test_url_matches_the_observed_request() -> None:
    url = archive_url(52.52, 13.41, date(2025, 1, 1), date(2025, 1, 3))
    assert url == (
        "https://archive-api.open-meteo.com/v1/archive?latitude=52.5200&longitude=13.4100"
        "&start_date=2025-01-01&end_date=2025-01-03"
        "&hourly=temperature_2m,shortwave_radiation&timezone=UTC"
    )


def test_bad_coordinates_and_dates_are_refused() -> None:
    with pytest.raises(ValueError, match="latitude"):
        archive_url(95, 0, date(2025, 1, 1), date(2025, 1, 2))
    with pytest.raises(ValueError, match="end date"):
        archive_url(52, 13, date(2025, 1, 3), date(2025, 1, 1))


def test_fetch_uses_the_url_and_maps_transport_errors() -> None:
    seen: list[str] = []

    def ok(url: str) -> dict[str, Any]:
        seen.append(url)
        return real()

    pts = fetch_archive(ok, 52.52, 13.41, date(2025, 1, 1), date(2025, 1, 3))
    assert len(pts) == 72 and seen[0].startswith("https://archive-api.open-meteo.com/")

    def down(url: str) -> dict[str, Any]:
        raise PriceFeedError(f"{url}: ConnectError")

    with pytest.raises(WeatherFeedError, match="ConnectError"):
        fetch_archive(down, 52.52, 13.41, date(2025, 1, 1), date(2025, 1, 3))
