"""What a plan does: power per slot, tank temperatures, cost. Shared by every checker."""

from __future__ import annotations

import math
from collections.abc import Sequence

from wattmagent.appliances.models import (
    AppliancePlan,
    DayInputs,
    battery_step,
    building_step,
    tank_step,
)


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


def simulate_battery(
    inputs: DayInputs, charge_kw: Sequence[float], discharge_kw: Sequence[float]
) -> list[float]:
    """Stored energy (kWh) at the end of each slot. Empty if the day has no battery."""
    spec = inputs.battery
    if spec is None:
        return []
    soc = spec.soc_initial_kwh
    out: list[float] = []
    for t in range(inputs.slots):
        ch = charge_kw[t] if t < len(charge_kw) else 0.0
        dis = discharge_kw[t] if t < len(discharge_kw) else 0.0
        soc = battery_step(spec, soc, ch, dis)
        out.append(soc)
    return out


def battery_net_kw(inputs: DayInputs, plan: AppliancePlan) -> list[float]:
    """Battery power the household draws per slot: charging minus discharging (AC side)."""
    n = inputs.slots
    ch, dis = plan.battery_charge_kw, plan.battery_discharge_kw
    return [(ch[t] if t < len(ch) else 0.0) - (dis[t] if t < len(dis) else 0.0) for t in range(n)]


def plan_bill(inputs: DayInputs, plan: AppliancePlan) -> float:
    """What ``plan`` costs on ``inputs.prices``, battery included (see ``plan_cost``)."""
    return plan_cost(
        inputs, appliance_kw(inputs, plan), plan.battery_charge_kw, plan.battery_discharge_kw
    )


def plan_cost(
    inputs: DayInputs,
    kw: Sequence[float],
    battery_charge_kw: Sequence[float] = (),
    battery_discharge_kw: Sequence[float] = (),
) -> float:
    """Cost of the day for appliance power ``kw`` (and a battery dispatch, if any).

    Without solar or a battery this is the appliance energy on the prices (base load is a
    constant and left out). Otherwise it is the household's whole net grid bill: base load
    included, own solar used first, the surplus exported at the export value, or curtailed when
    that is worth nothing or negative. Per slot the cheapest split of the solar between self-use
    and curtailment is taken, which is what the optimiser does too. A battery adds its charging
    to the demand and takes its discharging off, and every kWh discharged carries the wear cost.
    """
    if not inputs.has_grid_balance:
        return cost_eur(inputs.prices, kw)
    total = 0.0
    base, pv, export = inputs.base_kw, inputs.pv, inputs.export_value
    for t, price in enumerate(inputs.prices):
        ch = battery_charge_kw[t] if t < len(battery_charge_kw) else 0.0
        dis = battery_discharge_kw[t] if t < len(battery_discharge_kw) else 0.0
        demand = base[t] + (kw[t] if t < len(kw) else 0.0) + ch - dis
        best = math.inf
        for used in {0.0, min(pv[t], demand), pv[t]}:
            net = demand - used
            slot = price * max(0.0, net) - export[t] * max(0.0, -net)
            best = min(best, slot)
        total += best
        if inputs.battery is not None:
            total += inputs.battery.wear_eur_per_kwh * dis
    return total


def min_import_kw(
    inputs: DayInputs, t: int, appliance_power_kw: float, battery_net_kw_: float = 0.0
) -> float:
    """Smallest grid import possible in slot ``t`` (all solar used)."""
    return max(0.0, inputs.base_kw[t] + appliance_power_kw + battery_net_kw_ - inputs.pv[t])


def cost_eur(prices: Sequence[float], kw: Sequence[float]) -> float:
    """Energy of the given power series (1 h slots) on ``prices``."""
    return math.fsum(p * k for p, k in zip(prices, kw, strict=True))
