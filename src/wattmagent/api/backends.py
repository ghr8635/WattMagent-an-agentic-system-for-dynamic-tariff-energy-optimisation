"""What the API needs from its three stores, so memory and SQLite versions are interchangeable."""

from __future__ import annotations

from typing import Protocol

from wattmagent.api.idempotency import Begin, StoredResponse
from wattmagent.api.plans import StoredPlan
from wattmagent.graph.validate import Validated


class IdempotencyBackend(Protocol):
    def begin(self, scope: str, key: str, fingerprint: str) -> Begin: ...
    def finish(self, scope: str, key: str, stored: StoredResponse) -> None: ...
    def abort(self, scope: str, key: str) -> None: ...


class RateLimitBackend(Protocol):
    def check(self, who: str) -> float: ...


class PlanBackend(Protocol):
    def get(self, plan_id: str, owner: str) -> StoredPlan | None: ...
    def save(
        self,
        plan_id: str,
        owner: str,
        validated: Validated,
        charge_windows: list[str],
        ev_cost_eur: float | None,
    ) -> StoredPlan: ...
