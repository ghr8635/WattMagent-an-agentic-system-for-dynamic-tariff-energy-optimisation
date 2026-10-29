"""Deterministic validation of a parsed request. The model's output is never trusted.

Turns clock times into real instants for the planning window, applies conservative
rounding to the hourly grid (departure rounded *down*, earliest start rounded *up*),
and either returns a ``Validated`` request or a ``Clarification`` with questions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo

from wattmagent.graph.request import ParsedRequest

HORIZON_HOURS = 24
_RECENT = timedelta(hours=6)  # how recent a passed 'not before' time must be to be void


@dataclass(frozen=True, slots=True)
class Validated:
    target_soc: float
    current_soc: float
    depart_at: datetime  # UTC, on the hour: the car must be ready by then
    earliest_at: datetime | None  # UTC, on the hour: no charging before this
    notes: list[str] = field(default_factory=list)  # assumptions the user should see


@dataclass(frozen=True, slots=True)
class Clarification:
    questions: list[str]


def _next_occurrence(t: time, after: datetime, tz: tzinfo) -> datetime:
    """First local instant with clock time ``t`` strictly after ``after``."""
    local_after = after.astimezone(tz)
    day = local_after.date()
    for _ in range(3):
        cand = datetime.combine(day, t, tzinfo=tz)
        if cand > local_after:
            return cand
        day += timedelta(days=1)
    raise AssertionError("unreachable")  # pragma: no cover


def validate_request(
    parsed: ParsedRequest,
    *,
    window_start: datetime,
    tz: tzinfo,
    default_target_percent: float = 80.0,
    default_current_percent: float = 30.0,
) -> Validated | Clarification:
    questions: list[str] = []
    notes: list[str] = []

    target = parsed.target_percent
    if target is None:
        target = default_target_percent
        notes.append(f"No target given; assuming {target:g}%.")
    elif not 0 < target <= 100:
        questions.append("The target charge must be between 1% and 100%. What should it be?")

    current = parsed.current_percent
    if current is None:
        current = default_current_percent
        notes.append(f"Current charge unknown; assuming {current:g}%.")
    elif not 0 <= current <= 100:
        questions.append("The current charge must be between 0% and 100%. What is it?")

    depart_at: datetime | None = None
    if parsed.depart is None:
        questions.append("When does the car need to be ready (for example 07:00)?")
    else:
        raw = _next_occurrence(parsed.depart, window_start, tz)
        floored = raw.replace(minute=0, second=0, microsecond=0).astimezone(window_start.tzinfo)
        if parsed.depart.minute:
            notes.append(
                f"Departure {parsed.depart:%H:%M} is planned as {floored.astimezone(tz):%H:%M}."
            )
        if floored < window_start + timedelta(hours=1):
            questions.append(
                "There is less than one full hour before departure. Is the time right?"
            )
        elif floored > window_start + timedelta(hours=HORIZON_HOURS):
            questions.append(
                f"I plan {HORIZON_HOURS} hours ahead; please give a nearer departure time."
            )
        else:
            depart_at = floored

    earliest_at: datetime | None = None
    if parsed.earliest_start is not None and depart_at is not None:
        # The next time the clock shows this hour, rounded up to a full hour.
        nxt = _next_occurrence(parsed.earliest_start, window_start, tz)
        if nxt.minute:
            nxt = nxt.replace(minute=0) + timedelta(hours=1)
        if nxt.astimezone(window_start.tzinfo) < depart_at:
            earliest_at = nxt.astimezone(window_start.tzinfo)
        else:
            # It does not occur before departure. That is fine only if it already happened a
            # short while ago (e.g. "not before 22:00" asked at 23:30); otherwise it contradicts
            # the departure time and we ask.
            prev = datetime.combine(nxt.date() - timedelta(days=1), nxt.timetz(), tzinfo=tz)
            if window_start - prev > _RECENT:
                questions.append(
                    "Charging may not start before the car must be ready. Check the times."
                )

    if questions or target is None or current is None or depart_at is None:
        return Clarification(questions or ["Please restate the request."])
    return Validated(target / 100.0, current / 100.0, depart_at, earliest_at, notes)
