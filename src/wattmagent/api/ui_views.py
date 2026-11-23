"""What the web page shows: one planned day for the household, and the recent decisions.

Framework-free like ``service.py``, so it is tested without HTTP. Honesty rules for the page:

- The day is **planned, not measured**. The system has no meter data; the energy view is the
  optimiser's plan on the configured appliance figures, which are assumptions.
- Heat pump and solar are left out (they need a weather forecast, which is not verified), and the
  view says so.
- Prices are the day-ahead prices the service holds, wholesale only; stale or missing prices make
  the plan fall back, and the fallback is shown as a fallback.
- A view call is not an audit event (nothing is executed), so it writes no audit record.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from wattmagent.appliances.baselines import run_immediately
from wattmagent.appliances.household import Household, build_day_inputs
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.simulate import plan_bill, simulate_battery, simulate_tank
from wattmagent.core.errors import WattMagentError
from wattmagent.graph.context import AgentContext
from wattmagent.safety.freshness import PriceSnapshot, check_price_snapshot
from wattmagent.safety.pipeline import PlanRejected

MAX_DECISIONS = 100


class UiError(WattMagentError):
    """The page cannot be served right now. ``status_code`` is the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _r(values: Any, digits: int = 4) -> list[float]:
    return [round(float(v), digits) for v in values]


def _first_float(entry: dict[str, Any], *path: str) -> float | None:
    node: Any = entry
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return float(node) if isinstance(node, (int, float)) and not isinstance(node, bool) else None


