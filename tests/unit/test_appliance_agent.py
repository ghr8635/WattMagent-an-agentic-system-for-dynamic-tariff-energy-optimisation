"""Appliance requests: parser, agent (windows, safety), service routing."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.unit.test_api_core import TEXT, fresh_service
from wattmagent.appliances.agent import run_appliance_agent
from wattmagent.appliances.household import load_household, parse_household
from wattmagent.appliances.request import mentions_appliance, parse_appliance_request
from wattmagent.data.prices import PricePoint, synthetic_prices
from wattmagent.graph.context import AgentContext
from wattmagent.safety.audit import InMemoryAuditLog

TZ = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parents[2]
HOUSEHOLD = load_household(ROOT / "config" / "households" / "example_full.toml")
NOW = datetime(2025, 1, 3, 15, 20, tzinfo=TZ)  # plan window starts 16:00 today


def ctx(age_h: float = 1.0, prices: list[PricePoint] | None = None) -> AgentContext:
    pts = prices if prices is not None else synthetic_prices(datetime(2025, 1, 1, tzinfo=TZ), 5, 1)
    return AgentContext(pts, NOW - timedelta(hours=age_h), TZ)


def run(text: str, **kw: object) -> object:
    return run_appliance_agent(text, HOUSEHOLD, ctx(), NOW, **kw)  # type: ignore[arg-type]


# --- parser ---------------------------------------------------------------------------------

T = time
CASES: list[tuple[str, dict[str, tuple[time | None, time | None]]]] = [
    # text -> {appliance name: (deadline, earliest)}
    ("run the dishwasher before 6pm", {"dishwasher": (T(18), None)}),
    ("dishwasher by 18:00", {"dishwasher": (T(18), None)}),
    ("washing machine done by noon", {"washing_machine": (T(12), None)}),
    ("start the dryer after 9am", {"tumble_dryer_heat_pump": (None, T(9))}),
    ("dryer after 9am, done by 10pm", {"tumble_dryer_heat_pump": (T(22), T(9))}),
    ("dishwasher not before 20:00", {"dishwasher": (None, T(20))}),
    (
        "washer and dishwasher by 5:30pm",
        {"washing_machine": (T(17, 30), None), "dishwasher": (T(17, 30), None)},
    ),
    ("dishwasher by half past five", {"dishwasher": (T(5, 30), None)}),  # bare = 24-hour clock
    ("washing machine before 7 in the morning", {"washing_machine": (T(7), None)}),
    ("dishwasher before six pm", {"dishwasher": (T(18), None)}),
    ("run the dishwasher", {"dishwasher": (None, None)}),
    ("wash the dishes by 9pm", {"dishwasher": (T(21), None)}),
    ("laundry by 5pm", {"washing_machine": (T(17), None)}),
    (
        "dishwasher before 6pm and washing machine before noon",
        {"dishwasher": (T(18), None), "washing_machine": (T(12), None)},
    ),
    (
        "dishwasher and dryer, not before 10pm",
        {"dishwasher": (None, T(22)), "tumble_dryer_heat_pump": (None, T(22))},
    ),
    ("tumble dryer after 1pm", {"tumble_dryer_heat_pump": (None, T(13))}),
    ("DISHWASHER BEFORE 6PM", {"dishwasher": (T(18), None)}),
    ("dishwasher until midnight", {"dishwasher": (T(0), None)}),
    ("washing machine from 8am until 4pm", {"washing_machine": (T(16), T(8))}),
]


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_parser_cases(text: str, expected: dict[str, tuple[time | None, time | None]]) -> None:
    parsed = parse_appliance_request(text, HOUSEHOLD)
    got = {a.entry.name: (a.deadline, a.earliest) for a in parsed.asks}
    assert got == expected
    assert not parsed.ambiguous and not parsed.mentions_ev


def test_parser_finds_nothing_in_other_requests() -> None:
    for text in ("turn on the heater", "what is the weather", "80% by 7am"):
        assert not parse_appliance_request(text, HOUSEHOLD).asks
        assert not mentions_appliance(text, HOUSEHOLD)


def test_ev_words_are_detected() -> None:
    parsed = parse_appliance_request("charge the car to 80% and run the dishwasher", HOUSEHOLD)
    assert parsed.mentions_ev and parsed.asks


def test_a_new_catalogue_appliance_is_understood_without_code() -> None:
    hh = parse_household(
        {
            "catalogue": {"cycle": {"pottery_kiln": {"profile_kw": [2.0, 2.0, 1.0]}}},
            "cycle": [{"appliance": "pottery_kiln"}],
        }
    )
    parsed = parse_appliance_request("run the pottery kiln by 7am", hh)
    assert [(a.entry.name, a.deadline) for a in parsed.asks] == [("pottery_kiln", T(7))]


def test_two_matching_appliances_are_reported_as_ambiguous() -> None:
    hh = parse_household(
        {
            "cycle": [
                {"appliance": "dishwasher", "name": "dish_a"},
                {"appliance": "dishwasher", "name": "dish_b"},
            ]
        }
    )
    parsed = parse_appliance_request("dishwasher before 6pm", hh)
    assert parsed.ambiguous == ("dishwasher",) and not parsed.asks
    specific = parse_appliance_request("dish a before 6pm", hh)
    assert [a.entry.name for a in specific.asks] == ["dish_a"]


# --- agent: windows and results ---------------------------------------------------------------


def facts(text: str) -> dict[str, object]:
    resp = run(text)
    assert resp.status == "planned", resp.message  # type: ignore[attr-defined]
    return resp.facts  # type: ignore[attr-defined,no-any-return]


def test_a_plan_stays_inside_its_window_and_is_verified() -> None:
    audit = InMemoryAuditLog()
    resp = run("start the dryer after 9am, done by 10pm", audit=audit)
    assert resp.status == "planned" and resp.decision == "optimised"  # type: ignore[attr-defined]
    cycle = resp.facts["cycles"][0]  # type: ignore[attr-defined]
    assert cycle["window_start"] == "09:00" and cycle["window_start_day"] == "tomorrow"
    assert cycle["start"] >= "09:00" and cycle["start_day"] == "tomorrow"
    assert cycle["cost_eur"] <= cycle["cost_if_started_at_window_start_eur"] + 1e-9
    assert len(audit.entries) == 1 and audit.entries[0]["kind"] == "appliances"
    assert "dryer" not in json.dumps(audit.entries[0])  # names stay out of the audit log


def test_off_the_hour_times_are_rounded_to_stay_inside_the_ask() -> None:
    cycle = facts("washing machine after 9:30, done by 17:30")["cycles"][0]  # type: ignore[index]
    assert cycle["start"] >= "10:00" and cycle["end"] <= "17:00"
    assert cycle["start_day"] == "tomorrow"


def test_both_appliances_get_their_own_window() -> None:
    cycles = facts("dishwasher before 8pm and washing machine before noon")["cycles"]  # type: ignore[index]
    by = {c["appliance"]: c for c in cycles}
    assert (
        by["dishwasher"]["window_end"] == "20:00" and by["washing machine"]["window_end"] == "12:00"
    )


def test_a_window_that_is_too_short_asks_for_a_wider_one() -> None:
    resp = run("run the dishwasher before 6pm")  # 16:00-18:00 is 2 h; it needs 3
    assert resp.status == "needs_input"  # type: ignore[attr-defined]
    assert "needs 3 hours" in resp.message  # type: ignore[attr-defined]


def test_a_deadline_beyond_the_horizon_is_explained() -> None:
    resp = run("dishwasher before 4pm")  # 16:00 today is the window start: read as tomorrow
    assert resp.status == "planned"  # type: ignore[attr-defined]
    assert "beyond the end of the 24 hours" in resp.message  # type: ignore[attr-defined]


def test_an_earliest_after_the_clock_deadline_moves_the_deadline_forward() -> None:
    cycle = facts("dryer after 20:00, done by 18:00")["cycles"][0]  # type: ignore[index]
    assert cycle["window_start"] == "20:00" and cycle["window_start_day"] == "today"


def test_no_time_at_all_uses_the_whole_next_24_hours() -> None:
    cycle = facts("washing machine")["cycles"][0]  # type: ignore[index]
    assert cycle["window_start"] == "16:00" and cycle["window_end"] == "16:00"


def test_mixed_ambiguous_and_unknown_requests_ask_instead_of_guessing() -> None:
    mixed = run("charge the car to 80% and run the dishwasher")
    assert mixed.status == "needs_input" and "one request at a time" in mixed.message  # type: ignore[attr-defined]
    unknown = run("run the toaster")
    assert unknown.status == "needs_input" and "I know:" in unknown.message  # type: ignore[attr-defined]


def test_now_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run_appliance_agent("dishwasher", HOUSEHOLD, ctx(), datetime(2025, 1, 3, 15, 20))


# --- agent: safety --------------------------------------------------------------------------


def test_stale_prices_force_the_fallback_and_say_why() -> None:
    resp = run_appliance_agent("washing machine by 9pm", HOUSEHOLD, ctx(age_h=60), NOW)
    assert resp.decision == "fallback"
    assert "stale_prices" in resp.message
    cycle = resp.facts["cycles"][0]  # type: ignore[index]
    assert cycle["start"] == "16:00" and cycle["cost_eur"] is None  # no euro figure on bad prices


def test_missing_hours_force_the_fallback() -> None:
    pts = [p for p in ctx().prices if p.start.hour != 21]
    resp = run_appliance_agent("washing machine by 9am", HOUSEHOLD, ctx(prices=pts), NOW)
    assert resp.decision == "fallback" and "missing" in resp.message


def test_a_household_that_cannot_satisfy_its_connection_limit_is_refused() -> None:
    tight = replace(HOUSEHOLD, max_grid_kw=0.3)
    resp = run_appliance_agent("washing machine", tight, ctx(), NOW)
    assert resp.status == "error" and "No safe plan" in resp.message


# --- service routing ------------------------------------------------------------------------


def test_service_routes_appliance_text_and_keeps_the_car_path(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    svc = fresh_service(tmp_path, audit)
    svc.household = HOUSEHOLD
    status, body = svc.plan("washing machine by 6am", "req-1", owner="o")
    assert status == 200 and body["status"] == "planned"
    assert body["explanation_source"] == "template" and body["model_calls"] == 0
    assert "plan_id" not in body  # replanning exists only for the car
    assert body["facts"]["cycles"][0]["appliance"] == "washing machine"
    status, body = svc.plan(TEXT, "req-2", owner="o")  # the car request still works
    assert status == 200 and body["status"] == "planned" and "plan_id" in body
    assert [e.get("kind", "ev") for e in audit.entries] == ["appliances", "ev"]


def test_without_a_household_appliance_text_is_a_car_request(tmp_path: Path) -> None:
    status, body = fresh_service(tmp_path).plan("run the dishwasher before 6am", "req-1")
    assert body["status"] != "planned" or "cycles" not in (body["facts"] or {})
    assert status in (200, 422)
