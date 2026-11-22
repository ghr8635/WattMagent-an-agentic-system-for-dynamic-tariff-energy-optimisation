"""The data behind the web page: one planned day, recent decisions, and the audit tail."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.test_api_core import NOW, fresh_service
from wattmagent.api.service import PlanService
from wattmagent.api.ui_views import MAX_DECISIONS, UiError, build_day_view, local_day_slots
from wattmagent.appliances.household import load_household
from wattmagent.graph.context import AgentContext
from wattmagent.safety.audit import InMemoryAuditLog, JsonlAuditLog

ROOT = Path(__file__).resolve().parents[2]
BERLIN = ZoneInfo("Europe/Berlin")


def house(name: str = "example_full"):  # type: ignore[no-untyped-def]
    return load_household(ROOT / "config" / "households" / f"{name}.toml")


def service(
    tmp_path: Path, name: str = "example_full", audit: InMemoryAuditLog | None = None
) -> PlanService:
    svc = fresh_service(tmp_path, audit)
    svc.household = house(name)
    return svc


# --- local days -------------------------------------------------------------------------------


def test_a_local_day_has_24_hours_and_starts_at_local_midnight() -> None:
    slots = local_day_slots(datetime(2026, 3, 10).date(), BERLIN)
    assert len(slots) == 24 and slots[0].astimezone(BERLIN).hour == 0
    assert slots[-1].astimezone(BERLIN).hour == 23
    assert all(b - a == timedelta(hours=1) for a, b in zip(slots, slots[1:], strict=False))


@pytest.mark.parametrize(("day", "hours"), [("2026-03-29", 23), ("2026-10-25", 25)])
def test_daylight_saving_days_have_23_or_25_hours(day: str, hours: int) -> None:
    slots = local_day_slots(datetime.fromisoformat(day).date(), BERLIN)
    assert len(slots) == hours
    assert len({s.astimezone(BERLIN).isoformat() for s in slots}) == hours  # no repeated label


# --- the day view -----------------------------------------------------------------------------


def test_the_day_view_describes_prices_schedule_energy_and_cost(tmp_path: Path) -> None:
    view = service(tmp_path).day_view()
    assert (
        view["decision"] == "optimised" and view["day"] == "today" and view["date"] == "2026-03-10"
    )
    n = len(view["slots"])
    # a part the household does not have (battery, tank) comes back as an empty series
    assert n == 24 and all(len(v) in (0, n) for v in view["series"].values() if isinstance(v, list))
    assert all(len(v) == n for v in view["series"]["cycles_kw"].values())
    assert {i["appliance"] for i in view["items"]} == {
        "washing machine",
        "dishwasher",
        "tumble dryer heat pump",
    }
    for item in view["items"]:
        assert datetime.fromisoformat(item["start_at"]).utcoffset() is not None
        assert item["end_at"] > item["start_at"] and item["energy_kwh"] > 0
    cost = view["cost"]
    assert cost["saving_eur"] == pytest.approx(
        cost["run_immediately_eur"] - cost["planned_eur"], abs=1e-4
    )
    assert cost["saving_eur"] >= -1e-9  # the optimiser never does worse than starting at once
    assert view["left_out"] == ["heat pump", "solar"]
    assert any("not measured" in line for line in view["labels"])


def test_elapsed_hours_are_marked_by_the_clock(tmp_path: Path) -> None:
    view = service(tmp_path).day_view()
    elapsed = [s["elapsed"] for s in view["slots"]]
    # NOW is 17:20 UTC = 18:20 Berlin: the hours before 18:00 have passed, 18:00 has not ended
    assert elapsed == [True] * 18 + [False] * 6


def test_the_energy_series_add_up_to_the_planned_items(tmp_path: Path) -> None:
    view = service(tmp_path).day_view()
    total_cycles = sum(sum(v) for v in view["series"]["cycles_kw"].values())
    assert total_cycles == pytest.approx(sum(i["energy_kwh"] for i in view["items"]), abs=1e-2)


def test_a_battery_household_shows_its_battery_and_tank(tmp_path: Path) -> None:
    view = service(tmp_path, "example_battery").day_view()
    s = view["series"]
    assert any(v > 0 for v in s["battery_charge_kw"]) and len(s["battery_stored_kwh"]) == 24
    assert len(s["tank_temp_c"]) == 24 and view["cost"]["basis"].startswith("whole net grid bill")
    assert not any(
        c > 1e-9 and d > 1e-9
        for c, d in zip(s["battery_charge_kw"], s["battery_discharge_kw"], strict=True)
    )


def test_tomorrow_works_when_its_prices_exist_and_says_so_when_they_do_not(tmp_path: Path) -> None:
    svc = service(tmp_path)
    assert svc.day_view("tomorrow")["date"] == "2026-03-11"
    svc.clock = lambda: NOW + timedelta(days=3)  # the fixture holds four days of prices only
    svc._view = None
    with pytest.raises(UiError) as caught:
        svc.day_view("tomorrow")
    assert caught.value.status_code == 404 and "not available" in str(caught.value)


def test_stale_prices_show_the_fallback_not_an_optimised_plan(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.clock = lambda: NOW + timedelta(hours=48)  # prices were fetched at NOW
    view = svc.day_view()
    assert view["decision"] == "fallback" and any("stale_prices" in r for r in view["reasons"])
    assert view["cost"]["saving_eur"] == pytest.approx(0.0, abs=1e-9)  # fallback is run-immediately


def test_a_bad_day_and_a_missing_household_are_refused(tmp_path: Path) -> None:
    svc = service(tmp_path)
    with pytest.raises(UiError) as caught:
        svc.day_view("yesterday")
    assert caught.value.status_code == 422
    svc.household = None
    with pytest.raises(UiError) as caught:
        svc.day_view()
    assert caught.value.status_code == 404


def test_a_household_with_nothing_to_plan_is_refused(tmp_path: Path) -> None:
    from dataclasses import replace

    svc = service(tmp_path)
    assert svc.household is not None
    svc.household = replace(svc.household, cycles=(), tank=None, battery=None)
    with pytest.raises(UiError, match="nothing|no appliance"):
        svc.day_view()


def test_the_view_is_cached_per_day_hour_and_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wattmagent.api.service as service_module

    calls: list[str] = []
    real = service_module.build_day_view

    def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(args[3] if len(args) > 3 else "today")
        return real(*args, **kwargs)

    monkeypatch.setattr(service_module, "build_day_view", counting)
    svc = service(tmp_path)
    first = svc.day_view()
    assert svc.day_view() is first and calls == ["today"]  # no second solve
    svc.day_view("tomorrow")
    assert calls == ["today", "tomorrow"]  # another day is another view
    svc.clock = lambda: NOW + timedelta(hours=1)
    svc.day_view("tomorrow")
    assert len(calls) == 3  # a new hour recomputes


def test_building_a_view_needs_a_timezone_aware_clock(tmp_path: Path) -> None:
    svc = service(tmp_path)
    ctx: AgentContext = svc.provider.get()
    with pytest.raises(ValueError):
        build_day_view(house(), ctx, datetime(2026, 3, 10, 17, 20))


def test_building_a_view_writes_no_audit_record(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    service(tmp_path, audit=audit).day_view()
    assert audit.entries == []  # nothing was executed


# --- decisions --------------------------------------------------------------------------------


def entry(i: int, **extra: object) -> dict[str, object]:
    return {
        "schema": 1,
        "kind": "appliances",
        "request_id": f"r{i}",
        "at": f"2026-03-10T10:{i:02d}:00+00:00",
        "decision": "optimised",
        "outcome": "executed",
        "request_satisfied": True,
        "reasons": [],
        "plan": {"verified_cost_eur": 0.4, "baseline_cost_eur": 0.5},
        "verifier": {"passed": True, "rules": []},
        **extra,
    }


def test_recent_decisions_are_newest_first_and_limited(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    for i in range(30):
        audit.record(entry(i))
    rows = service(tmp_path, audit=audit).recent_decisions(5)
    assert [r["request_id"] for r in rows] == ["r29", "r28", "r27", "r26", "r25"]
    assert rows[0]["cost_eur"] == 0.4 and rows[0]["baseline_cost_eur"] == 0.5
    assert (
        len(service(tmp_path, audit=audit).recent_decisions(10_000)) == 30
    )  # capped at what exists
    assert len(service(tmp_path, audit=audit).recent_decisions(0)) == 1  # at least one is asked for
    assert MAX_DECISIONS == 100


def test_decision_rows_keep_only_known_fields_and_survive_odd_records(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    audit.record(
        entry(1, unlisted_field="do not show", reasons=["verifier_rejected: GRID_LIMIT"] * 9)
    )
    audit.record({"request_id": "odd", "reasons": "not a list", "verifier": 5, "plan": 3})
    audit.record(
        {
            "kind": "ev",
            "request_id": "e",
            "decision": "fallback",
            "verifier": {"passed": False, "rules": ["SOC_FLOOR"]},
        }
    )
    rows = service(tmp_path, audit=audit).recent_decisions(10)
    assert all("unlisted_field" not in r for r in rows)
    assert rows[2]["reasons"] == ["verifier_rejected: GRID_LIMIT"] * 5  # at most five are shown
    assert (
        rows[1]["reasons"] == []
        and rows[1]["verifier_passed"] is None
        and rows[1]["cost_eur"] is None
    )
    assert (
        rows[0]["kind"] == "ev"
        and rows[0]["rules"] == ["SOC_FLOOR"]
        and rows[0]["verifier_passed"] is False
    )
    assert "do not show" not in json.dumps(rows)


def test_no_audit_log_or_one_that_cannot_be_read_back_gives_no_rows(tmp_path: Path) -> None:
    svc = service(tmp_path)
    svc.audit = None
    assert svc.recent_decisions() == []

    class WriteOnly:
        def record(self, entry: dict[str, object]) -> None: ...

    svc.audit = WriteOnly()
    assert svc.recent_decisions() == []


# --- the audit tail ---------------------------------------------------------------------------


def test_the_file_audit_log_reads_back_the_newest_records(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    for i in range(50):
        log.record(entry(i))
    got = log.recent(3)
    assert [e["request_id"] for e in got] == ["r49", "r48", "r47"]
    assert log.recent(0) == [] and len(log.recent(500)) == 50


def test_the_tail_skips_damaged_lines_and_a_cut_off_first_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import wattmagent.safety.audit as audit_module

    path = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(path)
    for i in range(40):
        log.record(entry(i))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            "{not json\n\n[1,2]\n"
        )  # a damaged line, a blank one, valid JSON that is no record
    log.record(entry(99))
    monkeypatch.setattr(audit_module, "_TAIL_BYTES", 700)  # a tail that starts inside a record
    got = log.recent(100)
    ids = [e["request_id"] for e in got]
    assert ids[0] == "r99" and 1 < len(ids) < 41
    assert all(isinstance(e, dict) for e in got)
    assert ids == sorted(ids, key=lambda x: -int(x[1:]))  # still newest first, none repeated


def test_a_missing_audit_file_reads_as_empty(tmp_path: Path) -> None:
    log = JsonlAuditLog(tmp_path / "gone" / "audit.jsonl")
    assert log.recent(5) == []
    (tmp_path / "gone" / "audit.jsonl").unlink(missing_ok=True)
    assert log.recent(5) == []


def test_now_is_not_confused_with_utc_naive_input(tmp_path: Path) -> None:
    # the service clock is UTC; the view must work in local time and agree with the local date
    view = service(tmp_path).day_view()
    assert view["generated_at"] == NOW.isoformat() and view["timezone"] == "Europe/Berlin"
    assert view["slots"][0]["start"].startswith("2026-03-10T00:00:00+01:00")
    assert datetime.fromisoformat(view["slots"][0]["start"]).astimezone(UTC).day == 9
