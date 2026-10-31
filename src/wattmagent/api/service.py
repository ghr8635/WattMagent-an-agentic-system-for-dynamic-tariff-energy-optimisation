"""The planning service behind the API: framework-free, so it is tested without HTTP.

``PlanService.plan`` runs the agent and returns ``(http_status, json_body)``. The body
leaves out the agent's internal trace, which can quote model-server error text.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol

from wattmagent.core.errors import WattMagentError
from wattmagent.data.prices import load_csv
from wattmagent.graph import AgentContext, AgentDeps, run_agent
from wattmagent.graph.context import window_start_for
from wattmagent.graph.request import Completer
from wattmagent.graph.validate import HORIZON_HOURS
from wattmagent.safety.audit import AuditLog
from wattmagent.safety.freshness import PriceSnapshot, check_price_snapshot


class ServiceUnavailable(WattMagentError):
    """The service cannot plan right now (for example the price data cannot be read)."""


class ContextProvider(Protocol):
    def get(self) -> AgentContext: ...


class CsvContextProvider:
    """Serves prices from a CSV file and reloads it when the file changes.

    ``fetched_at`` is the file's modification time: the honest answer to "how old are
    these prices". A feed that rewrites the file keeps the freshness check meaningful.
    """

    def __init__(self, path: Path, tz: tzinfo) -> None:
        self._path = path
        self._tz = tz
        self._mtime_ns = -1
        self._ctx: AgentContext | None = None
        self._lock = threading.Lock()

    def get(self) -> AgentContext:
        with self._lock:
            try:
                mtime_ns = self._path.stat().st_mtime_ns
                if self._ctx is None or mtime_ns != self._mtime_ns:
                    prices = load_csv(self._path)
                    fetched = datetime.fromtimestamp(mtime_ns / 1e9, UTC)
                    self._ctx = AgentContext(prices, fetched, self._tz)
                    self._mtime_ns = mtime_ns
            except (OSError, ValueError) as exc:
                raise ServiceUnavailable(f"price data unavailable: {type(exc).__name__}") from exc
            assert self._ctx is not None
            return self._ctx


@dataclass(slots=True)
class PlanService:
    provider: ContextProvider
    audit: AuditLog | None = None
    llm: Completer | None = None
    engine: str = "simple"
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def plan(self, text: str, request_id: str) -> tuple[int, dict[str, Any]]:
        ctx = self.provider.get()
        resp = run_agent(
            text,
            AgentDeps(ctx, llm=self.llm, audit=self.audit),
            self.clock(),
            request_id=request_id,
            engine=self.engine,
        )
        body: dict[str, Any] = {
            "status": resp.status,
            "message": resp.message,
            "request_id": resp.request_id,
            "decision": resp.decision,
            "request_satisfied": resp.request_satisfied,
            "explanation_source": resp.explanation_source,
            "facts": resp.facts,
            "model_calls": resp.usage.llm_calls,
        }
        body = json.loads(json.dumps(body, default=str))  # plain JSON types only
        return (422 if resp.status == "error" else 200), body

    def ready(self) -> list[str]:
        """Reasons the service cannot plan right now; empty means ready."""
        try:
            ctx = self.provider.get()
        except ServiceUnavailable as exc:
            return [str(exc)]
        now = self.clock()
        return check_price_snapshot(
            PriceSnapshot(ctx.prices, ctx.fetched_at),
            now=now,
            window_start=window_start_for(now),
            slots=HORIZON_HOURS,
        )
