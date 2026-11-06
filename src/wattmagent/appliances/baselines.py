"""Rule-based planners: the floors the optimiser has to beat.

- ``run_immediately``: every cycle at its earliest start, tank on a simple thermostat;
- ``cheapest_start``: every cycle at the cheapest start for that cycle alone (ignores the
  grid limit and other cycles), tank on the same thermostat.

The thermostat is an hourly approximation of a real one: heat when the water is in the
lower quarter of the band, or when it would otherwise leave the band, and never above the
ceiling. In the last slot it tops up so the tank does not end colder than it began.
"""

from __future__ import annotations

from collections.abc import Callable

from wattmagent.appliances.models import AppliancePlan, CycleJob, DayInputs, tank_step
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


def _plan(inputs: DayInputs, start_of: Callable[[CycleJob], int]) -> AppliancePlan:
    n = inputs.slots
    cycles = {j.spec.name: place(j.spec.profile_kw, start_of(j), n) for j in inputs.jobs}
    return AppliancePlan(PlanStatus.RULE, cycles, thermostat(inputs))


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

    return _plan(inputs, start)
