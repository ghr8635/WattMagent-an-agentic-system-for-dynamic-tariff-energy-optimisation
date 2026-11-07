"""Rule-based planners: the floors the optimiser has to beat.

- ``run_immediately``: every cycle at its earliest start, tank on a simple thermostat;
- ``cheapest_start``: every cycle at the cheapest start for that cycle alone (ignores the
  grid limit and other cycles), tank on the same thermostat, heat pump on the thrifty one.

The thermostat is an hourly approximation of a real one: heat when the water is in the
lower quarter of the band, or when it would otherwise leave the band, and never above the
ceiling. In the last slot it tops up so the tank does not end colder than it began.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from wattmagent.appliances.models import (
    AppliancePlan,
    CycleJob,
    DayInputs,
    building_step,
    tank_step,
)
from wattmagent.appliances.simulate import place
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
    return AppliancePlan(
        PlanStatus.RULE, cycles, thermostat(inputs), heatpump_thermostat(inputs, thrifty=thrifty)
    )


def run_immediately(inputs: DayInputs) -> AppliancePlan:
    return _plan(inputs, lambda job: job.earliest)


def cheapest_start(inputs: DayInputs) -> AppliancePlan:
    def start(job: CycleJob) -> int:
        profile = job.spec.profile_kw

        def cost(s: int) -> float:
            return sum(inputs.prices[s + i] * kw for i, kw in enumerate(profile))

        best = job.earliest
        for s in job.starts:
            if cost(s) < cost(best) - _EPS:
                best = s
        return best

    return _plan(inputs, start, thrifty=True)
