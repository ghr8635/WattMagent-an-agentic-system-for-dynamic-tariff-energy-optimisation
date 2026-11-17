"""State that survives a restart and is shared by several worker processes on one host.

The in-memory idempotency store, rate limiter and plan store each live inside one process.
These three keep the same behaviour in one SQLite file (``WATTMAGENT_STATE_FILE``), so a
restart forgets nothing and ``uvicorn --workers 4`` counts requests together.

What this is and is not:
- Right for one machine, any number of processes, one writable volume. Every change runs in
  an immediate write transaction, so two workers can never both be "first" for a key and the
  rate limit never lets two requests through for the last free slot.
- **Not** for several machines: SQLite over a network file system is unsafe. Use a real
  database or Redis there (listed in docs/production-checklist.md).
- Times are wall-clock seconds, because a monotonic clock means nothing across processes.
- A database error (locked for more than a few seconds, disk full) raises, so the request
  fails with a 500 rather than skipping the rate limit or the idempotency check.
- The file holds plan requests (target charge, departure time) and answers sent to clients,
  never keys or request text; give it the same care as the audit log (a private volume).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from wattmagent.api.idempotency import MAX_ENTRIES, Begin, Kind, StoredResponse
from wattmagent.api.plans import MAX_PLANS, StoredPlan
from wattmagent.graph.validate import Validated

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency (
    scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
    created REAL NOT NULL, status INTEGER, body TEXT,
    PRIMARY KEY (scope, key));
CREATE INDEX IF NOT EXISTS idempotency_created ON idempotency (created);
CREATE TABLE IF NOT EXISTS rate_hits (who TEXT NOT NULL, t REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_hits_who ON rate_hits (who, t);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY, owner TEXT NOT NULL, version INTEGER NOT NULL,
    saved REAL NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS plans_saved ON plans (saved);
"""

BUSY_TIMEOUT_S = 5.0


class _Database:
    """One SQLite file; a short-lived connection per operation (cheap, and safe across threads
    and processes)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer
            conn.executescript(_SCHEMA)
        if not existed:
            with suppress(OSError):  # some file systems refuse chmod
                os.chmod(self.path, 0o600)  # the file holds plan details: owner only

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """An immediate write transaction: committed on success, rolled back on any error."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")


