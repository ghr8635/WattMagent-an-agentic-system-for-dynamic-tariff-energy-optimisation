"""Agent nodes and routing.

Every node is a plain function ``state -> partial state update``, which is exactly what
LangGraph expects; ``runner.py`` runs the same nodes without it. Routing is separate
(``ROUTES``) so both runners share one definition of the flow:

    parse_request -> validate_request -> plan -> explain -> END
                          |  (needs an answer)       |  (plan rejected)
                          +--> END                   +--> END
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypedDict

from wattmagent.core.types import Usage
from wattmagent.graph.context import AgentContext, build_inputs, window_start_for
from wattmagent.graph.explain import build_facts, llm_explanation, template_explanation
from wattmagent.graph.guard import GuardedCompleter
from wattmagent.graph.request import MAX_REQUEST_CHARS, Completer, ParsedRequest, parse_request
from wattmagent.graph.validate import Clarification, Validated, validate_request
from wattmagent.safety.audit import AuditLog
from wattmagent.safety.freshness import PriceSnapshot
from wattmagent.safety.pipeline import PlanRejected, SafePlanResult, plan_with_safety

END = "__end__"


class AgentState(TypedDict, total=False):
    text: str
    now: datetime
    request_id: str
    parsed: ParsedRequest
    validated: Validated
    questions: list[str]
    result: SafePlanResult
    facts: dict[str, Any]
    explanation: str
    explanation_source: str
    error: str
    trace: list[str]


@dataclass(slots=True)
class AgentDeps:
    context: AgentContext
    llm: Completer | None = None
    audit: AuditLog | None = None
    usage: Usage | None = None


Node = Callable[[AgentState], dict[str, Any]]


def _trace(state: AgentState, line: str) -> list[str]:
    return [*state.get("trace", []), line]


def make_nodes(deps: AgentDeps) -> dict[str, Node]:
    ctx = deps.context
    llm: Completer | None = GuardedCompleter(deps.llm) if deps.llm is not None else None

    def parse_node(state: AgentState) -> dict[str, Any]:
        text = state["text"].strip()
        if not text or len(text) > MAX_REQUEST_CHARS:
            reason = "empty" if not text else f"longer than {MAX_REQUEST_CHARS} characters"
            return {
                "parsed": ParsedRequest(),
                "error": f"The request is {reason}.",
                "trace": _trace(state, f"parse: rejected ({reason})"),
            }
        parsed, line = parse_request(text, llm, deps.usage)
        return {"parsed": parsed, "trace": _trace(state, line)}

    def validate_node(state: AgentState) -> dict[str, Any]:
        if "error" in state:
            return {"questions": [state["error"]], "trace": _trace(state, "validate: skipped")}
        out = validate_request(
            state["parsed"], window_start=window_start_for(state["now"]), tz=ctx.tz
        )
        if isinstance(out, Clarification):
            return {"questions": out.questions, "trace": _trace(state, "validate: needs answers")}
        return {"validated": out, "trace": _trace(state, "validate: ok")}

    def plan_node(state: AgentState) -> dict[str, Any]:
        window_start = window_start_for(state["now"])
        inputs, starts = build_inputs(ctx, state["validated"], window_start)
        try:
            result = plan_with_safety(
                inputs,
                snapshot=PriceSnapshot(ctx.prices, ctx.fetched_at),
                window_start=window_start,
                now=state["now"],
                audit=deps.audit,
                request_id=state["request_id"],
            )
        except PlanRejected as exc:
            return {
                "error": f"No safe plan could be produced: {exc}",
                "trace": _trace(state, "plan: rejected by safety layer"),
            }
        facts = build_facts(
            result,
            inputs,
            starts,
            ctx.tz,
            state["validated"].notes,
            ctx.price_basis,
            state["validated"].depart_at,
        )
        return {
            "result": result,
            "facts": facts,
            "trace": _trace(state, f"plan: {result.decision.value}"),
        }

    def explain_node(state: AgentState) -> dict[str, Any]:
        facts = state["facts"]
        template = template_explanation(facts)
        model = llm
        if model is None:
            return {
                "explanation": template,
                "explanation_source": "template",
                "trace": _trace(state, "explain: template (no model configured)"),
            }
        text, source = llm_explanation(model, facts, template, deps.usage)
        return {
            "explanation": text,
            "explanation_source": source,
            "trace": _trace(state, f"explain: {source}"),
        }

    return {
        "parse_request": parse_node,
        "validate_request": validate_node,
        "plan": plan_node,
        "explain": explain_node,
    }


ENTRY = "parse_request"

ROUTES: dict[str, Callable[[AgentState], str]] = {
    "parse_request": lambda s: "validate_request",
    "validate_request": lambda s: "plan" if "validated" in s else END,
    "plan": lambda s: "explain" if "result" in s else END,
    "explain": lambda s: END,
}


def new_state(text: str, now: datetime, request_id: str | None = None) -> AgentState:
    return {"text": text, "now": now, "request_id": request_id or uuid.uuid4().hex, "trace": []}
