"""The planning service behind the API: framework-free, so it is tested without HTTP.

``PlanService.plan`` runs the agent and returns ``(http_status, json_body)``. The body
leaves out the agent's internal trace, which can quote model-server error text.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, Protocol

from wattmagent.api.plans import PlanStore
from wattmagent.appliances.agent import run_appliance_agent
from wattmagent.appliances.household import Household
from wattmagent.appliances.request import mentions_appliance
from wattmagent.core.errors import ReplanError, WattMagentError
from wattmagent.data.prices import load_csv
from wattmagent.data.smard_live import JsonFetch, PriceFeedError, fetch_prices
from wattmagent.graph import AgentContext, AgentDeps, run_agent
from wattmagent.graph.agent import AgentResponse, run_replan
from wattmagent.graph.context import window_start_for
from wattmagent.graph.request import Completer
from wattmagent.graph.validate import HORIZON_HOURS
from wattmagent.safety.audit import AuditLog
from wattmagent.safety.freshness import PriceSnapshot, check_price_snapshot

log = logging.getLogger(__name__)


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


class LiveContextProvider:
    """Serves live SMARD prices, refreshed at most every ``refresh_s`` seconds.

    ``fetched_at`` is the moment WE fetched. If a refresh fails, the previous prices keep
    being served with their old ``fetched_at``, so the freshness check turns "the feed is
    down" into a safe fallback after its age limit, instead of hiding it. The refresh
    runs under a lock, so concurrent requests wait for one fetch (bounded by the timeout)
    rather than each starting their own.
    """

    def __init__(
        self,
        fetch: JsonFetch,
        tz: tzinfo,
        *,
        refresh_s: float = 900.0,
        weeks: int = 2,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetch = fetch
        self._tz = tz
        self._refresh_s = refresh_s
        self._weeks = weeks
        self._clock = clock
        self._ctx: AgentContext | None = None
        self._tried_at: datetime | None = None  # last attempt, successful or not
        self._lock = threading.Lock()

    def get(self) -> AgentContext:
        with self._lock:
            now = self._clock()
            due = (
                self._ctx is None
                or self._tried_at is None
                or (now - self._tried_at).total_seconds() >= self._refresh_s
            )
            if due:
                self._tried_at = now
                try:
                    got = fetch_prices(self._fetch, weeks=self._weeks, clock=self._clock)
                except PriceFeedError as exc:
                    if self._ctx is None:
                        raise ServiceUnavailable(f"price feed unavailable: {exc}") from exc
                    log.warning("price refresh failed, keeping older prices: %s", exc)
                else:
                    self._ctx = AgentContext(got.points, got.fetched_at, self._tz)
            assert self._ctx is not None
            return self._ctx


@dataclass(slots=True)
class PlanService:
    provider: ContextProvider
    audit: AuditLog | None = None
    llm: Completer | None = None
    engine: str = "simple"
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    store: PlanStore = field(default_factory=PlanStore)
    household: Household | None = None  # enables appliance requests (WATTMAGENT_HOUSEHOLD_FILE)
    _last_fetched: datetime | None = field(default=None, init=False, repr=False)

    def price_age_hours(self) -> float | None:
        """Age of the prices seen at last use, or None before the first use."""
        if self._last_fetched is None:
            return None
        return max((self.clock() - self._last_fetched).total_seconds() / 3600, 0.0)

    def _body(self, resp: AgentResponse) -> dict[str, Any]:
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
        return body

    def _remember(self, plan_id: str, owner: str, resp: AgentResponse) -> int:
        assert resp.validated is not None and resp.facts is not None
        stored = self.store.save(
            plan_id,
            owner,
            resp.validated,
            list(resp.facts["charge_windows"]),
            resp.facts["ev_cost_eur"],
        )
        return stored.version

    def plan(self, text: str, request_id: str, owner: str = "") -> tuple[int, dict[str, Any]]:
        ctx = self.provider.get()
        self._last_fetched = ctx.fetched_at
        if self.household is not None and mentions_appliance(text, self.household):
            # Appliance requests are planned and explained deterministically (no model, no
            # plan id): replanning is only built for the car.
            resp = run_appliance_agent(
                text, self.household, ctx, self.clock(), audit=self.audit, request_id=request_id
            )
        else:
            resp = run_agent(
                text,
                AgentDeps(ctx, llm=self.llm, audit=self.audit),
                self.clock(),
                request_id=request_id,
                engine=self.engine,
            )
        body = self._body(resp)
        if resp.status == "planned" and resp.validated is not None:
            plan_id = uuid.uuid4().hex
            body["plan_id"] = plan_id
            body["version"] = self._remember(plan_id, owner, resp)
        body = json.loads(json.dumps(body, default=str))  # plain JSON types only
        return (422 if resp.status == "error" else 200), body

    def replan(
        self,
        plan_id: str,
        owner: str,
        current_percent: float,
        battery_percent: float | None,
        request_id: str,
    ) -> tuple[int, dict[str, Any]]:
        """Plan again from now for a stored plan, using the car's real charge.

        Prices are whatever the provider holds now, and the time left is counted from
        now, so a replan also picks up fresher prices and a shorter night.
        """
        previous = self.store.get(plan_id, owner)
        if previous is None:
            raise ReplanError("unknown or expired plan", 404)
        ctx = self.provider.get()
        self._last_fetched = ctx.fetched_at
        if battery_percent is not None:
            if ctx.battery is None:
                raise ReplanError("no home battery is configured", 422)
            try:
                battery = replace(ctx.battery, initial_soc=battery_percent / 100.0)
            except ValueError as exc:
                raise ReplanError(
                    f"battery charge must lie between {ctx.battery.min_soc:.0%} and "
                    f"{ctx.battery.max_soc:.0%}",
                    422,
                ) from exc
            ctx = replace(ctx, battery=battery)
        # The audit record's request id ties the replan to its plan: "<plan id>.r<version>".
        audit_id = f"{plan_id}.r{previous.version + 1}"
        resp = run_replan(
            previous.validated,
            current_percent / 100.0,
            AgentDeps(ctx, llm=self.llm, audit=self.audit),
            self.clock(),
            request_id=audit_id,
        )
        body = self._body(resp)
        body["request_id"] = request_id
        if resp.status == "planned" and resp.validated is not None:
            body["plan_id"] = plan_id
            body["version"] = self._remember(plan_id, owner, resp)
            assert resp.facts is not None
            body["previous"] = {
                "version": previous.version,
                "charge_windows": previous.charge_windows,
                "ev_cost_eur": previous.ev_cost_eur,
            }
            body["changed"] = previous.charge_windows != resp.facts["charge_windows"]
        body = json.loads(json.dumps(body, default=str))
        return (422 if resp.status == "error" else 200), body

    def ready(self) -> list[str]:
        """Reasons the service cannot plan right now; empty means ready."""
        try:
            ctx = self.provider.get()
        except ServiceUnavailable as exc:
            return [str(exc)]
        self._last_fetched = ctx.fetched_at
        now = self.clock()
        return check_price_snapshot(
            PriceSnapshot(ctx.prices, ctx.fetched_at),
            now=now,
            window_start=window_start_for(now),
            slots=HORIZON_HOURS,
        )
