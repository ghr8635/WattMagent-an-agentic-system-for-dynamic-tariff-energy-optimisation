from __future__ import annotations

import json
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wattmagent.backtest.household import Scenario, simulate_day
from wattmagent.data.load import synthetic_load_kw
from wattmagent.data.prices import PricePoint
from wattmagent.devices.battery import BatterySpec
from wattmagent.devices.ev import EVSpec
from wattmagent.optimiser import household as optimiser_module
from wattmagent.optimiser.household import OptimiserError, plan_household
from wattmagent.planning.household import (
    HouseholdInputs,
    HouseholdPlan,
    PlanStatus,
    ev_immediately,
)
from wattmagent.safety import (
    Decision,
    InMemoryAuditLog,
    JsonlAuditLog,
    PlanRejected,
    PriceSnapshot,
    Rule,
    check_price_snapshot,
    plan_with_safety,
    verify_plan,
)

EV = EVSpec()
DEFAULT_BATTERY = BatterySpec()
START = datetime(2025, 3, 10, 17, tzinfo=UTC)  # 18:00 local in winter
NOW = START - timedelta(hours=3)


def make_inputs(
    prices: list[float] | None = None,
    battery: BatterySpec | None = DEFAULT_BATTERY,
    ev: EVSpec = EV,
) -> HouseholdInputs:
    prices = prices or [0.05 + 0.4 * i / 23 for i in range(24)]  # ascending: cheap first
    hours = [(18 + i) % 24 for i in range(len(prices))]
    return HouseholdInputs(
        prices=prices,
        load_kw=synthetic_load_kw(hours),
        ev_available=[i < ev.window_hours for i in range(len(prices))],
        ev=ev,
        battery=battery,
    )


def snapshot(inputs: HouseholdInputs, fetched_at: datetime | None = None) -> PriceSnapshot:
    pts = [PricePoint(START + timedelta(hours=i), p) for i, p in enumerate(inputs.prices)]
    return PriceSnapshot(pts, fetched_at or NOW)


def rules_of(inputs: HouseholdInputs, plan: HouseholdPlan, **kw: bool) -> set[str]:
    return set(verify_plan(inputs, plan, **kw).rules)


def good_plan(inputs: HouseholdInputs) -> HouseholdPlan:
    return plan_household(inputs)


# ---------------------------------------------------------------- verifier


def test_a_good_optimiser_plan_passes_and_cost_matches_the_simulator() -> None:
    inputs = make_inputs()
    plan = good_plan(inputs)
    report = verify_plan(inputs, plan)
    assert report.passed, report.violations
    window = [PricePoint(START + timedelta(hours=i), p) for i, p in enumerate(inputs.prices)]
    day = simulate_day(window, Scenario(EV, inputs.battery), plan, inputs.load_kw)
    assert report.cost_eur == pytest.approx(day.bill_eur + day.wear_eur, abs=1e-9)
    assert report.baseline_cost_eur is not None and report.cost_eur <= report.baseline_cost_eur


def test_missed_target_is_rejected() -> None:
    inputs = make_inputs()
    n = inputs.slots
    assert Rule.EV_TARGET.value in rules_of(
        inputs, HouseholdPlan(PlanStatus.RULE, [0.0] * n, [0.0] * n)
    )


def test_charging_while_unplugged_and_power_limits_are_rejected() -> None:
    inputs = make_inputs()
    base = ev_immediately(inputs)
    unplugged = [*base.ev_kw[:-1], 5.0]  # last slot: car is gone
    assert Rule.EV_UNPLUGGED.value in rules_of(
        inputs, HouseholdPlan(PlanStatus.RULE, unplugged, base.battery_kw)
    )
    too_much = [20.0, *base.ev_kw[1:]]
    assert Rule.EV_POWER.value in rules_of(
        inputs, HouseholdPlan(PlanStatus.RULE, too_much, base.battery_kw)
    )
    negative = [-3.0, *base.ev_kw[1:]]
    assert Rule.EV_POWER.value in rules_of(
        inputs, HouseholdPlan(PlanStatus.RULE, negative, base.battery_kw)
    )


def test_wrong_shape_and_non_finite_numbers_are_rejected() -> None:
    inputs = make_inputs()
    base = ev_immediately(inputs)
    short = HouseholdPlan(PlanStatus.RULE, base.ev_kw[:-1], base.battery_kw)
    assert rules_of(inputs, short) == {Rule.SHAPE.value}
    nan = HouseholdPlan(PlanStatus.RULE, [float("nan"), *base.ev_kw[1:]], base.battery_kw)
    assert rules_of(inputs, nan) == {Rule.NOT_FINITE.value}
    inf = HouseholdPlan(PlanStatus.RULE, base.ev_kw, [float("inf"), *base.battery_kw[1:]])
    assert rules_of(inputs, inf) == {Rule.NOT_FINITE.value}


def test_infeasible_plan_is_not_a_schedule() -> None:
    inputs = make_inputs()
    plan = HouseholdPlan(PlanStatus.INFEASIBLE, message="nope")
    assert rules_of(inputs, plan) == {Rule.NO_SCHEDULE.value}


