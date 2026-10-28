"""Mixed-integer linear programme for one planning day: EV plus home battery.

Per hourly slot ``t`` the unknowns are

- ``g``  grid import (kW, >= 0; no export is modelled)
- ``c``  battery charge, ``d`` battery discharge (kW, AC side)
- ``x``  EV charging (kW, grid side)
- ``s``  battery energy at the end of the slot (kWh)
- ``b``  1 if the battery may charge in the slot, 0 if it may discharge (binary)

and the model is

    minimise   sum_t price_t * g_t  +  wear * sum_t d_t
    subject to g_t + d_t - c_t - x_t = load_t                       (balance)
               s_t = s_{t-1} + eta_c * c_t - d_t / eta_d            (battery)
               sum_t eta_ev * x_t = energy the EV needs              (reach target, no more)
               0 <= c_t <= P * b_t,  0 <= d_t <= P * (1 - b_t)       (not both at once)
               x_t <= P_ev while plugged in, 0 otherwise
               s_min <= s_t <= s_max,  s_last >= s_initial           (no free energy from the end)

``b`` is needed because with negative prices charging and discharging in the same
hour could look profitable. The objective uses the prices the planner is given,
which may be forecasts; billing is done elsewhere on actual prices.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from wattmagent.core.errors import WattMagentError
from wattmagent.planning.household import HouseholdInputs, HouseholdPlan, PlanStatus

_TIME_LIMIT_S = 10.0
_NOISE = 1e-7  # solver output below this is rounding noise
_INFEASIBLE_TOL = 1e-6


class OptimiserError(WattMagentError):
    """The solver failed for a reason other than an infeasible request."""


def _clean(values: np.ndarray) -> list[float]:
    return [0.0 if abs(float(v)) < _NOISE else float(v) for v in values]


def plan_household(inputs: HouseholdInputs) -> HouseholdPlan:
    n = inputs.slots
    ev = inputs.ev
    bat = inputs.battery

    need_stored = max(0.0, (ev.target_soc - ev.arrival_soc) * ev.capacity_kwh)
    plugged = sum(inputs.ev_available)
    max_stored = plugged * ev.max_charge_kw * ev.efficiency
    if need_stored > max_stored + _INFEASIBLE_TOL:
        reachable = ev.arrival_soc + max_stored / ev.capacity_kwh
        return HouseholdPlan(
            PlanStatus.INFEASIBLE,
            message=(
                f"EV target {ev.target_soc:.0%} cannot be reached in {plugged} plugged-in "
                f"hours at {ev.max_charge_kw:g} kW; at most {reachable:.0%} is reachable"
            ),
        )

    # Variable blocks: g, c, d, x, s, b (each of length n).
    g, c, d, x, s, b = (slice(k * n, (k + 1) * n) for k in range(6))
    nv = 6 * n

    cost = np.zeros(nv)
    cost[g] = np.asarray(inputs.prices, dtype=float)
    p_bat = bat.max_power_kw if bat else 0.0
    if bat:
        cost[d] = bat.wear_eur_per_kwh

    lb = np.zeros(nv)
    ub = np.full(nv, np.inf)
    ub[c] = p_bat
    ub[d] = p_bat
    ub[x] = [ev.max_charge_kw if ok else 0.0 for ok in inputs.ev_available]
    ub[b] = 1.0 if bat else 0.0
    if bat:
        lb[s], ub[s] = bat.min_kwh, bat.max_kwh
        lb[s.stop - 1] = max(bat.min_kwh, bat.initial_kwh)  # end no lower than the start
    else:
        ub[s] = 0.0

    eta_c = bat.charge_efficiency if bat else 1.0
    eta_d = bat.discharge_efficiency if bat else 1.0
    s_init = bat.initial_kwh if bat else 0.0

    # Equalities: balance (n), battery dynamics (n), EV energy (1).
    a_eq = np.zeros((2 * n + 1, nv))
    b_eq = np.zeros(2 * n + 1)
    for t in range(n):
        a_eq[t, g.start + t] = 1.0
        a_eq[t, d.start + t] = 1.0
        a_eq[t, c.start + t] = -1.0
        a_eq[t, x.start + t] = -1.0
        b_eq[t] = inputs.load_kw[t]

        row = n + t
        a_eq[row, s.start + t] = 1.0
        if t > 0:
            a_eq[row, s.start + t - 1] = -1.0
        a_eq[row, c.start + t] = -eta_c
        a_eq[row, d.start + t] = 1.0 / eta_d
        b_eq[row] = s_init if t == 0 else 0.0
    a_eq[2 * n, x] = ev.efficiency
    b_eq[2 * n] = need_stored

    # Inequalities: c <= P*b and d <= P*(1-b)  (the second written as d + P*b <= P).
    a_ub = np.zeros((2 * n, nv))
    b_ub = np.zeros(2 * n)
    for t in range(n):
        a_ub[t, c.start + t] = 1.0
        a_ub[t, b.start + t] = -p_bat
        a_ub[n + t, d.start + t] = 1.0
        a_ub[n + t, b.start + t] = p_bat
        b_ub[n + t] = p_bat

    integrality = np.zeros(nv)
    integrality[b] = 1

    res = milp(
        cost,
        constraints=[
            LinearConstraint(a_eq, b_eq, b_eq),
            LinearConstraint(a_ub, -np.inf, b_ub),
        ],
        bounds=Bounds(lb, ub),
        integrality=integrality,
        options={"time_limit": _TIME_LIMIT_S, "mip_rel_gap": 1e-6},
    )
    if res.status == 2:  # solver proves infeasibility the pre-check could not see
        return HouseholdPlan(PlanStatus.INFEASIBLE, message="no feasible schedule exists")
    if not res.success or res.x is None:
        raise OptimiserError(f"solver stopped without an optimal plan: {res.message}")

    sol = np.asarray(res.x, dtype=float)
    battery_kw = _clean(sol[c] - sol[d]) if bat else [0.0] * n
    return HouseholdPlan(
        PlanStatus.OPTIMAL, _clean(sol[x]), battery_kw, planned_cost_eur=float(res.fun)
    )