class SqliteIdempotencyStore:
    def __init__(
        self, path: Path, ttl_s: float = 86_400.0, clock: Callable[[], float] = time.time
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl must be positive")
        self._db = _Database(path)
        self._ttl = ttl_s
        self._clock = clock

    def begin(self, scope: str, key: str, fingerprint: str) -> Begin:
        now = self._clock()
        with self._db.write() as conn:
            conn.execute("DELETE FROM idempotency WHERE created <= ?", (now - self._ttl,))
            conn.execute(
                "DELETE FROM idempotency WHERE rowid IN (SELECT rowid FROM idempotency "
                "ORDER BY created DESC LIMIT -1 OFFSET ?)",
                (MAX_ENTRIES - 1,),
            )
            row = conn.execute(
                "SELECT fingerprint, status, body FROM idempotency WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO idempotency (scope, key, fingerprint, created) VALUES (?,?,?,?)",
                    (scope, key, fingerprint, now),
                )
                return Begin(Kind.FRESH)
            stored_fp, status, body = row
            if stored_fp != fingerprint:
                return Begin(Kind.CONFLICT)
            if status is None:
                return Begin(Kind.IN_FLIGHT)
            return Begin(Kind.REPLAY, StoredResponse(int(status), json.loads(body)))

    def finish(self, scope: str, key: str, stored: StoredResponse) -> None:
        with self._db.write() as conn:
            conn.execute(
                "UPDATE idempotency SET status = ?, body = ? WHERE scope = ? AND key = ?",
                (stored.status_code, json.dumps(stored.body), scope, key),
            )

    def abort(self, scope: str, key: str) -> None:
        """The request failed unexpectedly: forget it so the client can retry."""
        with self._db.write() as conn:
            conn.execute(
                "DELETE FROM idempotency WHERE scope = ? AND key = ? AND status IS NULL",
                (scope, key),
            )


class SqliteRateLimiter:
    """Sliding window per caller, counted across every process that shares the file."""

    def __init__(
        self,
        path: Path,
        limit: int,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if limit <= 0 or window_s <= 0:
            raise ValueError("limit and window must be positive")
        self._db = _Database(path)
        self._limit = limit
        self._window = window_s
        self._clock = clock

    def check(self, who: str) -> float:
        """Count one request. Returns 0.0 if allowed, else the seconds until it would be."""
        now = self._clock()
        with self._db.write() as conn:
            conn.execute("DELETE FROM rate_hits WHERE t <= ?", (now - self._window,))
            count, oldest = conn.execute(
                "SELECT COUNT(*), MIN(t) FROM rate_hits WHERE who = ?", (who,)
            ).fetchone()
            if count >= self._limit:
                return max(self._window - (now - float(oldest)), 0.001)
            conn.execute("INSERT INTO rate_hits (who, t) VALUES (?, ?)", (who, now))
            return 0.0


def _dump(plan: StoredPlan) -> str:
    v = plan.validated
    return json.dumps(
        {
            "target_soc": v.target_soc,
            "current_soc": v.current_soc,
            "depart_at": v.depart_at.isoformat(),
            "earliest_at": v.earliest_at.isoformat() if v.earliest_at else None,
            "notes": list(v.notes),
            "charge_windows": list(plan.charge_windows),
            "ev_cost_eur": plan.ev_cost_eur,
        }
    )


def _load(owner: str, version: int, payload: str) -> StoredPlan:
    d: dict[str, Any] = json.loads(payload)
    validated = Validated(
        target_soc=float(d["target_soc"]),
        current_soc=float(d["current_soc"]),
        depart_at=datetime.fromisoformat(d["depart_at"]),
        earliest_at=datetime.fromisoformat(d["earliest_at"]) if d["earliest_at"] else None,
        notes=[str(n) for n in d["notes"]],
    )
    cost = d["ev_cost_eur"]
    return StoredPlan(
        owner, validated, version, [str(w) for w in d["charge_windows"]],
        None if cost is None else float(cost),
    )  # fmt: skip


class SqlitePlanStore:
    def __init__(
        self, path: Path, ttl_s: float = 129_600.0, clock: Callable[[], float] = time.time
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl must be positive")
        self._db = _Database(path)
        self._ttl = ttl_s
        self._clock = clock

    def get(self, plan_id: str, owner: str) -> StoredPlan | None:
        now = self._clock()
        with self._db.connect() as conn:
            row = conn.execute(
                "SELECT owner, version, saved, payload FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        row_owner, version, saved, payload = row
        if now - saved >= self._ttl or row_owner != owner:
            return None
        return _load(row_owner, int(version), payload)

    def save(
        self,
        plan_id: str,
        owner: str,
        validated: Validated,
        charge_windows: list[str],
        ev_cost_eur: float | None,
    ) -> StoredPlan:
        """Store a plan (or the replan of one). The version counts up inside the transaction."""
        now = self._clock()
        with self._db.write() as conn:
            conn.execute("DELETE FROM plans WHERE saved <= ?", (now - self._ttl,))
            conn.execute(
                "DELETE FROM plans WHERE rowid IN (SELECT rowid FROM plans "
                "ORDER BY saved DESC LIMIT -1 OFFSET ?)",
                (MAX_PLANS - 1,),
            )
            row = conn.execute(
                "SELECT owner, version FROM plans WHERE plan_id = ?", (plan_id,)
            ).fetchone()
            version = row[1] + 1 if row and row[0] == owner else 1
            plan = StoredPlan(owner, validated, version, list(charge_windows), ev_cost_eur)
            conn.execute(
                "INSERT OR REPLACE INTO plans (plan_id, owner, version, saved, payload) "
                "VALUES (?,?,?,?,?)",
                (plan_id, owner, version, now, _dump(plan)),
            )
            return plan