def test_battery_rules() -> None:
    inputs = make_inputs()
    base = ev_immediately(inputs)
    n = inputs.slots

    def with_battery(values: dict[int, float]) -> HouseholdPlan:
        kw = [values.get(t, 0.0) for t in range(n)]
        return HouseholdPlan(PlanStatus.RULE, base.ev_kw, kw)

    assert Rule.BATTERY_POWER.value in rules_of(inputs, with_battery({3: 9.0}))
    # 5 kW out of a battery with ~3.8 kWh usable, into a 0.7 kW house: cannot be done
    assert Rule.BATTERY_NOT_EXECUTABLE.value in rules_of(inputs, with_battery({13: -5.0}))
    # executable but leaves the battery lower than it started
    assert rules_of(inputs, with_battery({13: -0.3})) == {Rule.BATTERY_END.value}
    # a battery plan for a house without a battery
    no_battery = replace(inputs, battery=None)
    assert Rule.NO_BATTERY.value in rules_of(no_battery, with_battery({3: 1.0}))


def test_ev_request_the_car_cannot_accept_is_rejected() -> None:
    nearly_full = EVSpec(arrival_soc=0.9, target_soc=1.0)  # needs only ~6.5 kWh from the grid
    inputs = make_inputs(ev=nearly_full)
    base = ev_immediately(inputs)
    extra = list(base.ev_kw)
    extra[12] = 11.0  # the car is full long before this hour
    assert Rule.EV_NOT_ACCEPTED.value in rules_of(
        inputs, HouseholdPlan(PlanStatus.RULE, extra, base.battery_kw)
    )


def test_a_plan_costlier_than_charging_immediately_is_rejected() -> None:
    inputs = make_inputs()  # prices ascend, so charging at the end is the worst choice
    n = inputs.slots
    ev_kw = [0.0] * n
    for t in (10, 11, 12):  # three full-power hours at the end of the plug-in window
        ev_kw[t] = 11.0
    report = verify_plan(inputs, HouseholdPlan(PlanStatus.RULE, ev_kw, [0.0] * n))
    assert report.rules == [Rule.COST.value]


# --------------------------------------------------------------- freshness


def test_fresh_complete_prices_are_accepted() -> None:
    inputs = make_inputs()
    assert check_price_snapshot(snapshot(inputs), now=NOW, window_start=START, slots=24) == []


def test_stale_future_empty_missing_and_implausible_prices_are_flagged() -> None:
    inputs = make_inputs()

    def reasons(snap: PriceSnapshot) -> str:
        return " | ".join(check_price_snapshot(snap, now=NOW, window_start=START, slots=24))

    assert "stale_prices" in reasons(snapshot(inputs, NOW - timedelta(hours=40)))
    assert "fetched_in_future" in reasons(snapshot(inputs, NOW + timedelta(hours=2)))
    assert "no_prices" in reasons(PriceSnapshot([], NOW))
    gappy = snapshot(inputs)
    holes = PriceSnapshot([p for i, p in enumerate(gappy.points) if i not in (5, 6)], NOW)
    assert "missing_prices: 2 of 24" in reasons(holes)
    short = PriceSnapshot(list(gappy.points)[:20], NOW)
    assert "missing_prices: 4 of 24" in reasons(short)
    mwh = PriceSnapshot([PricePoint(p.start, 120.0) for p in gappy.points], NOW)  # EUR/MWh typed
    assert "implausible_price" in reasons(mwh)
    nan = PriceSnapshot([PricePoint(p.start, float("nan")) for p in gappy.points], NOW)
    assert "implausible_price" in reasons(nan)


def test_naive_timestamps_are_a_programming_error() -> None:
    inputs = make_inputs()
    with pytest.raises(ValueError, match="timezone-aware"):
        check_price_snapshot(
            snapshot(inputs), now=NOW.replace(tzinfo=None), window_start=START, slots=24
        )


# ---------------------------------------------------------------- pipeline


def test_happy_path_uses_the_optimiser_and_writes_one_audit_record() -> None:
    inputs, log = make_inputs(), InMemoryAuditLog()
    res = plan_with_safety(
        inputs, snapshot=snapshot(inputs), window_start=START, now=NOW, audit=log, request_id="r1"
    )
    assert res.decision is Decision.OPTIMISED and res.reasons == [] and res.request_satisfied
    assert res.request_id == "r1" and len(log.entries) == 1
    entry = log.entries[0]
    assert entry["decision"] == "optimised" and entry["outcome"] == "executed"
    assert entry["verifier"] == {"passed": True, "rules": []}
    assert entry["inputs"]["slots"] == 24 and len(entry["inputs"]["fingerprint"]) == 16
    json.dumps(entry)  # must be serialisable as it is


