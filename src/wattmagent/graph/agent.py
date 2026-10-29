"""Public entry point of the agent: text in, ``AgentResponse`` out."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wattmagent.core.types import Usage
from wattmagent.graph.nodes import AgentDeps, AgentState, make_nodes, new_state
from wattmagent.graph.runner import run_flow
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
    )
