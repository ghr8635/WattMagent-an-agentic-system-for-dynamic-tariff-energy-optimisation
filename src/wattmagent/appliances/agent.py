"""Appliance requests through the same safe pipeline: text in, verified plan out.

    "run the dishwasher before 6pm and the washing machine by noon"

Only the appliances the request names are planned (cycles). The hot-water tank, heat pump and
solar need weather and are not planned from a request yet: the forecast feed is unverified.
Time handling: the plan covers the next 24 hours from the next full hour; a clock time means
its next occurrence; a deadline is rounded down and an earliest start up to the full hour, so
the plan never runs outside what was asked. Nothing here parses free text with a model.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, time, timedelta
from typing import Any

from wattmagent.appliances.household import Household, base_load_series
from wattmagent.appliances.models import CycleJob, CycleSpec, DayInputs
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.request import ApplianceAsk, parse_appliance_request
from wattmagent.appliances.simulate import cost_eur, place
from wattmagent.graph.agent import AgentResponse
from wattmagent.graph.context import AgentContext, window_start_for
from wattmagent.graph.validate import HORIZON_HOURS
from wattmagent.safety.audit import AuditLog
from wattmagent.safety.freshness import PriceSnapshot, check_price_snapshot
from wattmagent.safety.pipeline import Decision, PlanRejected


def _next_occurrence(local_time: time, after: datetime) -> datetime:
    """The first instant strictly after ``after`` whose local clock reads ``local_time``."""
    tz = after.tzinfo
    local = after.astimezone(tz)
    for days in range(0, 3):
        candidate = datetime.combine(local.date() + timedelta(days=days), local_time, tzinfo=tz)
        if candidate > after:
            return candidate
    raise ValueError("no next occurrence")  # pragma: no cover  (three days always suffice)


def _day_word(instant: datetime, now: datetime) -> str:
    delta = (instant.date() - now.date()).days
    return {0: "today", 1: "tomorrow"}.get(delta, instant.strftime("%a"))


def _hhmm(instant: datetime) -> str:
    return instant.strftime("%H:%M")


def _error(message: str, rid: str, status: str = "error") -> AgentResponse:
    return AgentResponse(status, message, rid)


def run_appliance_agent(
    text: str,
    household: Household,
    ctx: AgentContext,
    now: datetime,
    *,
    audit: AuditLog | None = None,
    request_id: str | None = None,
) -> AgentResponse:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    rid = request_id or uuid.uuid4().hex
    parsed = parse_appliance_request(text, household)

    if parsed.mentions_ev:
        return _error(
            "I can plan the car or the appliances, one request at a time. "
            "Please send the car request and the appliance request separately.",
            rid,
            "needs_input",
        )
    if parsed.ambiguous:
        names = ", ".join(sorted(parsed.ambiguous))
        return _error(
            f"More than one appliance matches '{names}'. Which one do you mean?", rid, "needs_input"
        )
    if not parsed.asks:
        known = ", ".join(sorted({e.model.key.replace("_", " ") for e in household.cycles}))
        return _error(
            f"I could not find an appliance in your request. I know: {known}.", rid, "needs_input"
        )

    window_start = window_start_for(now).astimezone(ctx.tz)
    horizon_end = window_start + timedelta(hours=HORIZON_HOURS)
    starts = [window_start + timedelta(hours=i) for i in range(HORIZON_HOURS)]
    notes: list[str] = []
    jobs: list[CycleJob] = []
    windows: dict[str, tuple[datetime, datetime]] = {}
    for ask in parsed.asks:
        window = _window(ask, window_start, horizon_end)
        label = ask.entry.name.replace("_", " ")
        if ask.deadline and window[1] == horizon_end:
            notes.append(
                f"The {label} deadline falls at or beyond the end of the 24 hours I plan, "
                "so I used "
                f"{_hhmm(horizon_end)} {_day_word(horizon_end, now)}."
            )
        spec = CycleSpec(ask.entry.name, ask.entry.model.profile_kw, ask.entry.model.description)
        first = math.ceil((window[0] - window_start).total_seconds() / 3600 - 1e-9)
        last = math.floor((window[1] - window_start).total_seconds() / 3600 + 1e-9)
        if first + spec.duration > last:
            hours = spec.duration
            return _error(
                f"The {label} needs {hours} hour{'s' if hours > 1 else ''}, "
                f"but the window {_hhmm(window[0])}-{_hhmm(window[1])} is shorter. "
                "Give it a wider window.",
                rid,
                "needs_input",
            )
        jobs.append(CycleJob(spec, first, last))
        windows[ask.entry.name] = (starts[first], starts[0] + timedelta(hours=last))

    by_start = {p.start: p.eur_per_kwh for p in ctx.prices}
    prices = [by_start.get(s, 0.0) for s in starts]
    local_hours = [s.hour for s in starts]
    base = base_load_series(household, local_hours)
    inputs = DayInputs(prices, jobs=jobs, base_load_kw=base, max_grid_kw=household.max_grid_kw)
    problems = check_price_snapshot(
        PriceSnapshot(ctx.prices, ctx.fetched_at),
        now=now,
        window_start=window_start,
        slots=HORIZON_HOURS,
    )
    try:
        safe = plan_appliances_safely(
            inputs, audit=audit, request_id=rid, now=now, price_problems=problems
        )
    except PlanRejected as exc:
        return _error(f"No safe plan could be made: {exc}", rid)

    facts = _facts(safe, inputs, jobs, windows, starts, now, ctx, not problems)
    message = _explain(facts, safe.decision, problems, notes)
    return AgentResponse(
        "planned",
        message,
        rid,
        decision=safe.decision.value,
        request_satisfied=safe.request_satisfied,
        facts=facts,
        explanation_source="template",
    )


def _window(
    ask: ApplianceAsk, window_start: datetime, horizon_end: datetime
) -> tuple[datetime, datetime]:
    earliest = (
        _next_occurrence(ask.earliest, window_start - timedelta(seconds=1))
        if ask.earliest
        else window_start
    )
    earliest = max(earliest, window_start)
    deadline = _next_occurrence(ask.deadline, earliest) if ask.deadline else horizon_end
    return earliest, min(deadline, horizon_end)


def _facts(
    safe: Any,
    inputs: DayInputs,
    jobs: list[CycleJob],
    windows: dict[str, tuple[datetime, datetime]],
    starts: list[datetime],
    now: datetime,
    ctx: AgentContext,
    prices_ok: bool,
) -> dict[str, Any]:
    cycles: list[dict[str, Any]] = []
    for job in jobs:
        name = job.spec.name
        series = safe.plan.cycle_kw[name]
        first = next(i for i, v in enumerate(series) if v > 0)
        # a profile may start with zero power; find the placement that reproduces the series
        for s in job.starts:
            if all(
                abs(a - b) < 1e-6
                for a, b in zip(series, place(job.spec.profile_kw, s, inputs.slots), strict=True)
            ):
                first = s
                break
        begin = starts[first]
        end = begin + timedelta(hours=job.spec.duration)
        now_cost = cost_eur(inputs.prices, series)
        base_cost = cost_eur(inputs.prices, place(job.spec.profile_kw, job.earliest, inputs.slots))
        cycles.append(
            {
                "appliance": name.replace("_", " "),
                "start": _hhmm(begin),
                "start_day": _day_word(begin, now),
                "end": _hhmm(end),
                "end_day": _day_word(end, now),
                "window_start": _hhmm(windows[name][0]),
                "window_start_day": _day_word(windows[name][0], now),
                "window_end": _hhmm(windows[name][1]),
                "window_end_day": _day_word(windows[name][1], now),
                "energy_kwh": round(job.spec.energy_kwh, 2),
                "cost_eur": round(now_cost, 2) if prices_ok else None,
                "cost_if_started_at_window_start_eur": round(base_cost, 2) if prices_ok else None,
            }
        )
    return {"cycles": cycles, "price_basis": ctx.price_basis, "prices_ok": prices_ok}


def _explain(
    facts: dict[str, Any], decision: Decision, problems: list[str], notes: list[str]
) -> str:
    parts: list[str] = []
    for c in facts["cycles"]:
        line = (
            f"{c['appliance'].capitalize()}: start {c['start']} {c['start_day']}, "
            f"done by {c['end']} {c['end_day']} "
            f"(window {c['window_start']} {c['window_start_day']} "
            f"to {c['window_end']} {c['window_end_day']}, "
            f"{c['energy_kwh']} kWh)."
        )
        if c["cost_eur"] is not None:
            line += f" About {c['cost_eur']} EUR ({facts['price_basis']})"
            early = c["cost_if_started_at_window_start_eur"]
            if early is not None:
                line += f", {early} EUR if started at {c['window_start']}"
            line += "."
        parts.append(line)
    if decision is Decision.FALLBACK:
        why = "; ".join(problems) or "the optimised plan did not pass the safety checks"
        parts.append(f"Note: every appliance starts as early as its window allows, because {why}.")
    parts.extend(notes)
    return " ".join(parts)