def test_audit_fingerprint_is_deterministic_and_input_sensitive() -> None:
    a, b = InMemoryAuditLog(), InMemoryAuditLog()
    inputs = make_inputs()
    plan_with_safety(inputs, audit=a, now=NOW)
    plan_with_safety(inputs, audit=a, now=NOW)
    plan_with_safety(make_inputs([0.2] * 24), audit=b, now=NOW)
    fps = [e["inputs"]["fingerprint"] for e in a.entries + b.entries]
    assert fps[0] == fps[1] != fps[2]


def test_planner_crash_falls_back_to_a_verified_plan() -> None:
    inputs, log = make_inputs(), InMemoryAuditLog()

    def crash(_: HouseholdInputs) -> HouseholdPlan:
        raise RuntimeError("boom")

    res = plan_with_safety(inputs, planner=crash, audit=log, now=NOW)
    assert res.decision is Decision.FALLBACK and res.request_satisfied
    assert res.reasons == ["planner_error: RuntimeError"]
    assert res.plan.ev_kw == ev_immediately(inputs).ev_kw and res.report.passed
    assert log.entries[0]["decision"] == "fallback"


def test_a_plan_the_verifier_rejects_is_replaced() -> None:
    inputs = make_inputs()
    n = inputs.slots

    def lazy(_: HouseholdInputs) -> HouseholdPlan:
        return HouseholdPlan(PlanStatus.OPTIMAL, [0.0] * n, [0.0] * n)  # never charges

    res = plan_with_safety(inputs, planner=lazy, now=NOW)
    assert res.decision is Decision.FALLBACK
    assert res.reasons == ["verifier_rejected: ev_target"]
    assert res.report.passed and sum(res.plan.ev_kw) > 0


def test_stale_prices_skip_the_planner_and_use_the_fallback() -> None:
    inputs, calls = make_inputs(), []

    def spy(i: HouseholdInputs) -> HouseholdPlan:
        calls.append(1)
        return plan_household(i)

    old = snapshot(inputs, NOW - timedelta(hours=72))
    res = plan_with_safety(inputs, planner=spy, snapshot=old, window_start=START, now=NOW)
    assert calls == [] and res.decision is Decision.FALLBACK
    assert res.reasons[0].startswith("stale_prices")


def test_missing_window_start_with_snapshot_is_an_error() -> None:
    inputs = make_inputs()
    with pytest.raises(ValueError, match="window_start"):
        plan_with_safety(inputs, snapshot=snapshot(inputs), now=NOW)


def test_unreachable_target_is_reported_and_best_effort_is_still_verified() -> None:
    weak = EVSpec(max_charge_kw=3.0, arrival_soc=0.0, target_soc=1.0)
    inputs, log = make_inputs(ev=weak), InMemoryAuditLog()
    res = plan_with_safety(inputs, audit=log, now=NOW)
    assert res.decision is Decision.FALLBACK and not res.request_satisfied
    assert any(r.startswith("infeasible_request") for r in res.reasons)
    assert max(res.plan.ev_kw) == pytest.approx(3.0)  # charges as hard as the hardware allows
    assert log.entries[0]["request_satisfied"] is False and log.entries[0]["outcome"] == "executed"


def test_if_even_the_fallback_is_unsafe_nothing_is_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, log = make_inputs(), InMemoryAuditLog()
    n = inputs.slots
    bad = HouseholdPlan(PlanStatus.RULE, [99.0] + [0.0] * (n - 1), [0.0] * n)
    monkeypatch.setattr("wattmagent.safety.pipeline.ev_immediately", lambda _: bad)

    def crash(_: HouseholdInputs) -> HouseholdPlan:
        raise RuntimeError("boom")

    with pytest.raises(PlanRejected):
        plan_with_safety(inputs, planner=crash, audit=log, now=NOW)
    assert log.entries[0]["outcome"] == "rejected"


def test_jsonl_audit_log_appends_one_line_per_decision(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "audit.jsonl"
    log = JsonlAuditLog(path)
    inputs = make_inputs()
    plan_with_safety(inputs, audit=log, now=NOW, request_id="a")
    plan_with_safety(inputs, audit=log, now=NOW, request_id="b")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["request_id"] for line in lines] == ["a", "b"]


# ------------------------------------------- solver failure paths (optimiser)


def fake_solver(**fields: object):  # noqa: ANN201
    def _milp(*_a: object, **_k: object) -> types.SimpleNamespace:
        return types.SimpleNamespace(**fields)

    return _milp


def test_solver_proving_infeasibility_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(optimiser_module, "milp", fake_solver(status=2, success=False, x=None))
    plan = plan_household(make_inputs())
    assert plan.status is PlanStatus.INFEASIBLE


def test_solver_failure_raises_and_the_pipeline_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        optimiser_module,
        "milp",
        fake_solver(status=1, success=False, x=None, message="time limit"),
    )
    inputs = make_inputs()
    with pytest.raises(OptimiserError):
        plan_household(inputs)
    res = plan_with_safety(inputs, now=NOW)
    assert res.decision is Decision.FALLBACK
    assert res.reasons == ["planner_error: OptimiserError"] and res.report.passed
