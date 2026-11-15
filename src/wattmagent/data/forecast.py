"""Simple, transparent forecasts built from past observations only.

These exist to measure what imperfect information costs, not to be good forecasters. Each
forecast for an hour uses only values from at least ``min(lags)`` hours earlier, so nothing from
the forecast hour itself or after it leaks in. The same functions work on any hourly series
keyed by UTC start time (radiation, temperature, load).

- ``persistence``: the same hour yesterday ("tomorrow will be like today").
- ``mean of the last k days``: the same hour averaged over the previous k days; smoother, so
  it is usually closer on average but misses a sudden change from sunny to overcast.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta


def lagged_mean_forecast(
    actual: Mapping[datetime, float], lags_hours: Sequence[int]
) -> dict[datetime, float]:
    """Forecast for hour t = mean of ``actual[t - lag]`` over the lags.

    Hours for which any needed past value is missing get no forecast (they are left out, never
    filled from the future or from the hour itself).
    """
    if not lags_hours or any(lag < 1 for lag in lags_hours):
        raise ValueError("lags must be positive whole hours")
    out: dict[datetime, float] = {}
    for t in actual:
        past = [actual.get(t - timedelta(hours=lag)) for lag in lags_hours]
        if all(v is not None for v in past):
            out[t] = math.fsum(v for v in past if v is not None) / len(past)
    return out


def persistence(actual: Mapping[datetime, float], lag_hours: int = 24) -> dict[datetime, float]:
    return lagged_mean_forecast(actual, [lag_hours])


def mean_of_last_days(actual: Mapping[datetime, float], days: int = 3) -> dict[datetime, float]:
    if days < 1:
        raise ValueError("days must be at least 1")
    return lagged_mean_forecast(actual, [24 * k for k in range(1, days + 1)])


@dataclass(frozen=True, slots=True)
class ForecastSkill:
    hours: int  # hours compared
    mae: float  # mean absolute error
    rmse: float
    bias: float  # mean (forecast - actual): positive means over-forecasting


def forecast_skill(
    actual: Mapping[datetime, float],
    forecast: Mapping[datetime, float],
    active_above: float = 10.0,
) -> ForecastSkill:
    """Error of ``forecast`` against ``actual`` over the hours both exist.

    Only hours where either value exceeds ``active_above`` are counted (for radiation that is
    the daylight hours, so the many exact-zero night hours do not flatter the result).
    """
    diffs = [
        forecast[t] - actual[t]
        for t in forecast
        if t in actual and max(actual[t], forecast[t]) > active_above
    ]
    if not diffs:
        raise ValueError("no overlapping active hours to compare")
    n = len(diffs)
    return ForecastSkill(
        hours=n,
        mae=math.fsum(abs(d) for d in diffs) / n,
        rmse=math.sqrt(math.fsum(d * d for d in diffs) / n),
        bias=math.fsum(diffs) / n,
    )


def scale_to_observations(
    forecast: Sequence[float],
    observed: Sequence[float],
    *,
    min_share: float = 0.05,
    max_ratio: float = 3.0,
    ceiling: float | None = None,
) -> list[float]:
    """The day so far as observed, the rest of the day's forecast scaled by how the day is going.

    ``observed`` holds the first ``len(observed)`` slots of the day; the remaining slots are
    ``forecast`` times ``ratio = observed so far / forecast so far``, a simple "smart
    persistence": if the sun has run 40% below forecast all morning, expect the afternoon to
    do the same. The ratio is only used once the elapsed part carries at least ``min_share`` of
    the day's forecast energy (before that, at night or at dawn, there is nothing to learn
    from), and is limited to ``0 .. max_ratio``. ``ceiling`` caps each scaled value (the
    physical maximum). Units do not matter, as long as both series share them.
    """
    h = len(observed)
    if h > len(forecast):
        raise ValueError("more observed slots than forecast slots")
    if any(not math.isfinite(v) or v < 0 for v in (*forecast, *observed)):
        raise ValueError("series must be finite and not negative")
    if max_ratio < 0 or not 0 <= min_share <= 1:
        raise ValueError("max_ratio must be >= 0 and min_share within 0..1")
    total = math.fsum(forecast)
    so_far = math.fsum(forecast[:h])
    ratio = 1.0
    if total > 0 and so_far > 0 and so_far >= min_share * total:
        ratio = min(max_ratio, math.fsum(observed) / so_far)
    out = list(observed)
    for v in forecast[h:]:
        scaled = v * ratio
        out.append(min(scaled, ceiling) if ceiling is not None else scaled)
    return out
