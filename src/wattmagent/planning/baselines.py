"""Baseline charging planners.

A planner maps (hourly prices it is allowed to see, EV spec) to a kW-per-hour
schedule of the same length. Baselines bracket the problem:

- ``charge_immediately``: what most people do today. The "no optimisation" floor.
- ``cheapest_hours``: with a linear cost and a power cap, filling the cheapest
  hours first is optimal for a single EV *if the prices are known in advance*.
  Evaluated on actual prices it is the perfect-hindsight lower bound that real
  (forecast-based) planners can only approach.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from wattmagent.devices.ev import EVSpec

Planner = Callable[[Sequence[float], EVSpec], list[float]]

_TOL = 1e-9


def charge_immediately(prices: Sequence[float], spec: EVSpec) -> list[float]:
    need = spec.grid_energy_needed_kwh
    schedule = [0.0] * len(prices)
    for i in range(len(prices)):
        if need <= _TOL:
            break
        kw = min(spec.max_charge_kw, need)
        schedule[i] = kw
        need -= kw
    return schedule


def cheapest_hours(prices: Sequence[float], spec: EVSpec) -> list[float]:
    need = spec.grid_energy_needed_kwh
    schedule = [0.0] * len(prices)
    for i in sorted(range(len(prices)), key=lambda j: (prices[j], j)):
        if need <= _TOL:
            break
        kw = min(spec.max_charge_kw, need)
        schedule[i] = kw
        need -= kw
    return schedule


BASELINES: dict[str, Planner] = {
    "charge_immediately": charge_immediately,
    "cheapest_hours (hindsight bound)": cheapest_hours,
}
