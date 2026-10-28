"""The only supported way to turn inputs into a plan that may be executed.

    prices fresh?  --no-->  fallback
         |yes
    run optimiser  --error/infeasible-->  fallback
         |ok
    verify plan    --rejected-->  fallback
         |passed
       use it

The fallback (charge the EV as early as possible, battery idle) is itself verified for
hard limits. If even that fails, ``PlanRejected`` is raised: refusing to act is safer than
acting on an unverified plan. Every call writes exactly one audit record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from wattmagent.core.errors import WattMagentError
from wattmagent.optimiser.household import plan_household
from wattmagent.planning.household import (
    HouseholdInputs,
    HouseholdPlan,
    HouseholdPlanner,
    ev_immediately,
)
from wattmagent.safety.audit import SCHEMA_VERSION, AuditLog
from wattmagent.safety.freshness import DEFAULT_MAX_AGE, PriceSnapshot, check_price_snapshot
from wattmagent.safety.verifier import Rule, VerificationReport, verify_plan

log = logging.getLogger(__name__)


class PlanRejected(WattMagentError):  # noqa: N818  (reads better as a noun)
    """Not even the fallback plan passed verification; nothing may be executed."""


class Decision(StrEnum):
    OPTIMISED = "optimised"
    FALLBACK = "fallback"


@dataclass(frozen=True, slots=True)
class SafePlanResult:
    plan: HouseholdPlan
    decision: Decision
    reasons: list[str]
    report: VerificationReport
    request_satisfied: bool  # False if the request itself (e.g. EV target) cannot be met
    request_id: str
    audit: dict[str, Any] = field(default_factory=dict)


def _fingerprint(inputs: HouseholdInputs) -> str:
    payload = {
        "prices": [round(p, 6) for p in inputs.prices],
        "load": [round(v, 6) for v in inputs.load_kw],
        "plugged": list(inputs.ev_available),
        "ev": asdict(inputs.ev),
        "battery": asdict(inputs.battery) if inputs.battery else None,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def _summary(inputs: HouseholdInputs) -> dict[str, Any]:
    prices = list(inputs.prices)
    return {
        "slots": inputs.slots,
        "price_min": round(min(prices), 6),
        "price_max": round(max(prices), 6),
        "price_mean": round(sum(prices) / len(prices), 6),
        "ev_plugged_slots": sum(inputs.ev_available),
        "ev_target_soc": inputs.ev.target_soc,
        "ev_arrival_soc": inputs.ev.arrival_soc,
        "battery": inputs.battery is not None,
        "fingerprint": _fingerprint(inputs),
    }


def plan_with_safety(
    inputs: HouseholdInputs,
    *,
    planner: HouseholdPlanner = plan_household,
    snapshot: PriceSnapshot | None = None,
    window_start: datetime | None = None,
    now: datetime | None = None,
    max_price_age: timedelta = DEFAULT_MAX_AGE,
    audit: AuditLog | None = None,
    request_id: str | None = None,
) -> SafePlanResult:
    """Plan, verify, and fall back if anything is wrong. See the module docstring."""
    rid = request_id or uuid.uuid4().hex
    clock = now or datetime.now(UTC)
    reasons: list[str] = []
    plan: HouseholdPlan | None = None
    report: VerificationReport | None = None

    if snapshot is not None:
        if window_start is None:
            raise ValueError("window_start is required when a price snapshot is given")
        reasons += check_price_snapshot(
            snapshot,
            now=clock,
            window_start=window_start,
            slots=inputs.slots,
            max_age=max_price_age,
        )

    request_satisfied = True
    if not reasons:
        try:
            candidate = planner(inputs)
        except Exception as exc:  # noqa: BLE001  (any planner failure must lead to the fallback)
            reasons.append(f"planner_error: {type(exc).__name__}")
            log.exception("planner failed; using fallback", extra={"request_id": rid})
        else:
            if not candidate.feasible:
                request_satisfied = False
                reasons.append(f"infeasible_request: {candidate.message}")
            else:
                checked = verify_plan(inputs, candidate)
                if checked.passed:
                    plan, report = candidate, checked
                else:
                    reasons.append("verifier_rejected: " + ", ".join(checked.rules))
                    log.warning("verifier rejected plan", extra={"request_id": rid})

    decision = Decision.OPTIMISED
    if plan is None or report is None:
        decision = Decision.FALLBACK
        plan = ev_immediately(inputs)
        report = verify_plan(inputs, plan, compare_with_baseline=False)
        hard = [v for v in report.violations if v.rule is not Rule.EV_TARGET]
        if hard:
            entry = _entry(rid, clock, inputs, decision, reasons, report, plan, "rejected", False)
            if audit:
                audit.record(entry)
            raise PlanRejected("fallback plan failed verification: " + ", ".join(report.rules))
        if not report.passed:  # only the EV target is missed: it is physically out of reach
            request_satisfied = False
            if not any(r.startswith("infeasible_request") for r in reasons):
                reasons.append("infeasible_request: EV target cannot be reached")

    entry = _entry(
        rid, clock, inputs, decision, reasons, report, plan, "executed", request_satisfied
    )
    if audit:
        audit.record(entry)
    return SafePlanResult(plan, decision, reasons, report, request_satisfied, rid, entry)


def _entry(
    rid: str,
    clock: datetime,
    inputs: HouseholdInputs,
    decision: Decision,
    reasons: list[str],
    report: VerificationReport,
    plan: HouseholdPlan,
    outcome: str,
    request_satisfied: bool,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
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
            "ev_kwh_requested": round(sum(plan.ev_kw), 6),
        },
        "verifier": {"passed": report.passed, "rules": report.rules},
    }
