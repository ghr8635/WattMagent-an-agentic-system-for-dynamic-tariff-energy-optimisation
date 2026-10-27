"""Household planning types and the rule-based planners.

A household planner receives everything it is allowed to know for one planning day
(prices it can see, expected base load, when the EV is plugged in, device specs)
and returns a kW schedule per slot for the EV and the battery.

``PlanStatus`` makes the outcome explicit. An infeasible request is *reported*:
the plan carries no schedule, and the caller decides what to do (the backtest
falls back to charging as early as possible and counts the day).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from wattmagent.devices.battery import BatterySpec
from wattmagent.devices.ev import EVSpec
from wattmagent.planning.baselines import charge_immediately, cheapest_hours


class PlanStatus(StrEnum):
    OPTIMAL = "optimal"  # solved to optimality by the optimiser
    RULE = "rule"  # produced by a fixed rule, no optimisation
    INFEASIBLE = "infeasible"  # the request cannot be met; no schedule


@dataclass(frozen=True, slots=True)
class HouseholdInputs:
    """One planning day. All sequences have one entry per hourly slot."""

    prices: Sequence[float]  # EUR/kWh the planner believes
    load_kw: Sequence[float]  # expected base load
    ev_available: Sequence[bool]  # EV plugged in during the slot
    ev: EVSpec
    battery: BatterySpec | None = None

    def __post_init__(self) -> None:
        n = len(self.prices)
        if n == 0:
            raise ValueError("a planning day needs at least one slot")
        if not (len(self.load_kw) == len(self.ev_available) == n):
            raise ValueError("prices, load_kw and ev_available must have the same length")

    @property
    def slots(self) -> int:
        return len(self.prices)


@dataclass(frozen=True, slots=True)
class HouseholdPlan:
    status: PlanStatus
    ev_kw: list[float] = field(default_factory=list)  # grid-side charging power
    battery_kw: list[float] = field(default_factory=list)  # + charge, - discharge (AC side)
    message: str = ""
    # Cost the optimiser expects on the prices it was given: grid bill plus battery wear.
    # None for rule-based plans. Tests check it against the simulator.
    planned_cost_eur: float | None = None

    @property
    def feasible(self) -> bool:
        return self.status is not PlanStatus.INFEASIBLE


HouseholdPlanner = Callable[[HouseholdInputs], HouseholdPlan]


def _rule_plan(
    inputs: HouseholdInputs, rule: Callable[[Sequence[float], EVSpec], list[float]]
) -> HouseholdPlan:
    """Run an EV-only rule on the slots where the EV is plugged in; the battery idles."""
    plugged = [i for i, ok in enumerate(inputs.ev_available) if ok]
    ev_kw = [0.0] * inputs.slots
    for i, kw in zip(plugged, rule([inputs.prices[i] for i in plugged], inputs.ev), strict=True):
        ev_kw[i] = kw
    return HouseholdPlan(PlanStatus.RULE, ev_kw, [0.0] * inputs.slots)


def ev_immediately(inputs: HouseholdInputs) -> HouseholdPlan:
    """Charge as soon as plugged in. The battery does nothing. The 'no optimisation' floor."""
    return _rule_plan(inputs, charge_immediately)


def ev_cheapest_hours(inputs: HouseholdInputs) -> HouseholdPlan:
    """Fill the cheapest plugged-in hours first. The battery does nothing."""
    return _rule_plan(inputs, cheapest_hours)
