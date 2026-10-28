"""Independent plan verifier.

The optimiser is trusted to be *good*; the verifier does not trust it to be *safe*.
It re-checks a plan against hard rules using only the device simulators (the source
of truth for physics) and its own arithmetic, never the optimiser's internals.

Rules (see ``Rule``): plan shape and finite numbers; EV power limits and plug-in
hours; EV target reached; EV and battery requests physically executable; battery
power limits; battery not left lower than it started; cost not worse than the
"charge immediately" baseline beyond a small solver tolerance.

The cost tolerance exists because a mixed-integer solver stops within a small
relative gap of the optimum (set to 1e-6 here); the allowance is deliberately
larger (0.1% + 0.5 cent) so that only real regressions trip it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from wattmagent.devices.battery import BatterySession
from wattmagent.devices.ev import EVSession
from wattmagent.planning.household import HouseholdInputs, HouseholdPlan, ev_immediately

_POWER_TOL_KW = 1e-6
_ENERGY_TOL_KWH = 1e-4
_COST_REL_TOL = 1e-3
_COST_ABS_TOL_EUR = 0.005


class Rule(StrEnum):
    NO_SCHEDULE = "no_schedule"
    SHAPE = "shape"
    NOT_FINITE = "not_finite"
    EV_POWER = "ev_power"
    EV_UNPLUGGED = "ev_unplugged"
    EV_TARGET = "ev_target"
    EV_NOT_ACCEPTED = "ev_not_accepted"
    NO_BATTERY = "no_battery"
    BATTERY_POWER = "battery_power"
    BATTERY_NOT_EXECUTABLE = "battery_not_executable"
    BATTERY_END = "battery_end"
    COST = "cost_vs_baseline"


@dataclass(frozen=True, slots=True)
class Violation:
    rule: Rule
    message: str
    slot: int | None = None


@dataclass(frozen=True, slots=True)
class VerificationReport:
    violations: tuple[Violation, ...]
    cost_eur: float | None = None  # bill + battery wear, on the prices the plan was made with
    baseline_cost_eur: float | None = None

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def rules(self) -> list[str]:
        return sorted({v.rule.value for v in self.violations})


@dataclass(frozen=True, slots=True)
class _Outcome:
    cost_eur: float
    ev_target_met: bool
    battery_end_kwh: float
    ev_rejected_slots: list[int]
    battery_rejected_slots: list[int]


def _run(inputs: HouseholdInputs, ev_kw: list[float], battery_kw: list[float]) -> _Outcome:
    ev = EVSession(inputs.ev)
    bat = BatterySession(inputs.battery) if inputs.battery else None
    cost = delivered = 0.0
    ev_bad: list[int] = []
    bat_bad: list[int] = []
    for t in range(inputs.slots):
        requested_ev = min(max(ev_kw[t], 0.0), inputs.ev.max_charge_kw)
        ev_kwh = ev.charge(ev_kw[t])
        if abs(ev_kwh - requested_ev) > _ENERGY_TOL_KWH:
            ev_bad.append(t)
        demand = inputs.load_kw[t] + ev_kwh
        bat_kwh = 0.0
        if bat is not None:
            limit = bat.spec.max_power_kw
            requested_bat = min(max(battery_kw[t], -limit), limit)
            bat_kwh = bat.step(battery_kw[t], max_delivery_kwh=demand)
            if abs(bat_kwh - requested_bat) > _ENERGY_TOL_KWH:
                bat_bad.append(t)
            if bat_kwh < 0:
                delivered -= bat_kwh
        cost += max(demand + bat_kwh, 0.0) * inputs.prices[t]
    if inputs.battery:
        cost += delivered * inputs.battery.wear_eur_per_kwh
    return _Outcome(cost, ev.target_met, bat.stored_kwh if bat else 0.0, ev_bad, bat_bad)


def verify_plan(
    inputs: HouseholdInputs,
    plan: HouseholdPlan,
    *,
    require_target: bool = True,
    compare_with_baseline: bool = True,
) -> VerificationReport:
    """Check ``plan`` against ``inputs``. Never raises for a bad plan; it reports."""
    if not plan.feasible:
        return VerificationReport((Violation(Rule.NO_SCHEDULE, plan.message or "no schedule"),))
    n = inputs.slots
    if len(plan.ev_kw) != n or len(plan.battery_kw) != n:
        return VerificationReport(
            (
                Violation(
                    Rule.SHAPE,
                    f"expected {n} slots, got ev={len(plan.ev_kw)} battery={len(plan.battery_kw)}",
                ),
            )
        )
    if not all(math.isfinite(v) for v in (*plan.ev_kw, *plan.battery_kw)):
        return VerificationReport((Violation(Rule.NOT_FINITE, "plan contains NaN or infinity"),))

    found: list[Violation] = []
    for t, (kw, plugged) in enumerate(zip(plan.ev_kw, inputs.ev_available, strict=True)):
        if kw < -_POWER_TOL_KW or kw > inputs.ev.max_charge_kw + _POWER_TOL_KW:
            found.append(Violation(Rule.EV_POWER, f"EV power {kw:.3f} kW outside limits", t))
        if kw > _POWER_TOL_KW and not plugged:
            found.append(Violation(Rule.EV_UNPLUGGED, "EV charging while not plugged in", t))
    for t, kw in enumerate(plan.battery_kw):
        if inputs.battery is None:
            if abs(kw) > _POWER_TOL_KW:
                found.append(Violation(Rule.NO_BATTERY, "battery power but no battery", t))
        elif abs(kw) > inputs.battery.max_power_kw + _POWER_TOL_KW:
            found.append(Violation(Rule.BATTERY_POWER, f"battery power {kw:.3f} kW too high", t))

    out = _run(inputs, plan.ev_kw, plan.battery_kw)
    if require_target and not out.ev_target_met:
        found.append(Violation(Rule.EV_TARGET, "EV does not reach its target charge"))
    found += [
        Violation(Rule.EV_NOT_ACCEPTED, "car cannot accept this power", t)
        for t in out.ev_rejected_slots
    ]
    found += [
        Violation(Rule.BATTERY_NOT_EXECUTABLE, "battery cannot do this", t)
        for t in out.battery_rejected_slots
    ]
    if inputs.battery and out.battery_end_kwh < inputs.battery.initial_kwh - _ENERGY_TOL_KWH:
        found.append(Violation(Rule.BATTERY_END, "battery ends lower than it started"))

    baseline_cost: float | None = None
    if compare_with_baseline:
        base_plan = ev_immediately(inputs)
        base = _run(inputs, base_plan.ev_kw, base_plan.battery_kw)
        if base.ev_target_met:  # only meaningful when the baseline itself meets the target
            baseline_cost = base.cost_eur
            allowed = baseline_cost * (1 + _COST_REL_TOL) + _COST_ABS_TOL_EUR
            if out.cost_eur > allowed:
                found.append(
                    Violation(
                        Rule.COST,
                        f"cost {out.cost_eur:.3f} EUR exceeds baseline {baseline_cost:.3f} EUR",
                    )
                )
    return VerificationReport(tuple(found), out.cost_eur, baseline_cost)