def decision_rows(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Audit records reduced to what the page shows. The record holds numbers and rule names
    only; this keeps it that way even if a record grows new fields."""
    rows: list[dict[str, Any]] = []
    for e in entries:
        verifier = e.get("verifier")
        reasons = e.get("reasons")
        rows.append(
            {
                "at": str(e.get("at", "")),
                "kind": str(e.get("kind", "ev")),
                "request_id": str(e.get("request_id", ""))[:64],
                "decision": str(e.get("decision", "")),
                "outcome": str(e.get("outcome", "")),
                "request_satisfied": e.get("request_satisfied")
                if isinstance(e.get("request_satisfied"), bool)
                else None,
                "reasons": [str(r)[:160] for r in reasons[:5]] if isinstance(reasons, list) else [],
                "verifier_passed": verifier.get("passed") if isinstance(verifier, dict) else None,
                "rules": [str(r) for r in verifier.get("rules", [])[:8]]
                if isinstance(verifier, dict) and isinstance(verifier.get("rules"), list)
                else [],
                "cost_eur": _first_float(e, "plan", "verified_cost_eur"),
                "baseline_cost_eur": _first_float(e, "plan", "baseline_cost_eur"),
            }
        )
    return rows


def local_day_slots(day: date, tz: tzinfo) -> list[datetime]:
    """Start instants (UTC-aware) of every hour of the local calendar day: 23, 24 or 25 of them.
    Stepping in UTC keeps daylight-saving days right."""
    first = datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC)
    out: list[datetime] = []
    while (first + timedelta(hours=len(out))).astimezone(tz).date() == day:
        out.append(first + timedelta(hours=len(out)))
    return out


def build_day_view(
    household: Household, ctx: AgentContext, now: datetime, day: str = "today"
) -> dict[str, Any]:
    """Plan the household's local calendar day (midnight to midnight) and describe it for display.

    ``day`` is ``"today"`` or ``"tomorrow"``. The household file's appliance windows are local
    clock hours, so a whole local day is the natural unit; hours already past are marked
    ``elapsed``. Tomorrow needs its prices to be published: without them there is no view.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if day not in ("today", "tomorrow"):
        raise UiError("day must be 'today' or 'tomorrow'", 422)
    today = now.astimezone(ctx.tz).date()
    target = today if day == "today" else today + timedelta(days=1)
    starts = local_day_slots(target, ctx.tz)
    by_start = {p.start: p.eur_per_kwh for p in ctx.prices}
    if day == "tomorrow" and any(s not in by_start for s in starts):
        raise UiError("prices for tomorrow are not available yet", 404)
    prices = [by_start.get(s, 0.0) for s in starts]
    # no weather is verified, so the heat pump and solar are not part of the planned day
    plannable = replace(household, heatpump=None, solar=None)
    try:
        inputs = build_day_inputs(plannable, prices, [s.astimezone(ctx.tz).hour for s in starts])
    except ValueError as exc:
        raise UiError(f"this household cannot be planned for that day: {exc}", 422) from exc
    if not inputs.jobs and inputs.tank is None and inputs.battery is None:
        raise UiError("the household has no appliance, tank or battery to plan", 422)

    problems = check_price_snapshot(
        PriceSnapshot(ctx.prices, ctx.fetched_at),
        now=now,
        window_start=starts[0],
        slots=len(starts),
    )
    try:
        safe = plan_appliances_safely(
            inputs, request_id="ui-view", now=now, price_problems=problems
        )
    except PlanRejected as exc:
        raise UiError(f"no safe plan could be made: {exc}", 503) from exc
    plan = safe.plan
    baseline = run_immediately(inputs)
    optimised_eur, immediate_eur = plan_bill(inputs, plan), plan_bill(inputs, baseline)

    n = inputs.slots
    items: list[dict[str, Any]] = []
    for name, series in plan.cycle_kw.items():
        used = [t for t in range(min(n, len(series))) if series[t] > 1e-9]
        if not used:
            continue
        items.append(
            {
                "appliance": name.replace("_", " "),
                "start_at": starts[used[0]].astimezone(ctx.tz).isoformat(),
                "end_at": (starts[used[-1]] + timedelta(hours=1)).astimezone(ctx.tz).isoformat(),
                "energy_kwh": round(sum(series[t] for t in used), 3),
            }
        )
    items.sort(key=lambda i: i["start_at"])

    charge = list(plan.battery_charge_kw)
    discharge = list(plan.battery_discharge_kw)
    excluded = [
        label
        for label, present in (("heat pump", household.heatpump), ("solar", household.solar))
        if present is not None
    ]
    cycles = {name.replace("_", " "): _r(series[:n]) for name, series in plan.cycle_kw.items()}
    return {
        "generated_at": now.isoformat(),
        "day": day,
        "date": target.isoformat(),
        "timezone": str(ctx.tz),
        "price_basis": ctx.price_basis,
        "decision": safe.decision.value,
        "reasons": safe.reasons[:5],
        "request_satisfied": safe.request_satisfied,
        "slots": [
            {
                "start": s.astimezone(ctx.tz).isoformat(),
                "hour": s.astimezone(ctx.tz).hour,
                "price_eur_kwh": round(prices[i], 5),
                "elapsed": s + timedelta(hours=1) <= now,
            }
            for i, s in enumerate(starts)
        ],
        "series": {
            "base_kw": _r(inputs.base_kw),
            "cycles_kw": cycles,
            "tank_kw": _r(plan.tank_kw[:n]),
            "battery_charge_kw": _r(charge[:n]),
            "battery_discharge_kw": _r(discharge[:n]),
            "battery_stored_kwh": _r(simulate_battery(inputs, charge, discharge)),
            "tank_temp_c": _r(simulate_tank(inputs, plan.tank_kw), 2),
        },
        "items": items,
        "cost": {
            "planned_eur": round(optimised_eur, 4),
            "run_immediately_eur": round(immediate_eur, 4),
            "saving_eur": round(immediate_eur - optimised_eur, 4),
            "basis": (
                "whole net grid bill, base load included"
                if inputs.has_grid_balance
                else "appliance energy only, base load not included"
            ),
        },
        "left_out": excluded,
        "labels": [
            "Planned, not measured: the plan comes from the optimiser and a simulation.",
            "The day is planned from its start; hours already past are shown as planned.",
            "Appliance figures are assumptions from the catalogue and household file.",
            ctx.price_basis[0].upper() + ctx.price_basis[1:] + ".",
        ],
    }
