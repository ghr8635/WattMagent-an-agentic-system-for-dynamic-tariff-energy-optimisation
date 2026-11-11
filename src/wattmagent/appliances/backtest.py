"""Whole-day backtest of appliance strategies on a price series.

A planning day is local midnight to the next local midnight (23, 24 or 25 hours). Honesty
rules, as in the EV backtest:

- planners see the day's prices in advance (day-ahead prices; publication time unverified);
- by default planners also see the day's actual weather (perfect foresight); with
  ``radiation_forecast`` they plan on a forecast of the sun instead, while billing and the
  verifier use the actual sun, so the cost of forecast error shows up as a higher bill or an
  invalid day;
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

from wattmagent.appliances.baselines import cheapest_start, run_immediately, self_consumption
from wattmagent.appliances.household import Household, build_day_inputs
from wattmagent.appliances.models import AppliancePlan, DayInputs
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.pipeline import AppliancePlanner, plan_appliances_safely
from wattmagent.appliances.simulate import appliance_kw, plan_bill
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
    return plan_bill(billed, plan), math.fsum(kw), valid


def run_appliance_backtest(
    prices: Sequence[PricePoint],
    household: Household,
    planners: Mapping[str, AppliancePlanner],
    tz: tzinfo | None = None,
    outdoor: Mapping[datetime, float] | None = None,
    heating_max_mean_c: float = 15.0,
    radiation: Mapping[datetime, float] | None = None,
    radiation_forecast: Mapping[datetime, float] | None = None,
) -> list[ApplianceSummary]:
    """Replay ``planners`` over every complete local day.

    ``outdoor`` maps the UTC start of each hour to the outdoor temperature (needed when the
    household has a heat pump). The heat pump is part of a day's plan only when that day's mean
    outdoor temperature is at most ``heating_max_mean_c``; other days have no heating need and
    the pump is left out (the model cannot cool). Planners see the day's actual weather, a
    perfect-foresight assumption like the price one. A household with solar also needs
    ``radiation`` (W/m2, same keys); every day gets its solar, and the billed cost becomes the
    whole net grid bill (see ``plan_cost``).

    ``radiation_forecast`` (same keys) makes the planners see forecast solar instead of the
    actual values. Only days for which a forecast exists for every hour are replayed, the same
    days for every planner, so ``days`` can be smaller. Billing, the validity check and the
    hot-water and heat-pump physics use actuals; outdoor temperature is not forecast here (the
    heat pump still sees the true temperatures).
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

    def day_forecast(window: Sequence[PricePoint]) -> list[float] | None:
        """Forecast radiation for the day, or None if any hour has no forecast."""
        if radiation_forecast is None:
            return None
        values: list[float] = []
        for p in window:
            start = p.start if p.start.tzinfo else p.start.replace(tzinfo=UTC)
            value = radiation_forecast.get(start.astimezone(UTC))
            if value is None:
                return None
            values.append(value)
        return values

    if radiation_forecast is not None:
        if household.solar is None:
            raise ValueError("a radiation forecast only makes sense for a household with solar")
        slices = [sl for sl in slices if day_forecast(prices[sl]) is not None]
        if not slices:
            raise ValueError("the forecast covers no complete day")

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
            seen = inputs  # what the planner is told
            predicted = day_forecast(window)
            if predicted is not None:
                seen = build_day_inputs(household, actual, hours, temps, predicted)
            plan = planner(seen)
            fallback = not plan.feasible
            if fallback:
                plan = run_immediately(seen)
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


def default_planners(
    audit: AuditLog | None = None, with_battery: bool = False
) -> dict[str, AppliancePlanner]:
    """Strategies in display order; the first is the baseline. With ``audit`` the optimiser runs
    through the safety pipeline, so every plan is verified and logged."""

    def milp(inputs: DayInputs) -> AppliancePlan:
        return plan_appliances_safely(inputs, audit=audit).plan

    chosen: AppliancePlanner = milp if audit is not None else plan_appliances
    planners: dict[str, AppliancePlanner] = {
        "run_immediately": run_immediately,
        "cheapest_start": cheapest_start,
    }
    if with_battery:  # only differs from run_immediately when the household has a battery
        planners["self_consumption"] = self_consumption
    planners["milp_optimiser"] = chosen
    return planners


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
