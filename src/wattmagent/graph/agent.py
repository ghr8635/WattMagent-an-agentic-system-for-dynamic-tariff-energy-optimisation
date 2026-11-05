"""Public entry point of the agent: text in, ``AgentResponse`` out."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from wattmagent.core.errors import ReplanError
from wattmagent.core.types import Usage
from wattmagent.graph.context import window_start_for
from wattmagent.graph.nodes import AgentDeps, AgentState, make_nodes, new_state
from wattmagent.graph.runner import run_flow
from wattmagent.graph.validate import Validated
from wattmagent.safety.pipeline import SafePlanResult


@dataclass(frozen=True, slots=True)
class AgentResponse:
    status: str  # "planned", "needs_input" or "error"
    message: str  # the explanation, the questions, or the error
    request_id: str
    decision: str | None = None  # "optimised" or "fallback"
    request_satisfied: bool | None = None
    facts: dict[str, Any] | None = None
    explanation_source: str | None = None  # "model" or "template"
    trace: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    result: SafePlanResult | None = None
    validated: Validated | None = None  # the checked request; kept so it can be replanned


def run_agent(
    text: str,
    deps: AgentDeps,
    now: datetime,
    *,
    request_id: str | None = None,
    engine: str = "simple",
) -> AgentResponse:
    """Run one request. ``now`` must be timezone-aware. ``engine`` is 'simple' or 'langgraph'."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if deps.usage is None:
        deps.usage = Usage()
    nodes = make_nodes(deps)
    state: AgentState = new_state(text, now, request_id)
    if engine == "langgraph":
        from wattmagent.graph.langgraph_app import build_graph

        final: AgentState = build_graph(nodes).invoke(state)
    elif engine == "simple":
        final = run_flow(nodes, state)
    else:
        raise ValueError(f"unknown engine: {engine}")
    return _response(final, deps.usage)


def _response(state: AgentState, usage: Usage) -> AgentResponse:
    rid, trace = state["request_id"], state.get("trace", [])
    if "error" in state:
        return AgentResponse("error", state["error"], rid, trace=trace, usage=usage)
    result = state.get("result")
    if result is None:
        questions = " ".join(state.get("questions", ["Please restate the request."]))
        return AgentResponse("needs_input", questions, rid, trace=trace, usage=usage)
    return AgentResponse(
        "planned",
        state["explanation"],
        rid,
        decision=result.decision.value,
        request_satisfied=result.request_satisfied,
        facts=state["facts"],
        explanation_source=state["explanation_source"],
        trace=trace,
        usage=usage,
        result=result,
        validated=state.get("validated"),
    )


_CURRENT_UNKNOWN = "Current charge unknown"


def run_replan(
    previous: Validated,
    current_soc: float,
    deps: AgentDeps,
    now: datetime,
    *,
    request_id: str | None = None,
) -> AgentResponse:
    """Plan again from ``now`` for an already validated request.

    The target and the departure time stay as the user gave them; what changes is the
    car's actual charge, the time that is left, and whatever prices ``deps`` holds now.
    No text is parsed: the checked request from the first plan is reused, so a replan
    cannot be steered by new free text. Raises ``ReplanError`` when no plan is possible.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not 0.0 <= current_soc <= 1.0:
        raise ReplanError("the car's charge must be between 0% and 100%", 422)
    window_start = window_start_for(now)
    if previous.depart_at < window_start + timedelta(hours=1):
        raise ReplanError(
            "there is less than one full hour before the departure time; nothing can be planned"
        )
    # A 'not before' time that has passed no longer restricts anything.
    earliest = (
        previous.earliest_at
        if previous.earliest_at and previous.earliest_at > window_start
        else None
    )
    notes = [n for n in previous.notes if not n.startswith(_CURRENT_UNKNOWN)]
    notes.append(f"Replanned from the car's reported charge of {current_soc * 100:g}%.")
    validated = replace(previous, current_soc=current_soc, earliest_at=earliest, notes=notes)

    if deps.usage is None:
        deps.usage = Usage()
    nodes = make_nodes(deps)
    state: AgentState = new_state("", now, request_id)
    state["validated"] = validated
    state["trace"] = ["replan: from the stored request"]
    return _response(run_flow(nodes, state, start="plan"), deps.usage)
