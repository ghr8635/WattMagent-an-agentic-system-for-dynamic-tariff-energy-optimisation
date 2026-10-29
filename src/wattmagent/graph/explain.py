"""Explaining a verified plan in plain language, without letting numbers be invented.

``build_facts`` collects every figure from the verified plan. ``template_explanation``
writes the explanation from those facts alone and always works. ``llm_explanation``
lets the model rephrase it, but the result is accepted only if every number in the
text is one the template (or the facts) already contains. Otherwise the template wins.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any

from wattmagent.core.errors import BudgetExceeded, LLMError
from wattmagent.core.types import Usage
from wattmagent.graph.request import Completer
from wattmagent.llm.client import Message
from wattmagent.planning.household import HouseholdInputs
from wattmagent.safety.pipeline import Decision, SafePlanResult

_PRICE_REASONS = (
    "stale_prices",
    "missing_prices",
    "no_prices",
    "implausible_price",
    "fetched_in_future",
)
_MIN_KW = 0.01


def _windows(starts: Sequence[datetime], kw: Sequence[float], tz: tzinfo) -> list[str]:
    """Consecutive charging hours as local 'HH:MM-HH:MM' ranges."""
    out: list[str] = []
    run: tuple[datetime, datetime] | None = None
    for s, v in zip(starts, kw, strict=True):
        if v > _MIN_KW:
            run = (run[0], s + timedelta(hours=1)) if run else (s, s + timedelta(hours=1))
        elif run:
            out.append(f"{run[0].astimezone(tz):%H:%M}-{run[1].astimezone(tz):%H:%M}")
            run = None
    if run:
        out.append(f"{run[0].astimezone(tz):%H:%M}-{run[1].astimezone(tz):%H:%M}")
    return out


def build_facts(
    result: SafePlanResult,
    inputs: HouseholdInputs,
    starts: Sequence[datetime],
    tz: tzinfo,
    notes: Sequence[str],
    price_basis: str,
    depart_at: datetime,
) -> dict[str, Any]:
    plan = result.plan
    prices_ok = not any(r.split(":")[0] in _PRICE_REASONS for r in result.reasons)
    ev_cost = sum(kw * p for kw, p in zip(plan.ev_kw, inputs.prices, strict=True))
    saving = None
    rep = result.report
    if prices_ok and rep.cost_eur is not None and rep.baseline_cost_eur is not None:
        saving = rep.baseline_cost_eur - rep.cost_eur
    return {
        "decision": result.decision.value,
        "request_satisfied": result.request_satisfied,
        "reasons": [r.split(":")[0] for r in result.reasons],
        "target_percent": round(inputs.ev.target_soc * 100),
        "start_percent": round(inputs.ev.arrival_soc * 100),
        "ready_by": f"{depart_at.astimezone(tz):%H:%M}",
        "charge_windows": _windows(starts, plan.ev_kw, tz),
        "ev_grid_kwh": round(sum(plan.ev_kw), 1),
        "ev_cost_eur": round(ev_cost, 2) if prices_ok else None,
        "saving_eur": round(saving, 2) if saving is not None else None,
        "battery_kwh": round(sum(-kw for kw in plan.battery_kw if kw < 0), 1),
        "price_basis": price_basis,
        "notes": list(notes),
    }


def template_explanation(f: dict[str, Any]) -> str:
    parts: list[str] = []
    if not f["request_satisfied"]:
        parts.append(
            f"I cannot reach {f['target_percent']}% by {f['ready_by']}: the charger is too slow "
            "for the time available. I will charge as much as possible, as early as possible."
        )
    elif f["decision"] == Decision.FALLBACK.value:
        parts.append(
            "I could not use the optimised plan, so I fall back to the simple safe plan: "
            "charge as early as possible."
        )
    else:
        parts.append(
            f"Plan: charge the car from {f['start_percent']}% to {f['target_percent']}%, "
            f"ready by {f['ready_by']}."
        )
    if f["charge_windows"]:
        parts.append(
            f"Charging hours: {', '.join(f['charge_windows'])} "
            f"({f['ev_grid_kwh']} kWh from the grid)."
        )
    else:
        parts.append("No charging is needed.")
    if f["ev_cost_eur"] is not None:
        parts.append(f"Charging cost: about {f['ev_cost_eur']} EUR ({f['price_basis']}).")
    if f["saving_eur"] is not None and f["saving_eur"] > 0.005 and f["decision"] == "optimised":
        parts.append(
            f"That saves {f['saving_eur']} EUR for the household "
            "compared with charging immediately."
        )
    if f["battery_kwh"] > 0:
        parts.append(f"The home battery supplies {f['battery_kwh']} kWh at pricier hours.")
    if f["notes"]:
        parts.append(" ".join(f["notes"]))
    if any(r in _PRICE_REASONS for r in f["reasons"]):
        parts.append("Price data was missing or old, so no cost figures are given.")
    return " ".join(parts)


# ------------------------------------------------------------------- model rephrasing

_NUM = re.compile(r"\d+(?:[.,]\d+)?")
SYSTEM_PROMPT = (
    "You rewrite a charging-plan summary for a home owner in two or three friendly "
    "sentences. Use ONLY the facts and numbers in the given text. Do not add any other "
    "number, advice, promise or claim. Reply with the rewritten text only."
)


def _numbers(text: str) -> set[float]:
    return {round(float(n.replace(",", ".")), 2) for n in _NUM.findall(text)}


def numbers_allowed(candidate: str, template: str, facts: dict[str, Any]) -> bool:
    allowed = _numbers(template) | _numbers(json.dumps(facts)) | {0.0}
    for value in list(allowed):
        allowed |= {float(round(value)), round(value, 1)}
    return _numbers(candidate) <= allowed


def llm_explanation(
    client: Completer, facts: dict[str, Any], template: str, usage: Usage | None = None
) -> tuple[str, str]:
    """Returns (text, source) where source is 'model' or 'template'."""
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": template},
    ]
    try:
        text = client.complete(messages, usage).text.strip()
    except (LLMError, BudgetExceeded):
        return template, "template"
    if not text or len(text) > 1200 or not numbers_allowed(text, template, facts):
        return template, "template"
    return text, "model"
