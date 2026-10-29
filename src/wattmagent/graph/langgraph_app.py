"""The same flow as a LangGraph ``StateGraph``.

LangGraph is imported only here. Nodes and routes come from ``nodes.py``, so the graph
cannot drift from the tested flow.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from wattmagent.graph.nodes import END, ENTRY, ROUTES, AgentState, Node


def build_graph(nodes: Mapping[str, Node]) -> Any:
    from langgraph.graph import END as LG_END
    from langgraph.graph import START, StateGraph

    graph = StateGraph(AgentState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_edge(START, ENTRY)
    for name, route in ROUTES.items():

        def router(state: AgentState, route: Any = route) -> str:
            nxt = route(state)
            return LG_END if nxt == END else nxt

        graph.add_conditional_edges(name, router)
    return graph.compile()
