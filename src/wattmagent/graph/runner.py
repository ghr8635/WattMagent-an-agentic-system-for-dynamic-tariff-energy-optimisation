"""Running the agent flow.

``run_flow`` is a tiny executor with the same semantics as the LangGraph graph built in
``langgraph_app.py``: start at ``ENTRY``, run a node, merge its update into the state,
ask ``ROUTES`` where to go next, stop at ``END``. A step limit guards against loops.
A test runs both on the same inputs and requires identical results.
"""

from __future__ import annotations

from collections.abc import Mapping

from wattmagent.graph.nodes import END, ENTRY, ROUTES, AgentState, Node

MAX_STEPS = 12


def run_flow(nodes: Mapping[str, Node], state: AgentState) -> AgentState:
    current = ENTRY
    for _ in range(MAX_STEPS):
        state.update(nodes[current](state))  # type: ignore[typeddict-item]
        current = ROUTES[current](state)
        if current == END:
            return state
    raise RuntimeError("agent flow did not finish (routing loop?)")
