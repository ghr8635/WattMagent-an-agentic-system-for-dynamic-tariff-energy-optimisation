"""Replanning: the agent entry point, the plan store and the service methods."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_agent import NOW, TEXT, context, deps
from tests.unit.test_api_core import fresh_service
from wattmagent.api.plans import PlanStore
from wattmagent.api.service import PlanService
from wattmagent.core.errors import ReplanError
from wattmagent.graph import run_agent
from wattmagent.graph.agent import run_replan
from wattmagent.graph.context import AgentContext
from wattmagent.graph.validate import Validated
from wattmagent.safety.audit import InMemoryAuditLog


def first_plan() -> tuple[Validated, dict[str, Any]]:
    resp = run_agent(TEXT, deps(), NOW)
    assert resp.status == "planned" and resp.validated is not None and resp.facts is not None
    return resp.validated, resp.facts


# ------------------------------------------------------------------------------ agent


def test_replan_with_a_fuller_car_needs_less_energy() -> None:
    validated, facts = first_plan()
    resp = run_replan(validated, 0.60, deps(), NOW)
    assert resp.status == "planned" and resp.decision == "optimised"
    assert resp.facts is not None and resp.facts["ev_grid_kwh"] < facts["ev_grid_kwh"]
    assert resp.facts["start_percent"] == 60
    assert not any("unknown" in n for n in resp.facts["notes"])  # the 30% assumption is gone
    assert any("reported charge of 60%" in n for n in resp.facts["notes"])
    assert resp.trace[0].startswith("replan:")


def test_replan_when_the_car_is_already_full_plans_no_charging() -> None:
    validated, _ = first_plan()
    resp = run_replan(validated, 0.95, deps(), NOW)
    assert resp.status == "planned" and resp.facts is not None
    assert resp.facts["charge_windows"] == [] and "No charging is needed" in resp.message


def test_replan_later_in_the_night_still_meets_the_request() -> None:
    validated, _ = first_plan()
    later = NOW + timedelta(hours=3)
    resp = run_replan(validated, 0.45, deps(fetched_at=later), later)
    assert resp.status == "planned" and resp.request_satisfied is True


def test_a_passed_not_before_time_is_dropped() -> None:
    validated, _ = first_plan()
    assert validated.earliest_at is not None
    later = NOW + timedelta(hours=5)
    resp = run_replan(validated, 0.45, deps(fetched_at=later), later)
    assert resp.validated is not None and resp.validated.earliest_at is None


def test_replan_is_refused_when_no_full_hour_is_left() -> None:
    validated, _ = first_plan()
    almost = validated.depart_at - timedelta(minutes=30)
    with pytest.raises(ReplanError) as err:
        run_replan(validated, 0.5, deps(fetched_at=almost), almost)
    assert err.value.status_code == 409


def test_replan_validates_its_inputs() -> None:
    validated, _ = first_plan()
    with pytest.raises(ReplanError) as err:
        run_replan(validated, 1.2, deps(), NOW)
    assert err.value.status_code == 422
    with pytest.raises(ValueError):
        run_replan(validated, 0.5, deps(), NOW.replace(tzinfo=None))


# ------------------------------------------------------------------------------ store


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_store_scopes_plans_to_their_owner_and_counts_versions() -> None:
    validated, _ = first_plan()
    store = PlanStore()
    one = store.save("p" * 32, "alice", validated, ["03:00-06:00"], 7.5)
    two = store.save("p" * 32, "alice", validated, ["02:00-05:00"], 7.1)
    assert (one.version, two.version) == (1, 2)
    got = store.get("p" * 32, "alice")
    assert got is not None and got.charge_windows == ["02:00-05:00"]
    assert store.get("p" * 32, "mallory") is None and store.get("x" * 32, "alice") is None


def test_store_forgets_plans_after_the_ttl() -> None:
    validated, _ = first_plan()
    clock = Clock()
    store = PlanStore(ttl_s=10, clock=clock)
    store.save("p" * 32, "alice", validated, [], None)
    clock.t = 11
    assert store.get("p" * 32, "alice") is None
    with pytest.raises(ValueError):
        PlanStore(ttl_s=0)


# --------------------------------------------------------------------------- service


def test_plan_then_replan_through_the_service(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    svc = fresh_service(tmp_path, audit)
    status, first = svc.plan(TEXT, "r1", owner="alice")
    assert status == 200 and len(first["plan_id"]) == 32 and first["version"] == 1
    plan_id = first["plan_id"]

    status, second = svc.replan(plan_id, "alice", 60, None, "r2")
    assert status == 200 and second["plan_id"] == plan_id and second["version"] == 2
    assert second["request_id"] == "r2"
    assert second["previous"]["charge_windows"] == first["facts"]["charge_windows"]
    assert second["facts"]["ev_grid_kwh"] < first["facts"]["ev_grid_kwh"]
    assert isinstance(second["changed"], bool)
    assert audit.entries[-1]["request_id"] == f"{plan_id}.r2"  # ties the replan to its plan

    _, third = svc.replan(plan_id, "alice", 70, None, "r3")
    assert third["version"] == 3 and third["previous"]["version"] == 2


def test_unclear_requests_are_not_remembered(tmp_path: Path) -> None:
    status, body = fresh_service(tmp_path).plan("charge my car", "r", owner="alice")
    assert status == 200 and body["status"] == "needs_input" and "plan_id" not in body


def test_replan_of_an_unknown_or_foreign_plan_is_not_found(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    _, first = svc.plan(TEXT, "r1", owner="alice")
    for owner, plan_id in (("alice", "0" * 32), ("mallory", first["plan_id"])):
        with pytest.raises(ReplanError) as err:
            svc.replan(plan_id, owner, 50, None, "r")
        assert err.value.status_code == 404


def test_battery_charge_is_checked_against_the_battery(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    _, first = svc.plan(TEXT, "r1", owner="alice")
    with pytest.raises(ReplanError) as err:
        svc.replan(first["plan_id"], "alice", 50, 5, "r")  # below the 10% reserve
    assert err.value.status_code == 422
    status, body = svc.replan(first["plan_id"], "alice", 50, 80, "r")
    assert status == 200 and body["status"] == "planned"


def test_battery_charge_without_a_battery_is_refused() -> None:
    class NoBattery:
        def get(self) -> AgentContext:
            return context(battery=None)

    svc = PlanService(NoBattery(), clock=lambda: NOW)
    _, first = svc.plan(TEXT, "r1", owner="alice")
    with pytest.raises(ReplanError) as err:
        svc.replan(first["plan_id"], "alice", 50, 60, "r")
    assert err.value.status_code == 422
    assert svc.replan(first["plan_id"], "alice", 50, None, "r")[0] == 200  # fine without it


def test_replan_after_departure_is_refused(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    _, first = svc.plan(TEXT, "r1", owner="alice")
    late = PlanService(svc.provider, store=svc.store, clock=lambda: NOW + timedelta(hours=14))
    with pytest.raises(ReplanError) as err:
        late.replan(first["plan_id"], "alice", 50, None, "r")
    assert err.value.status_code == 409
