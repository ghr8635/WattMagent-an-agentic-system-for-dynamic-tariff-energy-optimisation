"""Daily-window backtest for a single EV against a price series.

Honesty rule: a planner only sees ``view(actual_window)`` (identity by default;
later, a forecast), but every schedule is billed against the *actual* prices.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from wattmagent.data.prices import PricePoint
from wattmagent.devices.ev import EVSession, EVSpec
from wattmagent.planning.baselines import Planner

View = Callable[[Sequence[float]], Sequence[float]]


@dataclass(frozen=True, slots=True)
class DayResult:
    window_start: datetime
    cost_eur: float
    grid_kwh: float
    target_met: bool


@dataclass(frozen=True, slots=True)
class StrategySummary:
    name: str
    days: int
    total_cost_eur: float
    mean_cost_per_day_eur: float
    days_target_met: int
    total_grid_kwh: float


def _validate_hourly(prices: Sequence[PricePoint]) -> None:
    for prev, cur in zip(prices, prices[1:], strict=False):
        if cur.start - prev.start != timedelta(hours=1):
            raise ValueError(f"series is not gap-free hourly at {prev.start}")


def _local_hour(p: PricePoint, tz: tzinfo | None) -> int:
    return (p.start.astimezone(tz) if tz is not None else p.start).hour


def window_slices(
    prices: Sequence[PricePoint], spec: EVSpec, tz: tzinfo | None = None
) -> Iterator[slice]:
    """One slice per plug-in session: from ``plug_in_hour`` to the next ``depart_hour``.

    With ``tz`` (and timezone-aware prices) the hours are local clock hours, so on
    daylight-saving nights a window is 12 or 14 hours long instead of 13. Without
    ``tz`` the hours are read from the timestamps as they are. Incomplete windows at
    the end of the series are dropped.
    """
    n = len(prices)
    last_slot_hour = (spec.depart_hour - 1) % 24
    i = 0
    while i < n:
        if _local_hour(prices[i], tz) != spec.plug_in_hour:
            i += 1
            continue
        j = i + 1
        while j < n and _local_hour(prices[j], tz) != spec.depart_hour:
            j += 1
        if j >= n:
            # Data ends before the departure slot exists; keep the window only if it is complete.
            if _local_hour(prices[n - 1], tz) == last_slot_hour:
                yield slice(i, n)
            return
        yield slice(i, j)
        i = j


def run_day(
    window: Sequence[PricePoint], spec: EVSpec, planner: Planner, view: View | None = None
) -> DayResult:
    actual = [p.eur_per_kwh for p in window]
    seen = view(actual) if view else actual
    schedule = planner(seen, spec)
    if len(schedule) != len(actual):
        raise ValueError(f"planner returned {len(schedule)} slots for {len(actual)} hours")
    session = EVSession(spec)
    cost = grid = 0.0
    for kw, price in zip(schedule, actual, strict=True):
        kwh = session.charge(kw)
        grid += kwh
        cost += kwh * price
    return DayResult(window[0].start, cost, grid, session.target_met)


def run_backtest(
    prices: Sequence[PricePoint],
    spec: EVSpec,
    planners: Mapping[str, Planner],
    view: View | None = None,
    tz: tzinfo | None = None,
) -> list[StrategySummary]:
    _validate_hourly(prices)
    slices = list(window_slices(prices, spec, tz))
    if not slices:
        raise ValueError("price series too short for a single charging window")
    out: list[StrategySummary] = []
    for name, planner in planners.items():
        days = [run_day(prices[s], spec, planner, view) for s in slices]
        total = sum(d.cost_eur for d in days)
        out.append(
            StrategySummary(
                name=name,
                days=len(days),
                total_cost_eur=total,
                mean_cost_per_day_eur=total / len(days),
                days_target_met=sum(d.target_met for d in days),
                total_grid_kwh=sum(d.grid_kwh for d in days),
            )
        )
    return out


def format_summary(results: Sequence[StrategySummary], baseline: str) -> str:
    base = next((r for r in results if r.name == baseline), None)
    head = f"{'strategy':<34}{'days':>6}{'EUR/day':>10}{'vs base':>10}{'saved/day':>11}"
    lines = [head + f"{'target met':>14}"]
    for r in results:
        delta = saved = ""
        if base and base.total_cost_eur != 0 and r is not base:
            delta = f"{(r.total_cost_eur / base.total_cost_eur - 1) * 100:+.1f}%"
            saved = f"{base.mean_cost_per_day_eur - r.mean_cost_per_day_eur:.3f}"
        lines.append(
            f"{r.name:<34}{r.days:>6}{r.mean_cost_per_day_eur:>10.3f}{delta:>10}{saved:>11}"
            f"{r.days_target_met:>9}/{r.days:<4}"
        )
    return "\n".join(lines)
