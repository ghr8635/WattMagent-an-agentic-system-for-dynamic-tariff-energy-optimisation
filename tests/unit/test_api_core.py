"""API building blocks that need no web framework: keys, rate limit, idempotency, service."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wattmagent.api.idempotency import IdempotencyStore, Kind, StoredResponse
from wattmagent.api.ratelimit import RateLimiter
from wattmagent.api.security import KeyRing, key_id
from wattmagent.api.service import CsvContextProvider, PlanService, ServiceUnavailable
from wattmagent.data.prices import PricePoint, synthetic_prices
from wattmagent.safety.audit import InMemoryAuditLog

NOW = datetime(2026, 3, 10, 17, 20, tzinfo=UTC)
TEXT = "Have the car at 90% by 6:30 tomorrow, and don't charge before 22:00."
KEY = "k" * 20


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def write_prices(path: Path, days: int = 4) -> None:
    pts: list[PricePoint] = synthetic_prices(datetime(2026, 3, 9, tzinfo=UTC), days, seed=1)
    path.write_text(
        "timestamp,price_eur_mwh\n"
        + "\n".join(f"{p.start.isoformat()},{p.eur_per_kwh * 1000:.3f}" for p in pts),
        encoding="utf-8",
    )


def fresh_service(tmp_path: Path, audit: InMemoryAuditLog | None = None) -> PlanService:
    csv = tmp_path / "prices.csv"
    write_prices(csv)
    mtime = NOW.timestamp()
    os.utime(csv, (mtime, mtime))  # "fetched" at NOW
    provider = CsvContextProvider(csv, ZoneInfo("Europe/Berlin"))
    return PlanService(provider, audit=audit, clock=lambda: NOW)


# ----------------------------------------------------------------------------- keys


def test_key_ring_accepts_only_known_keys() -> None:
    ring = KeyRing([KEY, "another-key-0123456789"])
    assert ring.authenticate(KEY) == key_id(KEY)
    assert ring.authenticate("another-key-0123456789") == key_id("another-key-0123456789")
    assert ring.authenticate("wrong-key-0123456789") is None
    assert ring.authenticate("") is None
    assert ring.authenticate(None) is None


def test_key_id_does_not_contain_the_key() -> None:
    assert KEY not in key_id(KEY) and len(key_id(KEY)) == 12


@pytest.mark.parametrize("keys", [[], [" "], ["short"]])
def test_key_ring_refuses_missing_or_weak_keys(keys: list[str]) -> None:
    with pytest.raises(ValueError):
        KeyRing(keys)


# ------------------------------------------------------------------------ rate limit


def test_rate_limiter_blocks_then_recovers() -> None:
    clock = Clock()
    limiter = RateLimiter(2, window_s=60, clock=clock)
    assert limiter.check("a") == 0 and limiter.check("a") == 0
    wait = limiter.check("a")
    assert 0 < wait <= 60
    assert limiter.check("b") == 0  # other callers are independent
    clock.t += 61
    assert limiter.check("a") == 0


def test_rate_limiter_rejects_bad_settings() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)


# ------------------------------------------------------------------------ idempotency


def test_idempotency_replay_conflict_and_in_flight() -> None:
    store = IdempotencyStore()
    assert store.begin("c", "key-00001", "f1").kind is Kind.FRESH
    assert store.begin("c", "key-00001", "f1").kind is Kind.IN_FLIGHT
    assert store.begin("c", "key-00001", "other").kind is Kind.CONFLICT
    stored = StoredResponse(200, {"x": 1})
    store.finish("c", "key-00001", stored)
    again = store.begin("c", "key-00001", "f1")
    assert again.kind is Kind.REPLAY and again.stored == stored
    assert store.begin("d", "key-00001", "f1").kind is Kind.FRESH  # scoped per caller


def test_idempotency_abort_allows_retry_and_entries_expire() -> None:
    clock = Clock()
    store = IdempotencyStore(ttl_s=10, clock=clock)
    store.begin("c", "key-00002", "f")
    store.abort("c", "key-00002")
    assert store.begin("c", "key-00002", "f").kind is Kind.FRESH
    store.finish("c", "key-00002", StoredResponse(200, {}))
    clock.t += 11
    assert store.begin("c", "key-00002", "f").kind is Kind.FRESH


def test_idempotency_abort_does_not_drop_a_finished_entry() -> None:
    store = IdempotencyStore()
    store.begin("c", "key-00003", "f")
    store.finish("c", "key-00003", StoredResponse(200, {}))
    store.abort("c", "key-00003")
    assert store.begin("c", "key-00003", "f").kind is Kind.REPLAY


# ----------------------------------------------------------------------------- service


def test_service_plans_and_audits(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    status, body = fresh_service(tmp_path, audit).plan(TEXT, "req-1")
    assert status == 200 and body["status"] == "planned" and body["decision"] == "optimised"
    assert body["request_id"] == "req-1" and "trace" not in body
    assert len(audit.entries) == 1


def test_service_returns_questions_as_200_and_errors_as_422(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    status, body = svc.plan("charge my car", "r")
    assert status == 200 and body["status"] == "needs_input"
    status, body = svc.plan("x" * 501, "r")
    assert status == 422 and body["status"] == "error"


def test_service_ready_and_not_ready(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    assert svc.ready() == []
    later = PlanService(svc.provider, clock=lambda: NOW + timedelta(days=3))
    assert any("stale" in r or "missing" in r for r in later.ready())


def test_missing_price_file_is_service_unavailable(tmp_path: Path) -> None:
    provider = CsvContextProvider(tmp_path / "nope.csv", ZoneInfo("Europe/Berlin"))
    svc = PlanService(provider, clock=lambda: NOW)
    with pytest.raises(ServiceUnavailable):
        svc.plan(TEXT, "r")
    assert svc.ready() and "unavailable" in svc.ready()[0]


def test_provider_reloads_when_the_file_changes(tmp_path: Path) -> None:
    csv = tmp_path / "p.csv"
    write_prices(csv, days=3)
    provider = CsvContextProvider(csv, ZoneInfo("Europe/Berlin"))
    first = provider.get()
    assert provider.get() is first  # unchanged file: cached
    write_prices(csv, days=4)
    os.utime(csv, (NOW.timestamp(), NOW.timestamp()))
    assert len(provider.get().prices) > len(first.prices)
