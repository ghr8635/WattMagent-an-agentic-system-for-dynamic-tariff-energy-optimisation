"""Replanning every hour as the day unfolds (a rolling horizon).

At the start of each hour the planner knows what has really happened so far (the sun that
actually shone, the state its own earlier decisions left the tank, house and battery in) and
plans the rest of the day again, keeping the hours already lived through fixed
(``Committed``). Two things follow, and the tests pin both:

- with **no new information** replanning changes nothing: an optimal plan stays optimal as the
  clock advances, so the cost must equal the one-shot plan. Replanning only pays when the
  forecast improves;
- the forecast improves here through ``scale_to_observations``, a one-line nowcast.

Safety: every replan that fails (infeasible, or the solver stops) leaves the previous plan in
force, and the executed day is checked by the independent verifier against what really happened.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from wattmagent.appliances.models import AppliancePlan, Committed, DayInputs
from wattmagent.appliances.optimiser import ApplianceOptimiserError, plan_appliances
from wattmagent.data.forecast import scale_to_observations


@dataclass(frozen=True, slots=True)
class ReplanResult:
    plan: AppliancePlan  # the day as executed: the plan after the last successful replan
    solves: int  # optimiser runs (the first plan plus every replan that was needed)
    failed: int  # replans that could not be completed and left the previous plan in force


def _same(a: Sequence[float], b: Sequence[float]) -> bool:
    return all(abs(x - y) <= 1e-9 for x, y in zip(a, b, strict=True))


def replan_day(
    actual: DayInputs,
    forecast_pv_kw: Sequence[float],
    *,
    nowcast: bool = True,
    pv_ceiling_kw: float | None = None,
) -> ReplanResult:
    """Run one day with a replan at the start of every hour after the first.

    ``actual`` is the world as it turns out (its ``pv_kw`` is the real solar power);
    ``forecast_pv_kw`` is what was known when the day began. Without ``nowcast`` the rest of the
    day keeps the original forecast; with it the forecast is rescaled to how the day is going.
    """
    n = actual.slots
    if len(forecast_pv_kw) != n:
        raise ValueError("the forecast must have one value per slot")
    assumed = list(forecast_pv_kw)
    plan = plan_appliances(replace(actual, pv_kw=assumed))
    if not plan.feasible:
        return ReplanResult(plan, 1, 0)
    solves, failed = 1, 0
    for h in range(1, n):
        observed = list(actual.pv_kw[:h])
        if nowcast:
            seen = scale_to_observations(forecast_pv_kw, observed, ceiling=pv_ceiling_kw)
        else:
            seen = observed + list(forecast_pv_kw[h:])
        if _same(seen[h:], assumed[h:]):  # nothing new about the rest of the day: plan stands
            continue
        solves += 1
        try:
            candidate = plan_appliances(replace(actual, pv_kw=seen), committed=Committed(h, plan))
        except ApplianceOptimiserError:
            failed += 1
            continue
        if candidate.feasible:
            plan, assumed = candidate, seen
        else:
            failed += 1
    return ReplanResult(plan, solves, failed)
