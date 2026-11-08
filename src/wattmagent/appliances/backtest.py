"""Whole-day backtest of appliance strategies on a price series.

A planning day is local midnight to the next local midnight (23, 24 or 25 hours). Honesty
rules, as in the EV backtest:

- planners see the day's prices in advance (day-ahead prices; publication time unverified);
- billing is the appliance energy on the actual prices; the base load never enters the cost;
- hot-water use and base load are synthetic shapes, and appliance figures are assumptions;
- a day counts as *valid* only if the independent verifier finds no violation, whatever
  strategy produced the plan; days where the optimiser's plan is replaced by the fallback
  are counted.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo

from wattmagent.appliances.baselines import cheapest_start, run_immediately
from wattmagent.appliances.household import Household, build_day_inputs
from wattmagent.appliances.models import AppliancePlan, DayInputs
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.pipeline import AppliancePlanner, plan_appliances_safely
from wattmagent.appliances.simulate import appliance_kw, plan_cost
from wattmagent.appliances.verifier import verify_appliance_plan
from wattmagent.data.prices import PricePoint
from wattmagent.safety.audit import AuditLog


@dataclass(frozen=True, slots=True)
class ApplianceDay:
    window_start: datetime
    cost_eur: float
    kwh: float
    valid: bool
    used_fallback: bool
    heating: bool = False


@dataclass(frozen=True, slots=True)
class ApplianceSummary:
    name: str
    days: int
    mean_cost_eur: float
    mean_kwh: float
    days_valid: int
    days_fallback: int
    heating_days: int = 0  # days on which the heat pump was part of the plan


def _local_hour(p: PricePoint, tz: tzinfo | None) -> int:
    return (p.start.astimezone(tz) if tz is not None else p.start).hour


def day_slices(prices: Sequence[PricePoint], tz: tzinfo | None) -> Iterator[slice]:
    """Complete local days, midnight to midnight. A partial first or last day is dropped."""
    starts = [i for i, p in enumerate(prices) if _local_hour(p, tz) == 0]
    for a, b in zip(starts, starts[1:], strict=False):
        yield slice(a, b)


def simulate_day(
    inputs: DayInputs, plan: AppliancePlan, actual: Sequence[float]
) -> tuple[float, float, bool]:
    """Bill ``plan`` on ``actual`` prices. Returns (cost EUR, kWh, passes the verifier)."""
    kw = appliance_kw(inputs, plan)
    valid = verify_appliance_plan(inputs, plan, compare_with_baseline=False).passed
    billed = replace(inputs, prices=list(actual))
    return plan_cost(billed, kw), math.fsum(kw), valid


def run_appliance_backtest(
    prices: Sequence[PricePoint],
    household: Household,
    planners: Mapping[str, AppliancePlanner],
    tz: tzinfo | None = None,
    outdoor: Mapping[datetime, float] | None = None,
    heating_max_mean_c: float = 15.0,
    radiation: Mapping[datetime, float] | None = None,
) -> list[ApplianceSummary]:
    """Replay ``planners`` over every complete local day.

    ``outdoor`` maps the UTC start of each hour to the outdoor temperature (needed when the
    household has a heat pump). The heat pump is part of a day's plan only when that day's mean
    outdoor temperature is at most ``heating_max_mean_c``; other days have no heating need and
    the pump is left out (the model cannot cool). Planners see the day's actual weather, a
    perfect-foresight assumption like the price one. A household with solar also needs
    ``radiation`` (W/m2, same keys); every day gets its solar, and the billed cost becomes the
    whole net grid bill (see ``plan_cost``).
    """
    for prev, cur in zip(prices, prices[1:], strict=False):
        if (cur.start - prev.start).total_seconds() != 3600:
            raise ValueError(f"series is not gap-free hourly at {prev.start}")
    slices = list(day_slices(prices, tz))
    if not slices:
        raise ValueError("price series too short for a single local day")

    if household.heatpump is not None and outdoor is None:
        raise ValueError("this household has a heat pump: outdoor temperatures are required")

    if household.solar is not None and radiation is None:
        raise ValueError("this household has solar: radiation data is required")

    def day_radiation(window: Sequence[PricePoint]) -> list[float] | None:
        if household.solar is None or radiation is None:
            return None
        values: list[float] = []
        for p in window:
            start = p.start if p.start.tzinfo else p.start.replace(tzinfo=UTC)
            value = radiation.get(start.astimezone(UTC))
            if value is None:
                raise ValueError(f"no radiation for {p.start.isoformat()}; it must cover every day")
            values.append(value)
        return values

    def day_outdoor(window: Sequence[PricePoint]) -> list[float] | None:
        if household.heatpump is None or outdoor is None:
            return None
        temps: list[float] = []
        for p in window:
            start = p.start if p.start.tzinfo else p.start.replace(tzinfo=UTC)
            value = outdoor.get(start.astimezone(UTC))
            if value is None:
                raise ValueError(f"no weather for {p.start.isoformat()}; it must cover every day")
            temps.append(value)
        return temps if math.fsum(temps) / len(temps) <= heating_max_mean_c else None

    out: list[ApplianceSummary] = []
    for name, planner in planners.items():
        days: list[ApplianceDay] = []
        for sl in slices:
            window = prices[sl]
            actual = [p.eur_per_kwh for p in window]
            hours = [_local_hour(p, tz) for p in window]
            temps = day_outdoor(window)
            inputs = build_day_inputs(household, actual, hours, temps, day_radiation(window))
            plan = planner(inputs)
            fallback = not plan.feasible
            if fallback:
                plan = run_immediately(inputs)
            cost, kwh, valid = simulate_day(inputs, plan, actual)
            days.append(
                ApplianceDay(window[0].start, cost, kwh, valid, fallback, temps is not None)
            )
        n = len(days)
        out.append(
            ApplianceSummary(
                name=name,
                days=n,
                mean_cost_eur=math.fsum(d.cost_eur for d in days) / n,
                mean_kwh=math.fsum(d.kwh for d in days) / n,
                days_valid=sum(d.valid for d in days),
                days_fallback=sum(d.used_fallback for d in days),
                heating_days=sum(d.heating for d in days),
            )
        )
    return out


def default_planners(audit: AuditLog | None = None) -> dict[str, AppliancePlanner]:
    """Strategies in display order; the first is the baseline. With ``audit`` the optimiser runs
    through the safety pipeline, so every plan is verified and logged."""

    def milp(inputs: DayInputs) -> AppliancePlan:
        return plan_appliances_safely(inputs, audit=audit).plan

    chosen: AppliancePlanner = milp if audit is not None else plan_appliances
    return {
        "run_immediately": run_immediately,
        "cheapest_start": cheapest_start,
        "milp_optimiser": chosen,
    }


def format_summary(results: Sequence[ApplianceSummary], baseline: str) -> str:
    base = next((r for r in results if r.name == baseline), None)
    head = (
        f"{'strategy':<18}{'days':>6}{'cost/day':>10}{'saved/day':>11}{'vs base':>9}"
        f"{'kWh/day':>9}{'valid days':>13}{'fallback':>10}{'heating':>9}"
    )
    lines = [head]
    for r in results:
        saved = delta = ""
        if base and base.mean_cost_eur != 0 and r is not base:
            saved = f"{base.mean_cost_eur - r.mean_cost_eur:.4f}"
            delta = f"{(r.mean_cost_eur / base.mean_cost_eur - 1) * 100:+.1f}%"
        lines.append(
            f"{r.name:<18}{r.days:>6}{r.mean_cost_eur:>10.4f}{saved:>11}{delta:>9}"
            f"{r.mean_kwh:>9.2f}{r.days_valid:>8}/{r.days:<4}{r.days_fallback:>10}{r.heating_days:>9}"
        )
    return "\n".join(lines)
