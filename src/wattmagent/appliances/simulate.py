"""What a plan does: power per slot, tank temperatures, cost. Shared by every checker."""

from __future__ import annotations

import math
from collections.abc import Sequence

from wattmagent.appliances.models import AppliancePlan, DayInputs, building_step, tank_step


def place(profile: Sequence[float], start: int, slots: int) -> list[float]:
    """The profile laid into a day of ``slots`` slots, starting at ``start``."""
    out = [0.0] * slots
    for i, kw in enumerate(profile):
        if 0 <= start + i < slots:
            out[start + i] = kw
    return out


def appliance_kw(inputs: DayInputs, plan: AppliancePlan) -> list[float]:
    """Total appliance power per slot (cycles plus tank), without the base load."""
    n = inputs.slots
    total = [0.0] * n
    for series in plan.cycle_kw.values():
        for t in range(min(n, len(series))):
            total[t] += series[t]
    for t in range(min(n, len(plan.tank_kw))):
        total[t] += plan.tank_kw[t]
    for t in range(min(n, len(plan.heatpump_kw))):
        total[t] += plan.heatpump_kw[t]
    return total


def simulate_building(inputs: DayInputs, heatpump_kw: Sequence[float]) -> list[float]:
    """Indoor temperature at the end of each slot. Empty if the day has no building."""
    spec = inputs.building
    if spec is None:
        return []
    cop = inputs.cop
    temp = spec.t_initial_c
    out: list[float] = []
    for t in range(inputs.slots):
        kw = heatpump_kw[t] if t < len(heatpump_kw) else 0.0
        temp = building_step(spec, temp, kw * cop[t], inputs.outdoor_c[t])
        out.append(temp)
    return out


def simulate_tank(inputs: DayInputs, tank_kw: Sequence[float]) -> list[float]:
    """Temperature at the end of each slot. Empty if the day has no tank."""
    spec = inputs.tank
    if spec is None:
        return []
    temp = spec.t_initial_c
    out: list[float] = []
    for t in range(inputs.slots):
        kw = tank_kw[t] if t < len(tank_kw) else 0.0
        temp = tank_step(spec, temp, kw, inputs.tank_draw_kwh[t])
        out.append(temp)
    return out


def cost_eur(prices: Sequence[float], kw: Sequence[float]) -> float:
    """Energy of the given power series (1 h slots) on ``prices``."""
    return math.fsum(p * k for p, k in zip(prices, kw, strict=True))
