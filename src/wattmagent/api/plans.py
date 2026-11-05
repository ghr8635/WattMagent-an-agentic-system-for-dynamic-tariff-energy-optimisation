"""Remembers the checked request behind each plan, so it can be replanned later.

In memory and per process, like the idempotency store: a restart forgets plans, and a
replan of a forgotten plan is a clean 404. Plans belong to the API key that made them;
another key gets the same 404 as for an unknown id, so ids reveal nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from wattmagent.graph.validate import Validated

MAX_PLANS = 10_000


@dataclass(frozen=True, slots=True)
class StoredPlan:
    owner: str
    validated: Validated
    version: int  # 1 for the first plan, +1 for every replan
    charge_windows: list[str]
    ev_cost_eur: float | None


@dataclass(slots=True)
class _Entry:
    plan: StoredPlan
    saved: float


class PlanStore:
    def __init__(
        self, ttl_s: float = 129_600.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl must be positive")
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        for key in [k for k, e in self._entries.items() if now - e.saved >= self._ttl]:
            del self._entries[key]
        while len(self._entries) >= MAX_PLANS:  # oldest first
            del self._entries[next(iter(self._entries))]

    def get(self, plan_id: str, owner: str) -> StoredPlan | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(plan_id)
            if entry is None or now - entry.saved >= self._ttl or entry.plan.owner != owner:
                return None
            return entry.plan

    def save(
        self,
        plan_id: str,
        owner: str,
        validated: Validated,
        charge_windows: list[str],
        ev_cost_eur: float | None,
    ) -> StoredPlan:
        """Store a plan (or the replan of one). The version counts up inside the lock."""
        now = self._clock()
        with self._lock:
            self._purge(now)
            existing = self._entries.get(plan_id)
            version = existing.plan.version + 1 if existing and existing.plan.owner == owner else 1
            plan = StoredPlan(owner, validated, version, list(charge_windows), ev_cost_eur)
            self._entries[plan_id] = _Entry(plan, now)
            return plan
