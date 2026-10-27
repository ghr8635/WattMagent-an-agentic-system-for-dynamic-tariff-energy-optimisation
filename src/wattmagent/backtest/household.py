"""Whole-day backtest for a household with an EV and an optional battery.

A planning day runs from the local plug-in hour to the next plug-in hour (24 h, or
23/25 h on daylight-saving days). At plug-in time the next day's prices are
assumed to be published already (day-ahead prices; the publication time is
unverified, see docs/data.md), so planners see the whole window.

Honesty rules, as in the EV-only backtest:

- planners see ``view(actual_prices)``; billing always uses the actual prices;
- the base load is synthetic and known to the planner (an assumption);
- the bill is what the grid charges; battery wear is reported separately and
  added in a ``net`` figure, because wear is a planning cost, not a bill item;
- days where the request cannot be met are counted, and a rule-based fallback runs.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo

from wattmagent.data.load import synthetic_load_kw
from wattmagent.data.prices import PricePoint
from wattmagent.devices.battery import BatterySession, BatterySpec
from wattmagent.devices.ev import EVSession, EVSpec
from wattmagent.optimiser.household import plan_household
from wattmagent.planning.household import (
    HouseholdInputs,
    HouseholdPlan,
    HouseholdPlanner,
    ev_cheapest_hours,
    ev_immediately,
)

View = Callable[[Sequence[float]], Sequence[float]]


@dataclass(frozen=True, slots=True)
class Scenario:
    ev: EVSpec
    battery: BatterySpec | None = None
    daily_load_kwh: float = 9.0


@dataclass(frozen=True, slots=True)
class HouseholdDay:
    window_start: datetime
    bill_eur: float
    wear_eur: float
    grid_kwh: float
    target_met: bool
    used_fallback: bool
    battery_end_kwh: float
    battery_delivered_kwh: float


@dataclass(frozen=True, slots=True)
class HouseholdSummary:
    name: str
    days: int
    mean_bill_eur: float
    mean_net_eur: float  # bill plus battery wear
    days_target_met: int
    days_fallback: int
    mean_battery_kwh: float  # battery energy delivered per day


def _local_hour(p: PricePoint, tz: tzinfo | None) -> int:
    return (p.start.astimezone(tz) if tz is not None else p.start).hour


def day_slices(prices: Sequence[PricePoint], ev: EVSpec, tz: tzinfo | None) -> Iterator[slice]:
    """Complete planning days: plug-in hour to the next plug-in hour. A partial tail is dropped."""
    starts = [i for i, p in enumerate(prices) if _local_hour(p, tz) == ev.plug_in_hour]
    for a, b in zip(starts, starts[1:], strict=False):
        yield slice(a, b)


def _ev_mask(hours: Sequence[int], ev: EVSpec) -> list[bool]:
    return [(h - ev.plug_in_hour) % 24 < ev.window_hours for h in hours]


def simulate_day(
    window: Sequence[PricePoint],
    scenario: Scenario,
    plan: HouseholdPlan,
    load_kw: Sequence[float],
    used_fallback: bool = False,
) -> HouseholdDay:
    """Run a plan through the device simulators and bill it on the window's actual prices."""
    ev = EVSession(scenario.ev)
    bat = BatterySession(scenario.battery) if scenario.battery else None
    bill = grid = delivered = 0.0
    for t, point in enumerate(window):
        ev_kwh = ev.charge(plan.ev_kw[t])
        demand = load_kw[t] + ev_kwh
        bat_kwh = bat.step(plan.battery_kw[t], max_delivery_kwh=demand) if bat else 0.0
        if bat_kwh < 0:
            delivered -= bat_kwh
        slot_kwh = max(demand + bat_kwh, 0.0)
        grid += slot_kwh
        bill += slot_kwh * point.eur_per_kwh
    wear = delivered * scenario.battery.wear_eur_per_kwh if scenario.battery else 0.0
    return HouseholdDay(
        window[0].start,
        bill,
        wear,
        grid,
        ev.target_met,
        used_fallback,
        bat.stored_kwh if bat else 0.0,
        delivered,
    )


def run_household_backtest(
    prices: Sequence[PricePoint],
    scenario: Scenario,
    planners: Mapping[str, HouseholdPlanner],
    view: View | None = None,
    tz: tzinfo | None = None,
) -> list[HouseholdSummary]:
    for prev, cur in zip(prices, prices[1:], strict=False):
        if (cur.start - prev.start).total_seconds() != 3600:
            raise ValueError(f"series is not gap-free hourly at {prev.start}")
    slices = list(day_slices(prices, scenario.ev, tz))
    if not slices:
        raise ValueError("price series too short for a single planning day")

    out: list[HouseholdSummary] = []
    for name, planner in planners.items():
        days: list[HouseholdDay] = []
        for sl in slices:
            window = prices[sl]
            actual = [p.eur_per_kwh for p in window]
            hours = [_local_hour(p, tz) for p in window]
            load = synthetic_load_kw(hours, scenario.daily_load_kwh)
            inputs = HouseholdInputs(
                prices=list(view(actual)) if view else actual,
                load_kw=load,
                ev_available=_ev_mask(hours, scenario.ev),
                ev=scenario.ev,
                battery=scenario.battery,
            )
            plan = planner(inputs)
            fallback = not plan.feasible
            if fallback:
                plan = ev_immediately(inputs)
            days.append(simulate_day(window, scenario, plan, load, fallback))
        n = len(days)
        out.append(
            HouseholdSummary(
                name=name,
                days=n,
                mean_bill_eur=math.fsum(d.bill_eur for d in days) / n,
                mean_net_eur=math.fsum(d.bill_eur + d.wear_eur for d in days) / n,
                days_target_met=sum(d.target_met for d in days),
                days_fallback=sum(d.used_fallback for d in days),
                mean_battery_kwh=math.fsum(d.battery_delivered_kwh for d in days) / n,
            )
        )
    return out


def default_planners(with_battery: bool) -> dict[str, HouseholdPlanner]:
    """The strategies compared by the CLI, in display order. The first is the baseline."""

    def milp_ev_only(inputs: HouseholdInputs) -> HouseholdPlan:
        return plan_household(replace(inputs, battery=None))

    planners: dict[str, HouseholdPlanner] = {
        "ev_immediately": ev_immediately,
        "ev_cheapest_hours": ev_cheapest_hours,
        "milp_ev_only": milp_ev_only,
    }
    if with_battery:
        planners["milp_ev_and_battery"] = plan_household
    return planners


def format_household_summary(results: Sequence[HouseholdSummary], baseline: str) -> str:
    base = next((r for r in results if r.name == baseline), None)
    head = (
        f"{'strategy':<24}{'days':>6}{'bill/day':>10}{'net/day':>10}"
        f"{'saved/day':>11}{'vs base':>9}{'bat kWh/d':>11}{'target met':>13}{'fallback':>10}"
    )
    lines = [head]
    for r in results:
        saved = delta = ""
        if base and base.mean_net_eur != 0 and r is not base:
            saved = f"{base.mean_net_eur - r.mean_net_eur:.3f}"
            delta = f"{(r.mean_net_eur / base.mean_net_eur - 1) * 100:+.1f}%"
        lines.append(
            f"{r.name:<24}{r.days:>6}{r.mean_bill_eur:>10.3f}{r.mean_net_eur:>10.3f}"
            f"{saved:>11}{delta:>9}{r.mean_battery_kwh:>11.2f}"
            f"{r.days_target_met:>8}/{r.days:<4}{r.days_fallback:>10}"
        )
    return "\n".join(lines)
