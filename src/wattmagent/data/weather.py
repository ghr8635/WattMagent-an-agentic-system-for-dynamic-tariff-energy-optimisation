"""Hourly outdoor temperature and solar radiation from Open-Meteo's archive API.

Source: Open-Meteo (https://open-meteo.com), data licensed CC BY 4.0, attribution required.
The free tier is for non-commercial use (up to 10,000 calls a day); a commercial product
needs a paid plan or a self-hosted server.

Response layout, as observed on 2026-11-07 (see ``tests/fixtures/open_meteo_archive_berlin.json``
and docs/data.md). Not an official contract here, so everything is validated and a surprise
is an error, not a guess:

- request: ``/v1/archive?latitude=..&longitude=..&start_date=..&end_date=..
  &hourly=temperature_2m,shortwave_radiation&timezone=UTC``
- reply: ``{"utc_offset_seconds": 0, "timezone": "GMT", "hourly_units": {...},
  "hourly": {"time": ["2025-01-01T00:00", ...], "temperature_2m": [...],
  "shortwave_radiation": [...]}}``; timestamps have no zone suffix and are UTC because we
  ask for ``timezone=UTC``; the values are one per hour.

NOT verified: the forecast endpoint (needed for live use, because the archive lags the
present), and how the archive marks missing hours (we accept ``null`` and skip the hour).
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from wattmagent.core.errors import WattMagentError
from wattmagent.data.smard_live import JsonFetch, PriceFeedError

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
_TEMP_RANGE_C = (-60.0, 60.0)
_GHI_MAX_W_M2 = 1500.0  # above the solar constant at the surface: a corrupt value


class WeatherFeedError(WattMagentError):
    """The weather feed could not be read or returned something unexpected."""


@dataclass(frozen=True, slots=True)
class WeatherPoint:
    start: datetime  # start of the hour, UTC
    temperature_c: float  # air temperature at 2 m
    shortwave_w_m2: float  # global horizontal shortwave radiation, hourly mean


def archive_url(latitude: float, longitude: float, start: date, end: date) -> str:
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("latitude must be within +-90 and longitude within +-180")
    if end < start:
        raise ValueError("end date is before start date")
    return (
        f"{ARCHIVE_URL}?latitude={latitude:.4f}&longitude={longitude:.4f}"
        f"&start_date={start.isoformat()}&end_date={end.isoformat()}"
        "&hourly=temperature_2m,shortwave_radiation&timezone=UTC"
    )


def _number(value: object, where: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise WeatherFeedError(f"{where}: {value!r} is not a number")
    return float(value)


def parse_archive(doc: dict[str, Any]) -> list[WeatherPoint]:
    """Hourly points, UTC, in order. Hours with a missing value are skipped, not filled."""
    if doc.get("utc_offset_seconds") != 0:
        raise WeatherFeedError("expected UTC timestamps (utc_offset_seconds == 0)")
    units = doc.get("hourly_units")
    if not isinstance(units, dict):
        raise WeatherFeedError("'hourly_units' missing")
    if units.get("temperature_2m") != "°C" or units.get("shortwave_radiation") != "W/m²":
        raise WeatherFeedError(f"unexpected units: {units!r}")
    hourly = doc.get("hourly")
    if not isinstance(hourly, dict):
        raise WeatherFeedError("'hourly' missing")
    times, temps, ghis = (hourly.get(k) for k in ("time", "temperature_2m", "shortwave_radiation"))
    if not (isinstance(times, list) and isinstance(temps, list) and isinstance(ghis, list)):
        raise WeatherFeedError("hourly arrays missing")
    if not len(times) == len(temps) == len(ghis):
        raise WeatherFeedError("hourly arrays differ in length")

    out: list[WeatherPoint] = []
    previous: datetime | None = None
    for raw, t_raw, g_raw in zip(times, temps, ghis, strict=True):
        try:
            start = datetime.strptime(str(raw), "%Y-%m-%dT%H:%M").replace(tzinfo=UTC)
        except ValueError as exc:
            raise WeatherFeedError(f"bad timestamp {raw!r}") from exc
        if previous is not None and (start - previous).total_seconds() != 3600:
            raise WeatherFeedError(f"timestamps are not consecutive hours at {raw}")
        previous = start
        temp = _number(t_raw, f"temperature at {raw}")
        ghi = _number(g_raw, f"radiation at {raw}")
        if temp is None or ghi is None:
            continue
        if not _TEMP_RANGE_C[0] <= temp <= _TEMP_RANGE_C[1]:
            raise WeatherFeedError(f"implausible temperature {temp} C at {raw}")
        if not 0.0 <= ghi <= _GHI_MAX_W_M2:
            raise WeatherFeedError(f"implausible radiation {ghi} W/m2 at {raw}")
        out.append(WeatherPoint(start, temp, ghi))
    if not out:
        raise WeatherFeedError("no usable hours in the reply")
    return out


def fetch_archive(
    fetch: JsonFetch, latitude: float, longitude: float, start: date, end: date
) -> list[WeatherPoint]:
    url = archive_url(latitude, longitude, start, end)
    try:
        doc = fetch(url)
    except PriceFeedError as exc:  # the shared HTTP fetcher reports in its own error type
        raise WeatherFeedError(str(exc)) from exc
    return parse_archive(doc)


def temperature_by_hour(points: Sequence[WeatherPoint]) -> Mapping[datetime, float]:
    """Outdoor temperature keyed by the UTC start of the hour."""
    return {p.start: p.temperature_c for p in points}


def radiation_by_hour(points: Sequence[WeatherPoint]) -> Mapping[datetime, float]:
    """Global horizontal radiation (W/m2) keyed by the UTC start of the hour it covers.

    Open-Meteo's ``shortwave_radiation`` is the mean over the PRECEDING hour: the value stamped
    08:00 is the average of 07:00-08:00. The real Berlin fixture agrees (on 1 January the sun
    rises about 07:18 UTC and sets about 15:01 UTC, computed from the sun's position; the first
    non-zero stamp is 08:00, the last 15:00, and 07:00 and 16:00 are zero). A planning slot
    starting at 07:00 must therefore use
    the value stamped 08:00, so every key is moved back one hour. Using the stamp as the start
    of the slot would put the sun one hour too early.
    """
    return {p.start - timedelta(hours=1): p.shortwave_w_m2 for p in points}


def solar_yield_kwh_per_kwp(points: Sequence[WeatherPoint], performance_ratio: float) -> float:
    """Model energy per installed kWp over ``points``: sum of radiation / 1000 x ratio.
    A sanity check against known yields (Germany: very roughly 900-1,100 kWh/kWp a year on a
    good roof), not a forecast."""
    return math.fsum(p.shortwave_w_m2 for p in points) / 1000.0 * performance_ratio


def synthetic_weather(start: datetime, days: int, seed: int) -> list[WeatherPoint]:
    """Plausible German hourly weather: a yearly and a daily temperature cycle, noise, and a
    radiation curve that follows the sun and the season. Deterministic for (start, days, seed).
    NOT measured data. ``start`` is taken as UTC.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    rng = random.Random(seed)  # noqa: S311  (simulation, not crypto)
    first = datetime(start.year, start.month, start.day, tzinfo=UTC)
    out: list[WeatherPoint] = []
    drift = 0.0
    cloud = 0.6
    for i in range(days * 24):
        t = first + timedelta(hours=i)
        doy = t.timetuple().tm_yday
        season = math.sin(2 * math.pi * (doy - 110) / 365)  # +1 midsummer, -1 midwinter
        if i % 24 == 0:
            drift = 0.8 * drift + rng.gauss(0.0, 2.0)  # slow weather-system wandering
            cloud = min(1.0, max(0.1, cloud + rng.gauss(0.0, 0.25)))
        daily = math.sin(2 * math.pi * (t.hour - 9) / 24)
        temp = 9.5 + 9.0 * season + (2.0 + 1.5 * season) * daily + drift
        sun = max(0.0, math.sin(math.pi * (t.hour - 5.5) / 13.0)) if 5 <= t.hour <= 18 else 0.0
        ghi = sun * (250.0 + 450.0 * (season + 1) / 2) * cloud
        out.append(WeatherPoint(t, round(temp, 2), round(ghi, 1)))
    return out
