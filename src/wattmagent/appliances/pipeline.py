"""The supported way to turn appliance inputs into a plan that may be executed.

Same contract as the EV pipeline: plan, verify, fall back to the rule-based plan if anything
is wrong, refuse (``PlanRejected``) if even that breaks a hard limit, and write exactly one
audit record per call. Comfort-band misses in the fallback mean the request itself is out of
reach (for example a draw larger than the element can replace); they are reported, not hidden.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from wattmagent.appliances.baselines import run_immediately
from wattmagent.appliances.models import AppliancePlan, DayInputs
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.verifier import (
    REQUEST_RULES,
    ApplianceReport,
    verify_appliance_plan,
)
from wattmagent.safety.audit import SCHEMA_VERSION, AuditLog
from wattmagent.safety.pipeline import Decision, PlanRejected

log = logging.getLogger(__name__)

AppliancePlanner = Callable[[DayInputs], AppliancePlan]


@dataclass(frozen=True, slots=True)
class SafeAppliancePlan:
    plan: AppliancePlan
    decision: Decision
    reasons: list[str]
    report: ApplianceReport
    request_satisfied: bool
    request_id: str
    audit: dict[str, Any] = field(default_factory=dict)


def _summary(inputs: DayInputs) -> dict[str, Any]:
    payload = {
        "prices": [round(p, 6) for p in inputs.prices],
        "jobs": [(j.spec.name, j.spec.profile_kw, j.earliest, j.deadline) for j in inputs.jobs],
        "tank": repr(inputs.tank),
        "building": repr(inputs.building),
        "pv": [round(v, 4) for v in inputs.pv_kw],
        "export": inputs.export_price_eur_kwh,
        "outdoor": [round(v, 3) for v in inputs.outdoor_c],
        "draw": [round(v, 6) for v in inputs.tank_draw_kwh],
        "base": [round(v, 6) for v in inputs.base_load_kw],
        "limit": inputs.max_grid_kw,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    prices = list(inputs.prices)
    # Counts and a fingerprint only: appliance names are user text and stay out of the log.
    return {
        "slots": inputs.slots,
        "cycles": len(inputs.jobs),
        "tank": inputs.tank is not None,
        "heatpump": inputs.building is not None,
        "solar": inputs.has_solar,
        "price_min": round(min(prices), 6),
        "price_max": round(max(prices), 6),
        "fingerprint": hashlib.sha256(blob).hexdigest()[:16],
    }


def plan_appliances_safely(
    inputs: DayInputs,
    *,
    planner: AppliancePlanner = plan_appliances,
    audit: AuditLog | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
    price_problems: Sequence[str] = (),
) -> SafeAppliancePlan:
    rid = request_id or uuid.uuid4().hex
    clock = now or datetime.now(UTC)
    reasons: list[str] = []
    plan: AppliancePlan | None = None
    report: ApplianceReport | None = None
    request_satisfied = True

    if price_problems:  # stale, missing or implausible prices: never optimise on them
        reasons.extend(price_problems)
        request_satisfied = True
        log.warning("price problems; using fallback", extra={"request_id": rid})
    try:
        candidate = planner(inputs) if not price_problems else None
    except Exception as exc:  # noqa: BLE001  (any planner failure must lead to the fallback)
        reasons.append(f"planner_error: {type(exc).__name__}")
        log.exception("appliance planner failed; using fallback", extra={"request_id": rid})
    else:
        if candidate is None:
            pass
        elif not candidate.feasible:
            request_satisfied = False
            reasons.append(f"infeasible_request: {candidate.message}")
        else:
            checked = verify_appliance_plan(inputs, candidate)
            if checked.passed:
                plan, report = candidate, checked
            else:
                reasons.append("verifier_rejected: " + ", ".join(checked.rules))
                log.warning("verifier rejected appliance plan", extra={"request_id": rid})

    decision = Decision.OPTIMISED
    if plan is None or report is None:
        decision = Decision.FALLBACK
        plan = run_immediately(inputs)
        report = verify_appliance_plan(inputs, plan, compare_with_baseline=False)
        hard = [v for v in report.violations if v.rule not in REQUEST_RULES]
        if hard:
            entry = _entry(rid, clock, inputs, decision, reasons, report, plan, "rejected", False)
            if audit:
                audit.record(entry)
            raise PlanRejected("fallback plan failed verification: " + ", ".join(report.rules))
        if not report.passed:
            request_satisfied = False
            if not any(r.startswith("infeasible_request") for r in reasons):
                reasons.append("infeasible_request: comfort band cannot be kept")

    entry = _entry(
        rid, clock, inputs, decision, reasons, report, plan, "executed", request_satisfied
    )
    if audit:
        audit.record(entry)
    return SafeAppliancePlan(plan, decision, reasons, report, request_satisfied, rid, entry)


def _entry(
    rid: str,
    clock: datetime,
    inputs: DayInputs,
    decision: Decision,
    reasons: list[str],
    report: ApplianceReport,
    plan: AppliancePlan,
    outcome: str,
    request_satisfied: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "kind": "appliances",
        "request_id": rid,
        "at": clock.isoformat(),
        "decision": decision.value,
        "outcome": outcome,
        "request_satisfied": request_satisfied,
        "reasons": reasons,
        "inputs": _summary(inputs),
        "plan": {
            "status": plan.status.value,
            "planned_cost_eur": plan.planned_cost_eur,
            "verified_cost_eur": report.cost_eur,
            "baseline_cost_eur": report.baseline_cost_eur,
        },
        "verifier": {"passed": report.passed, "rules": report.rules},
    }
