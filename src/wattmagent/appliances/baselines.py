"""Rule-based planners: the floors the optimiser has to beat.

- ``run_immediately``: every cycle at its earliest start, tank on a simple thermostat;
- ``self_consumption``: ``run_immediately`` plus a battery that stores solar surplus and covers
  the house when short, ignoring prices (only differs from it when there is a battery);
- ``cheapest_start``: every cycle at the cheapest start for that cycle alone (on the net bill
  when there is solar; ignores the grid limit and other cycles), tank on the same thermostat,
  heat pump on the thrifty one.

The thermostat is an hourly approximation of a real one: heat when the water is in the
lower quarter of the band, or when it would otherwise leave the band, and never above the
ceiling. In the last slot it tops up so the tank does not end colder than it began.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

from wattmagent.appliances.models import (
    AppliancePlan,
    CycleJob,
    DayInputs,
    battery_step,
    building_step,
    tank_step,
)
from wattmagent.appliances.simulate import appliance_kw, place, plan_cost
from wattmagent.planning.household import PlanStatus

_EPS = 1e-9


def thermostat(inputs: DayInputs) -> list[float]:
    spec = inputs.tank
    if spec is None:
        return []
    n = inputs.slots
    low = spec.t_min_c + 0.25 * (spec.t_max_c - spec.t_min_c)
    target = 0.5 * (spec.t_min_c + spec.t_max_c)
    temp = spec.t_initial_c
    out: list[float] = []
    for t in range(n):
        draw = inputs.tank_draw_kwh[t]
        off = tank_step(spec, temp, 0.0, draw)
        floor = spec.t_initial_c if t == n - 1 else spec.t_min_c
        want = temp < low or off < floor - _EPS
        kw = 0.0
        if want:
            if spec.modulating:
                need = (target - off) * spec.heat_capacity_kwh_per_k / spec.efficiency
                kw = min(spec.power_kw, max(need, 0.0))
                if off < floor:  # never plan less than what keeps the floor
                    kw = max(
                        kw,
                        min(
                            spec.power_kw,
                            (floor - off) * spec.heat_capacity_kwh_per_k / spec.efficiency,
                        ),
                    )
            else:
                on = tank_step(spec, temp, spec.power_kw, draw)
                kw = spec.power_kw if (on <= spec.t_max_c + _EPS or off < floor - _EPS) else 0.0
        out.append(kw)
        temp = tank_step(spec, temp, kw, draw)
    return out


def heatpump_thermostat(inputs: DayInputs, *, thrifty: bool = False) -> list[float]:
    """Electric power per slot of a pump that holds the room at a fixed level.

    Each hour it delivers the heat that brings the room to the target, within its limits.
    The ordinary thermostat holds the start temperature (or the middle of the band if that
    is lower). The ``thrifty`` one holds the lower quarter of the band. A colder house loses
    less heat, so the optimiser's gain must be judged against the thrifty one, otherwise part
    of it would just be running cooler. In the last hours (enough to climb back) the target
    is the start temperature, so the building does not end colder than it began.
    """
    spec = inputs.building
    if spec is None:
        return []
    cop = inputs.cop
    n = inputs.slots
    mid = 0.5 * (spec.t_min_c + spec.t_max_c)
    hold_c = (
        spec.t_min_c + 0.25 * (spec.t_max_c - spec.t_min_c)
        if thrifty
        else min(mid, spec.t_initial_c)
    )
    # hours the pump needs to climb from the hold level back to the start temperature
    climb_kwh = max(0.0, spec.t_initial_c - hold_c) * spec.heat_capacity_kwh_per_k
    ramp = min(n, math.ceil(climb_kwh / spec.max_heat_kw) + 2)
    temp = spec.t_initial_c
    out: list[float] = []
    for t in range(n):
        off = building_step(spec, temp, 0.0, inputs.outdoor_c[t])
        target = spec.t_initial_c if t >= n - ramp else hold_c  # climb back before the end
        heat = (target - off) * spec.heat_capacity_kwh_per_k
        # never overshoot the ceiling, never ask for more than the pump can give
        ceiling = (spec.t_max_c - off) * spec.heat_capacity_kwh_per_k
        heat = min(spec.max_heat_kw, max(0.0, min(heat, ceiling)))
        out.append(heat / cop[t])
        temp = building_step(spec, temp, heat, inputs.outdoor_c[t])
    return out


def _plan(
    inputs: DayInputs, start_of: Callable[[CycleJob], int], *, thrifty: bool = False
) -> AppliancePlan:
    n = inputs.slots
    cycles = {j.spec.name: place(j.spec.profile_kw, start_of(j), n) for j in inputs.jobs}
    idle = [0.0] * n if inputs.battery is not None else []  # these rules leave the battery alone
    return AppliancePlan(
        PlanStatus.RULE,
        cycles,
        thermostat(inputs),
        heatpump_thermostat(inputs, thrifty=thrifty),
        battery_charge_kw=list(idle),
        battery_discharge_kw=list(idle),
    )


def run_immediately(inputs: DayInputs) -> AppliancePlan:
    return _plan(inputs, lambda job: job.earliest)


def cheapest_start(inputs: DayInputs) -> AppliancePlan:
    def start(job: CycleJob) -> int:
        profile = job.spec.profile_kw

        def cost(s: int) -> float:
            if inputs.has_grid_balance:  # judge the start on the net bill, so a sunny hour counts
                return plan_cost(inputs, place(profile, s, inputs.slots))
            return sum(inputs.prices[s + i] * kw for i, kw in enumerate(profile))

        best = job.earliest
        for s in job.starts:
            if cost(s) < cost(best) - _EPS:
                best = s
        return best

    return _plan(inputs, start, thrifty=True)


def self_consumption(inputs: DayInputs) -> AppliancePlan:
    """What a typical home battery does: store solar surplus, cover the house when short.

    The appliances run as in ``run_immediately``, so the difference to that baseline is the
    battery alone. The battery charges only from surplus solar and discharges only to cover
    the house's own demand, never below the level it started the day with (so every day ends
    no emptier than it began, the same rule the optimiser obeys). It ignores prices.
    """
    plan = run_immediately(inputs)
    battery = inputs.battery
    if battery is None:
        return plan
    n = inputs.slots
    appliances = appliance_kw(inputs, plan)
    base, pv = inputs.base_kw, inputs.pv
    floor = max(battery.soc_min_kwh, battery.soc_initial_kwh)
    stored = battery.soc_initial_kwh
    charge: list[float] = []
    discharge: list[float] = []
    for t in range(n):
        net = base[t] + appliances[t] - pv[t]  # positive: the house needs grid power
        ch = dis = 0.0
        if net < 0:  # surplus solar
            room = (battery.capacity_kwh - stored) / battery.charge_efficiency
            ch = min(-net, battery.max_charge_kw, max(room, 0.0))
        elif net > 0:
            available = max(stored - floor, 0.0) * battery.discharge_efficiency
            dis = min(net, battery.max_discharge_kw, available)
        stored = battery_step(battery, stored, ch, dis)
        charge.append(ch)
        discharge.append(dis)
    return replace(plan, battery_charge_kw=charge, battery_discharge_kw=discharge)
