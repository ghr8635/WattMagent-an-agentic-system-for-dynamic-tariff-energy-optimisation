"""Mixed-integer linear programme for shiftable cycles and a hot-water tank.

Unknowns

- ``y[j, s]``  1 if cycle job ``j`` starts in slot ``s`` (binary; one start per job)
- ``z[t]``     element duty in slot ``t`` (binary for an on/off element, 0..1 if modulating)
- ``T[t]``     tank temperature at the end of slot ``t`` (inside the comfort band)
- ``Q[t]``, ``Tb[t]``  heat-pump heat (thermal kW, 0..max) and indoor temperature (comfort band)
- ``g[t]``, ``x[t]``, ``c[t]``  grid import, export and curtailed solar (only with solar):
  ``g - x - c = base + appliances - solar``, cost ``price g - export_value x``

Model

    minimise   sum_{j,s} y[j,s] * (sum_i price[s+i] * profile_j[i])  +  sum_t price[t] * P * z[t]
    subject to sum_s y[j,s] = 1                                          (each cycle runs once)
               T[t] = a T[t-1] + (eta P / C) z[t] + (loss * ambient - draw[t]) / C
               T[last] >= T_initial                                      (no free heat from the end)
               base[t] + cycle power[t] + P z[t] <= max_grid_kw          (if a limit is set)

    Tb[t] = (1 - UA/Cb) Tb[t-1] + Q[t]/Cb + UA T_out[t]/Cb,  cost price[t] * Q[t] / COP[t]

(COP depends on the known outdoor temperature, so this stays linear; the heat-pump part is a
plain LP). With ``a = 1 - loss / C`` and ``C`` the tank heat capacity. A cycle is one block
of ``y``, so it cannot be paused or split: that is what "shiftable cycle" means. Without solar,
base load is a constant, left out of the cost, and matters only for the connection limit. With
solar the cost is the household's whole net grid bill, base load included.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from wattmagent.appliances.models import AppliancePlan, DayInputs
from wattmagent.appliances.simulate import place
from wattmagent.core.errors import WattMagentError
from wattmagent.planning.household import PlanStatus

_TIME_LIMIT_S = 10.0
_NOISE = 1e-7


class ApplianceOptimiserError(WattMagentError):
    """The solver failed for a reason other than an infeasible request."""


def plan_appliances(inputs: DayInputs) -> AppliancePlan:
    n = inputs.slots
    prices = np.asarray(inputs.prices, dtype=float)
    tank = inputs.tank

    # Column layout: one y column per (job, start), then z (n), then T (n) if there is a tank.
    y_cols: list[tuple[int, int]] = [
        (j, s) for j, job in enumerate(inputs.jobs) for s in job.starts
    ]
    n_y = len(y_cols)
    z0 = n_y
    t0 = z0 + n
    nv = n_y + (2 * n if tank else 0)
    building = inputs.building
    cop = inputs.cop
    q0 = nv  # heat-pump thermal power (n), then indoor temperature (n)
    b0 = q0 + n
    if building is not None:
        nv += 2 * n
    solar = inputs.has_solar
    g0 = nv  # grid import (n), export (n), curtailment (n): only with solar
    x0 = g0 + n
    c0 = x0 + n
    if solar:
        nv += 3 * n
    if nv == 0:
        return AppliancePlan(PlanStatus.OPTIMAL, planned_cost_eur=0.0)

    cost = np.zeros(nv)
    lb = np.zeros(nv)
    ub = np.ones(nv)
    integrality = np.zeros(nv)
    integrality[:n_y] = 1
    for k, (j, s) in enumerate(y_cols):
        profile = inputs.jobs[j].spec.profile_kw
        cost[k] = float(np.dot(prices[s : s + len(profile)], profile))

    def load_row(t: int) -> np.ndarray:
        """Appliance power in slot ``t`` as a row over the variables."""
        row = np.zeros(nv)
        for k, (j, s) in enumerate(y_cols):
            i = t - s
            profile = inputs.jobs[j].spec.profile_kw
            if 0 <= i < len(profile):
                row[k] = profile[i]
        if tank is not None:
            row[z0 + t] = tank.power_kw
        if building is not None:
            row[q0 + t] = 1.0 / cop[t]
        return row

    if building is not None:
        cost[q0 : q0 + n] = prices / np.asarray(cop)
        ub[q0 : q0 + n] = building.max_heat_kw
        lb[b0 : b0 + n] = building.t_min_c
        ub[b0 : b0 + n] = building.t_max_c
        lb[b0 + n - 1] = max(building.t_min_c, building.t_initial_c)

    eq_rows: list[np.ndarray] = []
    eq_rhs: list[float] = []
    for j in range(len(inputs.jobs)):
        row = np.zeros(nv)
        for k, (jj, _) in enumerate(y_cols):
            if jj == j:
                row[k] = 1.0
        eq_rows.append(row)
        eq_rhs.append(1.0)

    ub_rows: list[np.ndarray] = []
    ub_rhs: list[float] = []
    p_tank = 0.0
    if tank is not None:
        p_tank = tank.power_kw
        cap = tank.heat_capacity_kwh_per_k
        a = 1.0 - tank.loss_kw_per_k / cap
        cost[z0 : z0 + n] = prices * p_tank
        if not tank.modulating:
            integrality[z0 : z0 + n] = 1
        lb[t0 : t0 + n] = tank.t_min_c
        ub[t0 : t0 + n] = tank.t_max_c
        lb[t0 + n - 1] = max(tank.t_min_c, tank.t_initial_c)
        for t in range(n):
            row = np.zeros(nv)
            row[t0 + t] = 1.0
            if t > 0:
                row[t0 + t - 1] = -a
            row[z0 + t] = -tank.efficiency * p_tank / cap
            rhs = (tank.loss_kw_per_k * tank.ambient_c - inputs.tank_draw_kwh[t]) / cap
            if t == 0:
                rhs += a * tank.t_initial_c
            eq_rows.append(row)
            eq_rhs.append(rhs)

    if building is not None:
        c_b = building.heat_capacity_kwh_per_k
        a_b = 1.0 - building.ua_kw_per_k / c_b
        for t in range(n):
            row = np.zeros(nv)
            row[b0 + t] = 1.0
            if t > 0:
                row[b0 + t - 1] = -a_b
            row[q0 + t] = -1.0 / c_b
            rhs = building.ua_kw_per_k * inputs.outdoor_c[t] / c_b
            if t == 0:
                rhs += a_b * building.t_initial_c
            eq_rows.append(row)
            eq_rhs.append(rhs)

    base = inputs.base_kw
    if solar:
        pv = inputs.pv
        export = inputs.export_value
        cost[:g0] = 0.0  # appliance energy is paid for through the grid import below
        cost[g0 : g0 + n] = prices
        cost[x0 : x0 + n] = -np.asarray(export)
        ub[g0 : g0 + n] = inputs.max_grid_kw if inputs.max_grid_kw is not None else np.inf
        ub[x0 : x0 + n] = pv
        ub[c0 : c0 + n] = pv
        for t in range(n):  # grid import - export - curtailed solar = demand - solar
            row = -load_row(t)
            row[g0 + t] = 1.0
            row[x0 + t] = -1.0
            row[c0 + t] = -1.0
            eq_rows.append(row)
            eq_rhs.append(base[t] - pv[t])
    elif inputs.max_grid_kw is not None:
        for t in range(n):
            ub_rows.append(load_row(t))
            ub_rhs.append(inputs.max_grid_kw - base[t])

    constraints = []
    if eq_rows:
        e = np.asarray(eq_rhs)
        constraints.append(LinearConstraint(np.vstack(eq_rows), e, e))
    if ub_rows:
        constraints.append(LinearConstraint(np.vstack(ub_rows), -np.inf, np.asarray(ub_rhs)))

    res = milp(
        cost,
        constraints=constraints,
        bounds=Bounds(lb, ub),
        integrality=integrality,
        options={"time_limit": _TIME_LIMIT_S, "mip_rel_gap": 1e-6},
    )
    if res.status == 2:
        return AppliancePlan(
            PlanStatus.INFEASIBLE,
            message="no feasible schedule: check deadlines, the comfort band and the grid limit",
        )
    if not res.success or res.x is None:
        raise ApplianceOptimiserError(f"solver stopped without an optimal plan: {res.message}")

    sol = np.asarray(res.x, dtype=float)
    chosen: dict[int, int] = {}
    for k, (j, s) in enumerate(y_cols):
        if sol[k] > 0.5:
            chosen[j] = s
    cycle_kw = {
        job.spec.name: place(job.spec.profile_kw, chosen[j], n) for j, job in enumerate(inputs.jobs)
    }
    tank_kw: list[float] = []
    if tank is not None:
        duty = sol[z0 : z0 + n]
        if not tank.modulating:
            duty = np.round(duty)
        tank_kw = [0.0 if abs(float(d)) < _NOISE else float(d) * p_tank for d in duty]
    heatpump_kw: list[float] = []
    if building is not None:
        heat = np.clip(sol[q0 : q0 + n], 0.0, building.max_heat_kw)
        heatpump_kw = [0.0 if h < _NOISE else float(h) / cop[t] for t, h in enumerate(heat)]
    return AppliancePlan(
        PlanStatus.OPTIMAL, cycle_kw, tank_kw, heatpump_kw, planned_cost_eur=float(res.fun)
    )
