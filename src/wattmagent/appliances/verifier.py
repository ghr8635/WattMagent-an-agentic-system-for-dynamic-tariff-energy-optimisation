"""Independent check of an appliance plan.

Like the EV verifier, this does not look inside the optimiser. It re-derives what the plan
does from the plan's own power series and the physical tank model, then checks hard rules:

- every cycle appears, once, as exactly its profile, without a pause, inside its window;
- heat pump within its electric limit at each hour's COP; indoor temperature inside the band
  and not colder at the end;
- tank power within limits (and either off or full if the element does not modulate);
- tank temperature inside the comfort band in every slot, and not colder at the end;
- battery within its power limits, never charging and discharging at once, stored energy inside
  its range in every slot and not lower at the end, and never feeding the grid;
- grid import (base load plus appliances plus battery minus own solar) within the connection
  limit;
- cost not worse than the "run immediately" baseline (when that baseline is itself valid).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from wattmagent.appliances.baselines import run_immediately
from wattmagent.appliances.models import AppliancePlan, CycleJob, DayInputs
from wattmagent.appliances.simulate import (
    appliance_kw,
    battery_net_kw,
    min_import_kw,
    place,
    plan_bill,
    simulate_battery,
    simulate_building,
    simulate_tank,
)

_POWER_TOL_KW = 1e-6
_TEMP_TOL_K = 1e-3
_ENERGY_TOL_KWH = 1e-3
_COST_REL_TOL = 1e-3
_COST_ABS_TOL_EUR = 0.0005


class ApplianceRule(StrEnum):
    NO_SCHEDULE = "no_schedule"
    SHAPE = "shape"
    NOT_FINITE = "not_finite"
    CYCLE_SHAPE = "cycle_shape"  # not exactly the profile once: partial, split, scaled, twice
    CYCLE_WINDOW = "cycle_window"
    TANK_POWER = "tank_power"
    TANK_COMFORT = "tank_comfort"
    TANK_END = "tank_end"
    HEATPUMP_POWER = "heatpump_power"
    INDOOR_COMFORT = "indoor_comfort"
    INDOOR_END = "indoor_end"
    BATTERY_POWER = "battery_power"  # over a limit, or charging and discharging at once
    BATTERY_SOC = "battery_soc"  # stored energy outside its range
    BATTERY_END = "battery_end"  # emptier at the end than at the start
    BATTERY_EXPORT = "battery_export"  # discharging more than the house uses (into the grid)
    GRID_LIMIT = "grid_limit"
    COST = "cost_vs_baseline"


# Rules about the request being out of reach rather than the plan being unsafe.
REQUEST_RULES = frozenset(
    {
        ApplianceRule.TANK_COMFORT,
        ApplianceRule.TANK_END,
        ApplianceRule.INDOOR_COMFORT,
        ApplianceRule.INDOOR_END,
    }
)


@dataclass(frozen=True, slots=True)
class ApplianceViolation:
    rule: ApplianceRule
    message: str
    slot: int | None = None


@dataclass(frozen=True, slots=True)
class ApplianceReport:
    violations: tuple[ApplianceViolation, ...]
    cost_eur: float | None = None
    baseline_cost_eur: float | None = None

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def rules(self) -> list[str]:
        return sorted({v.rule.value for v in self.violations})


def _matches(series: list[float], expected: list[float]) -> bool:
    return all(abs(a - b) <= _POWER_TOL_KW for a, b in zip(series, expected, strict=True))


def _check_cycle(job: CycleJob, series: list[float], n: int) -> list[ApplianceViolation]:
    name = job.spec.name
    starts = [
        s
        for s in range(n - job.spec.duration + 1)
        if _matches(series, place(job.spec.profile_kw, s, n))
    ]
    if not starts:
        return [
            ApplianceViolation(
                ApplianceRule.CYCLE_SHAPE, f"{name}: power series is not the profile run once"
            )
        ]
    if not any(job.earliest <= s <= job.deadline - job.spec.duration for s in starts):
        return [
            ApplianceViolation(
                ApplianceRule.CYCLE_WINDOW,
                f"{name}: runs in slots {starts[0]}..{starts[0] + job.spec.duration - 1}, "
                f"allowed {job.earliest}..{job.deadline - 1}",
                starts[0],
            )
        ]
    return []


def _check_battery(inputs: DayInputs, plan: AppliancePlan) -> list[ApplianceViolation]:
    battery = inputs.battery
    assert battery is not None
    found: list[ApplianceViolation] = []
    charge, discharge = plan.battery_charge_kw, plan.battery_discharge_kw
    for t in range(inputs.slots):
        ch, dis = charge[t], discharge[t]
        if ch < -_POWER_TOL_KW or ch > battery.max_charge_kw + _POWER_TOL_KW:
            found.append(
                ApplianceViolation(
                    ApplianceRule.BATTERY_POWER,
                    f"battery charge {ch:.3f} kW outside 0..{battery.max_charge_kw:g} kW",
                    t,
                )
            )
        if dis < -_POWER_TOL_KW or dis > battery.max_discharge_kw + _POWER_TOL_KW:
            found.append(
                ApplianceViolation(
                    ApplianceRule.BATTERY_POWER,
                    f"battery discharge {dis:.3f} kW outside 0..{battery.max_discharge_kw:g} kW",
                    t,
                )
            )
        if min(ch, dis) > _POWER_TOL_KW:
            found.append(
                ApplianceViolation(
                    ApplianceRule.BATTERY_POWER, "battery charges and discharges in one hour", t
                )
            )
    stored = simulate_battery(inputs, charge, discharge)
    for t, kwh in enumerate(stored):
        if (
            kwh < battery.soc_min_kwh - _ENERGY_TOL_KWH
            or kwh > battery.capacity_kwh + _ENERGY_TOL_KWH
        ):
            found.append(
                ApplianceViolation(
                    ApplianceRule.BATTERY_SOC,
                    f"battery holds {kwh:.2f} kWh, range "
                    f"{battery.soc_min_kwh:.2f}-{battery.capacity_kwh:.2f} kWh",
                    t,
                )
            )
    if stored and stored[-1] < battery.soc_initial_kwh - _ENERGY_TOL_KWH:
        found.append(
            ApplianceViolation(
                ApplianceRule.BATTERY_END,
                f"battery ends at {stored[-1]:.2f} kWh, emptier than the start "
                f"{battery.soc_initial_kwh:.2f} kWh",
            )
        )
    appliances = appliance_kw(inputs, plan)
    net = battery_net_kw(inputs, plan)
    for t in range(inputs.slots):
        if inputs.base_kw[t] + appliances[t] + net[t] < -_POWER_TOL_KW:
            found.append(
                ApplianceViolation(
                    ApplianceRule.BATTERY_EXPORT,
                    "battery discharges more than the house uses (battery energy would be "
                    "exported)",
                    t,
                )
            )
    return found


def _hard_checks(inputs: DayInputs, plan: AppliancePlan) -> list[ApplianceViolation]:
    n = inputs.slots
    found: list[ApplianceViolation] = []

    names = {j.spec.name for j in inputs.jobs}
    if set(plan.cycle_kw) != names or any(len(s) != n for s in plan.cycle_kw.values()):
        return [ApplianceViolation(ApplianceRule.SHAPE, "cycle series do not match the jobs")]
    if inputs.tank is not None and len(plan.tank_kw) != n:
        return [ApplianceViolation(ApplianceRule.SHAPE, "tank series has the wrong length")]
    if inputs.tank is None and any(abs(v) > _POWER_TOL_KW for v in plan.tank_kw):
        return [ApplianceViolation(ApplianceRule.SHAPE, "tank power but no tank")]
    if inputs.building is not None and len(plan.heatpump_kw) != n:
        return [ApplianceViolation(ApplianceRule.SHAPE, "heat-pump series has the wrong length")]
    if inputs.building is None and any(abs(v) > _POWER_TOL_KW for v in plan.heatpump_kw):
        return [ApplianceViolation(ApplianceRule.SHAPE, "heat-pump power but no building")]
    if inputs.battery is not None and (
        len(plan.battery_charge_kw) != n or len(plan.battery_discharge_kw) != n
    ):
        return [ApplianceViolation(ApplianceRule.SHAPE, "battery series have the wrong length")]
    if inputs.battery is None and any(
        abs(v) > _POWER_TOL_KW for v in (*plan.battery_charge_kw, *plan.battery_discharge_kw)
    ):
        return [ApplianceViolation(ApplianceRule.SHAPE, "battery power but no battery")]
    everything = (
        [v for s in plan.cycle_kw.values() for v in s]
        + list(plan.tank_kw)
        + list(plan.heatpump_kw)
        + list(plan.battery_charge_kw)
        + list(plan.battery_discharge_kw)
    )
    if not all(math.isfinite(v) for v in everything):
        return [ApplianceViolation(ApplianceRule.NOT_FINITE, "plan contains NaN or infinity")]

    for job in inputs.jobs:
        found += _check_cycle(job, plan.cycle_kw[job.spec.name], n)

    tank = inputs.tank
    if tank is not None:
        for t, kw in enumerate(plan.tank_kw):
            if kw < -_POWER_TOL_KW or kw > tank.power_kw + _POWER_TOL_KW:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.TANK_POWER, f"tank {kw:.3f} kW outside limits", t
                    )
                )
            elif not tank.modulating and _POWER_TOL_KW < kw < tank.power_kw - _POWER_TOL_KW:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.TANK_POWER, f"on/off element at partial power {kw:.3f} kW", t
                    )
                )
        temps = simulate_tank(inputs, plan.tank_kw)
        for t, temp in enumerate(temps):
            if temp < tank.t_min_c - _TEMP_TOL_K or temp > tank.t_max_c + _TEMP_TOL_K:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.TANK_COMFORT,
                        f"tank at {temp:.1f} C, band {tank.t_min_c:g}-{tank.t_max_c:g} C",
                        t,
                    )
                )
        if temps and temps[-1] < tank.t_initial_c - _TEMP_TOL_K:
            found.append(
                ApplianceViolation(
                    ApplianceRule.TANK_END,
                    f"tank ends at {temps[-1]:.1f} C, colder than the start {tank.t_initial_c:g} C",
                )
            )

    building = inputs.building
    if building is not None:
        cop = inputs.cop
        for t, kw in enumerate(plan.heatpump_kw):
            limit = building.max_heat_kw / cop[t]
            if kw < -_POWER_TOL_KW or kw > limit + _POWER_TOL_KW:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.HEATPUMP_POWER,
                        f"heat pump {kw:.3f} kW outside 0..{limit:.3f} kW",
                        t,
                    )
                )
        indoor = simulate_building(inputs, plan.heatpump_kw)
        for t, temp in enumerate(indoor):
            if temp < building.t_min_c - _TEMP_TOL_K or temp > building.t_max_c + _TEMP_TOL_K:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.INDOOR_COMFORT,
                        f"indoor {temp:.1f} C, band {building.t_min_c:g}-{building.t_max_c:g} C",
                        t,
                    )
                )
        if indoor and indoor[-1] < building.t_initial_c - _TEMP_TOL_K:
            found.append(
                ApplianceViolation(
                    ApplianceRule.INDOOR_END,
                    f"indoor ends at {indoor[-1]:.1f} C, colder than the start "
                    f"{building.t_initial_c:g} C",
                )
            )

    battery = inputs.battery
    if battery is not None:
        found += _check_battery(inputs, plan)

    if inputs.max_grid_kw is not None:
        net = battery_net_kw(inputs, plan)
        for t, kw in enumerate(appliance_kw(inputs, plan)):
            draw = min_import_kw(inputs, t, kw, net[t])
            if draw > inputs.max_grid_kw + _POWER_TOL_KW:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.GRID_LIMIT,
                        f"{draw:.2f} kW from the grid exceeds the {inputs.max_grid_kw:g} kW limit",
                        t,
                    )
                )
    return found


def verify_appliance_plan(
    inputs: DayInputs, plan: AppliancePlan, *, compare_with_baseline: bool = True
) -> ApplianceReport:
    """Check ``plan`` against ``inputs``. Never raises for a bad plan; it reports."""
    if not plan.feasible:
        return ApplianceReport(
            (ApplianceViolation(ApplianceRule.NO_SCHEDULE, plan.message or "no schedule"),)
        )
    found = _hard_checks(inputs, plan)
    if found and found[0].rule in (ApplianceRule.SHAPE, ApplianceRule.NOT_FINITE):
        return ApplianceReport(tuple(found))

    cost = plan_bill(inputs, plan)
    baseline_cost: float | None = None
    if compare_with_baseline:
        base = run_immediately(inputs)
        if not _hard_checks(inputs, base):  # only meaningful if the baseline is itself valid
            baseline_cost = plan_bill(inputs, base)
            if cost > baseline_cost * (1 + _COST_REL_TOL) + _COST_ABS_TOL_EUR:
                found.append(
                    ApplianceViolation(
                        ApplianceRule.COST,
                        f"cost {cost:.4f} EUR exceeds baseline {baseline_cost:.4f} EUR",
                    )
                )
    return ApplianceReport(tuple(found), cost, baseline_cost)
