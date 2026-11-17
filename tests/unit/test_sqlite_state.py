"""SQLite-backed state: same behaviour as the in-memory stores, plus restart and sharing."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wattmagent.api import sqlite_state
from wattmagent.api.idempotency import IdempotencyStore, Kind, StoredResponse
from wattmagent.api.plans import PlanStore
from wattmagent.api.ratelimit import RateLimiter
from wattmagent.api.sqlite_state import (
    SqliteIdempotencyStore,
    SqlitePlanStore,
    SqliteRateLimiter,
)
from wattmagent.graph.validate import Validated


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "wattmagent.sqlite3"  # parent folder does not exist yet


def validated(earliest: bool = True) -> Validated:
    return Validated(
        target_soc=0.9,
        current_soc=0.35,
        depart_at=datetime(2026, 3, 11, 5, 30, tzinfo=UTC),
        earliest_at=datetime(2026, 3, 10, 21, 0, tzinfo=UTC) if earliest else None,
        notes=["assumed 80% target", "départ à 6h"],  # non-ASCII must survive too
    )


# --- idempotency ------------------------------------------------------------------------------


def test_idempotency_has_the_same_four_outcomes(db: Path) -> None:
    store = SqliteIdempotencyStore(db, clock=Clock())
    assert store.begin("a", "k", "fp1").kind is Kind.FRESH
    assert store.begin("a", "k", "fp1").kind is Kind.IN_FLIGHT
    assert store.begin("a", "k", "other").kind is Kind.CONFLICT
    answer = StoredResponse(200, {"plan_id": "p1", "n": [1, 2.5], "ok": True, "x": None})
    store.finish("a", "k", answer)
    replay = store.begin("a", "k", "fp1")
    assert replay.kind is Kind.REPLAY and replay.stored == answer
    assert store.begin("b", "k", "fp1").kind is Kind.FRESH  # another caller, same key


def test_abort_forgets_an_unfinished_request_but_not_a_finished_one(db: Path) -> None:
    store = SqliteIdempotencyStore(db, clock=Clock())
    store.begin("a", "k", "fp")
    store.abort("a", "k")
    assert store.begin("a", "k", "fp").kind is Kind.FRESH
    store.finish("a", "k", StoredResponse(200, {}))
    store.abort("a", "k")
    assert store.begin("a", "k", "fp").kind is Kind.REPLAY


def test_idempotency_entries_expire(db: Path) -> None:
    clock = Clock()
    store = SqliteIdempotencyStore(db, ttl_s=100.0, clock=clock)
    store.begin("a", "k", "fp")
    store.finish("a", "k", StoredResponse(200, {"x": 1}))
    clock.t += 99
    assert store.begin("a", "k", "fp").kind is Kind.REPLAY
    clock.t += 1
    assert store.begin("a", "k", "fp").kind is Kind.FRESH


def test_idempotency_is_capped_oldest_first(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_state, "MAX_ENTRIES", 3)
    clock = Clock()
    store = SqliteIdempotencyStore(db, clock=clock)
    for i in range(5):
        clock.t += 1
        store.begin("a", f"k{i}", "fp")
    with sqlite3.connect(db) as conn:
        keys = {r[0] for r in conn.execute("SELECT key FROM idempotency")}
    assert keys == {"k2", "k3", "k4"}  # at most 3 kept, the newest


def test_a_second_worker_and_a_restart_see_the_same_keys(db: Path) -> None:
    clock = Clock()
    worker_1 = SqliteIdempotencyStore(db, clock=clock)
    worker_2 = SqliteIdempotencyStore(db, clock=clock)
    assert worker_1.begin("a", "k", "fp").kind is Kind.FRESH
    assert worker_2.begin("a", "k", "fp").kind is Kind.IN_FLIGHT  # not run a second time
    worker_1.finish("a", "k", StoredResponse(200, {"v": 1}))
    restarted = SqliteIdempotencyStore(db, clock=clock)
    assert restarted.begin("a", "k", "fp").kind is Kind.REPLAY


def test_bad_ttls_are_refused(db: Path) -> None:
    with pytest.raises(ValueError):
        SqliteIdempotencyStore(db, ttl_s=0)
    with pytest.raises(ValueError):
        SqlitePlanStore(db, ttl_s=-1)


# --- rate limit -------------------------------------------------------------------------------


def test_rate_limit_allows_the_limit_then_says_how_long_to_wait(db: Path) -> None:
    clock = Clock()
    limiter = SqliteRateLimiter(db, limit=3, window_s=60.0, clock=clock)
    assert [limiter.check("a") for _ in range(3)] == [0.0, 0.0, 0.0]
    clock.t += 10
    assert limiter.check("a") == pytest.approx(50.0)
    assert limiter.check("b") == 0.0  # another caller has its own allowance
    clock.t += 50
    assert limiter.check("a") == 0.0  # the first hit has left the window


def test_rate_limit_is_shared_by_workers_and_survives_a_restart(db: Path) -> None:
    clock = Clock()
    one = SqliteRateLimiter(db, limit=4, clock=clock)
    two = SqliteRateLimiter(db, limit=4, clock=clock)
    assert [one.check("a"), two.check("a"), one.check("a"), two.check("a")] == [0.0] * 4
    assert one.check("a") > 0 and two.check("a") > 0
    assert SqliteRateLimiter(db, limit=4, clock=clock).check("a") > 0  # restart: still counted


def test_rate_limit_bad_settings(db: Path) -> None:
    with pytest.raises(ValueError):
        SqliteRateLimiter(db, limit=0)


def _hit(args: tuple[str, str, int, float]) -> int:
    path, who, n, go_at = args
    while time.time() < go_at:  # all processes open the (new) file at the same instant
        pass
    limiter = SqliteRateLimiter(Path(path), limit=20, window_s=3600.0)  # real clock
    return sum(1 for _ in range(n) if limiter.check(who) == 0.0)


@pytest.mark.skipif(sys.platform == "win32", reason="process pool start-up is slow on Windows")
def test_four_processes_together_never_exceed_the_limit(db: Path) -> None:
    with ProcessPoolExecutor(max_workers=4) as pool:
        go_at = time.time() + 1.5
        allowed = sum(pool.map(_hit, [(str(db), "a", 40, go_at)] * 4))  # 160 attempts, limit 20
    assert allowed == 20


def _race(args: tuple[str, int, float]) -> list[bool]:
    path, keys, go_at = args
    while time.time() < go_at:  # every process starts at the same instant: real contention
        pass
    store = SqliteIdempotencyStore(Path(path))
    return [store.begin("a", f"key-{i}", "fp").kind is Kind.FRESH for i in range(keys)]


@pytest.mark.skipif(sys.platform == "win32", reason="process pool start-up is slow on Windows")
def test_only_one_of_many_processes_is_first_for_each_key(db: Path) -> None:
    keys = 300
    with ProcessPoolExecutor(max_workers=4) as pool:
        go_at = time.time() + 1.5
        runs = list(pool.map(_race, [(str(db), keys, go_at)] * 4))
    per_key = [sum(run[i] for run in runs) for i in range(keys)]
    assert per_key == [1] * keys  # exactly one winner per key, no errors from lock contention


# --- plans ------------------------------------------------------------------------------------


def test_a_plan_round_trips_exactly(db: Path) -> None:
    store = SqlitePlanStore(db, clock=Clock())
    for earliest in (True, False):
        saved = store.save(f"p{earliest}", "me", validated(earliest), ["22:00-02:00"], 1.23)
        got = store.get(f"p{earliest}", "me")
        assert got == saved and got is not None
        assert got.validated == validated(earliest)
        assert got.validated.depart_at.utcoffset() is not None  # still timezone-aware
    none_cost = store.save("p3", "me", validated(), [], None)
    assert store.get("p3", "me") == none_cost and none_cost.ev_cost_eur is None


def test_versions_count_up_and_owners_are_separate(db: Path) -> None:
    store = SqlitePlanStore(db, clock=Clock())
    assert store.save("p", "me", validated(), ["a"], 1.0).version == 1
    assert store.save("p", "me", validated(), ["b"], 2.0).version == 2
    assert store.get("p", "me") is not None and store.get("p", "me").charge_windows == ["b"]  # type: ignore[union-attr]
    assert store.get("p", "someone else") is None  # same answer as an unknown id
    assert store.get("unknown", "me") is None


def test_plans_expire_and_survive_a_restart(db: Path) -> None:
    clock = Clock()
    SqlitePlanStore(db, ttl_s=100.0, clock=clock).save("p", "me", validated(), [], 1.0)
    reopened = SqlitePlanStore(db, ttl_s=100.0, clock=clock)
    assert reopened.get("p", "me") is not None
    clock.t += 100
    assert reopened.get("p", "me") is None


def test_plans_are_capped_oldest_first(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_state, "MAX_PLANS", 2)
    clock = Clock()
    store = SqlitePlanStore(db, clock=clock)
    for i in range(4):
        clock.t += 1
        store.save(f"p{i}", "me", validated(), [], None)
    assert store.get("p0", "me") is None and store.get("p1", "me") is None
    assert store.get("p2", "me") is not None and store.get("p3", "me") is not None


# --- same behaviour as the in-memory versions -------------------------------------------------


def test_memory_and_sqlite_rate_limiters_agree_on_a_long_sequence(db: Path) -> None:
    clock = Clock()
    mem = RateLimiter(5, 60.0, clock=clock)
    sql = SqliteRateLimiter(db, 5, 60.0, clock=clock)
    steps = [(who, dt) for dt in (0, 1, 7, 13, 29, 31, 61, 90) for who in ("a", "b", "a", "a")] * 3
    for who, dt in steps:
        clock.t += dt
        assert sql.check(who) == pytest.approx(mem.check(who))


def test_memory_and_sqlite_idempotency_agree(db: Path) -> None:
    clock = Clock()
    mem = IdempotencyStore(50.0, clock=clock)
    sql = SqliteIdempotencyStore(db, 50.0, clock=clock)
    answer = StoredResponse(200, {"a": 1})
    script = [
        ("begin", "k1", "x"), ("begin", "k1", "x"), ("begin", "k1", "y"), ("finish", "k1", ""),
        ("begin", "k1", "x"), ("begin", "k2", "x"), ("abort", "k2", ""), ("begin", "k2", "z"),
        ("tick", "", "60"), ("begin", "k1", "x"), ("begin", "k2", "z"),
    ]  # fmt: skip
    for op, key, fp in script:
        if op == "tick":
            clock.t += float(fp)
        elif op == "begin":
            a, b = mem.begin("s", key, fp), sql.begin("s", key, fp)
            assert (a.kind, a.stored) == (b.kind, b.stored)
        elif op == "finish":
            mem.finish("s", key, answer)
            sql.finish("s", key, answer)
        else:
            mem.abort("s", key)
            sql.abort("s", key)


def test_memory_and_sqlite_plan_stores_agree(db: Path) -> None:
    clock = Clock()
    mem, sql = PlanStore(100.0, clock=clock), SqlitePlanStore(db, 100.0, clock=clock)
    for who, plan_id in (("me", "p"), ("me", "p"), ("you", "p"), ("me", "q")):
        a = mem.save(plan_id, who, validated(), ["w"], 1.0)
        b = sql.save(plan_id, who, validated(), ["w"], 1.0)
        assert a == b
    for who, plan_id in (("me", "p"), ("you", "p"), ("me", "q"), ("me", "zzz")):
        assert mem.get(plan_id, who) == sql.get(plan_id, who)


# --- file hygiene -----------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_state_file_is_private_to_its_owner(db: Path) -> None:
    SqlitePlanStore(db)
    assert (os.stat(db).st_mode & 0o777) == 0o600


def test_a_failed_write_rolls_back(db: Path) -> None:
    clock = Clock()
    store = SqliteRateLimiter(db, limit=2, clock=clock)
    store.check("a")
    original = clock.__call__

    def boom() -> float:
        raise RuntimeError("clock failed")

    store._clock = boom  # type: ignore[assignment]  # the failure happens before any write
    with pytest.raises(RuntimeError):
        store.check("a")
    store._clock = original
    assert (
        store.check("a") == 0.0 and store.check("a") > 0
    )  # one earlier hit, not lost, not doubled
