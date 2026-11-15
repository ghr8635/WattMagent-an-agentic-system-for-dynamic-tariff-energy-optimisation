"""Replanning part-way through the day: history is fixed, the future is planned afresh."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from wattmagent.appliances import replan as replan_module
from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.household import build_day_inputs, load_household, parse_household
from wattmagent.appliances.models import Committed, CycleJob, CycleSpec, DayInputs
from wattmagent.appliances.optimiser import (
    ApplianceOptimiserError,
    cycle_start,
    plan_appliances,
)
from wattmagent.appliances.replan import replan_day
from wattmagent.appliances.simulate import plan_bill
from wattmagent.appliances.verifier import verify_appliance_plan
from wattmagent.data.forecast import persistence, scale_to_observations
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import radiation_by_hour, synthetic_weather, temperature_by_hour
from wattmagent.planning.household import PlanStatus

ROOT = Path(__file__).resolve().parents[2]
HOUSE = load_household(ROOT / "config" / "households" / "example_battery.toml")
CEILING = HOUSE.solar.kwp * 1.1 * HOUSE.solar.performance_ratio if HOUSE.solar else None
START = datetime(2025, 6, 10, tzinfo=UTC)


def world(day: int = 1, seed: int = 1) -> tuple[DayInputs, list[float]]:
    """A summer day for the battery house: the day as it turns out, and yesterday's forecast."""
    prices = [p.eur_per_kwh for p in synthetic_prices(START, 6, seed)]
    rad = radiation_by_hour(synthetic_weather(START - timedelta(days=4), 12, seed))
    forecast = persistence(rad)
    slots = [START + timedelta(days=day, hours=i) for i in range(24)]
    window = prices[day * 24 : day * 24 + 24]
    actual = build_day_inputs(HOUSE, window, list(range(24)), None, [rad[t] for t in slots])
    seen = build_day_inputs(HOUSE, window, list(range(24)), None, [forecast[t] for t in slots])
    return actual, seen.pv_kw


# --- the nowcast ----------------------------------------------------------------------------------


def test_the_rest_of_the_day_is_scaled_by_how_the_day_is_going() -> None:
    forecast = [0.0, 0.0, 100.0, 200.0, 200.0, 100.0, 0.0]
    observed = [0.0, 0.0, 50.0, 100.0]  # half the forecast so far
    out = scale_to_observations(forecast, observed)
    assert out[:4] == observed  # the elapsed part is what really happened
    assert out[4:] == pytest.approx([100.0, 50.0, 0.0])


def test_no_information_at_dawn_or_at_the_start_means_no_change() -> None:
    forecast = [0.0] * 6 + [10.0, 300.0, 600.0, 600.0, 300.0, 10.0] + [0.0] * 12
    early = [0.0] * 6 + [0.0]  # sun not up yet (and 'observed' zero must not zero the day)
    assert scale_to_observations(forecast, early)[7:] == forecast[7:]
    assert scale_to_observations(forecast, [])[:] == forecast
    assert scale_to_observations([0.0] * 5, [0.0, 0.0]) == [0.0] * 5


def test_the_ratio_and_the_values_are_limited() -> None:
    forecast = [0.0, 10.0, 10.0, 10.0]
    assert scale_to_observations(forecast, [0.0, 1000.0], max_ratio=2.0)[2:] == [20.0, 20.0]
    assert scale_to_observations(forecast, [0.0, 1000.0], ceiling=25.0)[2:] == [25.0, 25.0]
    assert scale_to_observations(forecast, [0.0, 0.0])[2:] == [0.0, 0.0]  # dark day


@pytest.mark.parametrize(
    "args",
    [
        ([1.0], [1.0, 2.0]),  # more observed than forecast
        ([1.0, float("nan")], [1.0]),
        ([1.0, 2.0], [-1.0]),
    ],
)
def test_bad_series_are_refused(args: tuple[list[float], list[float]]) -> None:
    with pytest.raises(ValueError):
        scale_to_observations(*args)


# --- committing the past --------------------------------------------------------------------------


def test_cycle_start_is_read_back_from_the_power_series() -> None:
    job = CycleJob(CycleSpec("wash", (1.0, 0.5)), 0, 24)
    plan = plan_appliances(DayInputs([0.3] * 24, jobs=[job]))
    start = cycle_start(job, plan.cycle_kw["wash"])
    assert start is not None and plan.cycle_kw["wash"][start] == 1.0
    assert cycle_start(job, [0.7] * 24) is None


def test_with_nothing_committed_the_plan_is_unchanged() -> None:
    actual, _ = world()
    plan = plan_appliances(actual)
    again = plan_appliances(actual, committed=Committed(0, plan))
    assert again.planned_cost_eur == pytest.approx(plan.planned_cost_eur, abs=1e-6)


@pytest.mark.parametrize("until", [1, 6, 12, 18, 23, 24])
def test_replanning_on_the_same_information_keeps_the_history_and_the_cost(until: int) -> None:
    actual, _ = world()
    plan = plan_appliances(actual)
    again = plan_appliances(actual, committed=Committed(until, plan))
    assert again.status is PlanStatus.OPTIMAL
    assert again.planned_cost_eur == pytest.approx(plan.planned_cost_eur, abs=1e-5)
    for series_a, series_b in (
        (plan.tank_kw, again.tank_kw),
        (plan.heatpump_kw, again.heatpump_kw),
        (plan.battery_charge_kw, again.battery_charge_kw),
        (plan.battery_discharge_kw, again.battery_discharge_kw),
    ):
        assert series_b[:until] == pytest.approx(series_a[:until], abs=1e-6)
    for name, series in plan.cycle_kw.items():
        assert again.cycle_kw[name][:until] == pytest.approx(series[:until], abs=1e-9)
    assert verify_appliance_plan(actual, again, compare_with_baseline=False).passed


def test_the_history_is_kept_even_when_it_was_not_the_best_choice() -> None:
    """Replanning with different prices than the history was planned on must not touch the past."""
    actual, _ = world()
    history = plan_appliances(actual)
    flipped = replace(actual, prices=list(reversed(actual.prices)))
    free = plan_appliances(flipped)
    kept = plan_appliances(flipped, committed=Committed(12, history))
    assert kept.status is PlanStatus.OPTIMAL
    pairs = [
        (history.tank_kw, kept.tank_kw, free.tank_kw),
        (history.battery_charge_kw, kept.battery_charge_kw, free.battery_charge_kw),
        (history.battery_discharge_kw, kept.battery_discharge_kw, free.battery_discharge_kw),
    ]
    differs = False
    for old, now, unconstrained in pairs:
        assert now[:12] == pytest.approx(old[:12], abs=1e-6)  # the past is the past
        differs = differs or old[:12] != pytest.approx(unconstrained[:12], abs=1e-6)
    assert differs  # the test is only meaningful if the free plan would have changed history
    assert kept.planned_cost_eur is not None and free.planned_cost_eur is not None
    assert kept.planned_cost_eur >= free.planned_cost_eur - 1e-6  # a fixed past never helps


def test_the_hot_water_history_is_kept_even_when_it_was_not_the_best_choice() -> None:
    house = parse_household({"tank": {"appliance": "water_heater_200l"}})
    cheap_early = [0.50] * 24
    cheap_early[8] = 0.02  # the morning reheat is cheapest at 08:00 ...
    cheap_late = [0.50] * 24
    cheap_late[8] = 0.90  # ... but the prices that really applied make 10:00 the cheap hour
    cheap_late[10] = 0.02
    early = build_day_inputs(house, cheap_early, list(range(24)))
    late = replace(early, prices=cheap_late)
    history = plan_appliances(early)
    assert history.tank_kw[8] > 0  # it reheated at 08:00, while that was cheapest
    free = plan_appliances(late)
    assert free.tank_kw[:12] != pytest.approx(history.tank_kw[:12], abs=1e-6)  # meaningful
    kept = plan_appliances(late, committed=Committed(12, history))
    assert kept.status is PlanStatus.OPTIMAL
    assert kept.tank_kw[:12] == pytest.approx(history.tank_kw[:12], abs=1e-6)
    assert verify_appliance_plan(late, kept, compare_with_baseline=False).passed


def test_a_cycle_that_has_not_started_cannot_start_in_the_past() -> None:
    prices = [0.50] * 6 + [0.05] * 6 + [0.50] * 12  # cheap hours 6-11
    job = CycleJob(CycleSpec("wash", (1.0,)), 0, 24)
    inputs = DayInputs(prices, jobs=[job])
    committed_plan = plan_appliances(
        replace(inputs, prices=[0.50] * 24)
    )  # starts at the cheapest: 0
    # nothing had started by hour 3 in a plan that starts the wash at 22:
    late = replace(committed_plan, cycle_kw={"wash": [1.0 if t == 22 else 0.0 for t in range(24)]})
    again = plan_appliances(inputs, committed=Committed(3, late))
    start = cycle_start(job, again.cycle_kw["wash"])
    assert start is not None and start >= 3  # it may not move into the elapsed hours
    assert start in range(6, 12)  # and takes the cheap hours that are still ahead


def test_a_cycle_that_already_started_stays_where_it_started() -> None:
    prices = [0.50] * 6 + [0.05] * 6 + [0.50] * 12
    job = CycleJob(CycleSpec("wash", (1.0,)), 0, 24)
    inputs = DayInputs(prices, jobs=[job])
    early = plan_appliances(replace(inputs, prices=[0.10] + [0.50] * 23))  # started at hour 0
    again = plan_appliances(inputs, committed=Committed(5, early))
    assert cycle_start(job, again.cycle_kw["wash"]) == 0  # history cannot be rewritten


def test_a_history_that_cannot_be_completed_is_reported_as_infeasible() -> None:
    job = CycleJob(CycleSpec("heavy", (3.0,)), 0, 24)
    loose = DayInputs([0.2] * 24, jobs=[job])
    plan = plan_appliances(replace(loose, prices=[0.9] * 3 + [0.1] * 21))  # started at hour 3
    tight = replace(loose, max_grid_kw=2.0)  # but 3 kW is over the limit that really applies
    assert plan_appliances(tight, committed=Committed(5, plan)).status is PlanStatus.INFEASIBLE


@pytest.mark.parametrize("until", [-1, 25])
def test_a_commitment_outside_the_day_is_refused(until: int) -> None:
    actual, _ = world()
    with pytest.raises(ValueError):
        plan_appliances(actual, committed=Committed(until, plan_appliances(actual)))


# --- replanning a whole day -----------------------------------------------------------------------


def test_with_a_perfect_forecast_there_is_nothing_to_replan() -> None:
    actual, _ = world()
    result = replan_day(actual, actual.pv_kw, pv_ceiling_kw=CEILING)
    assert (result.solves, result.failed) == (1, 0)
    assert plan_bill(actual, result.plan) == pytest.approx(
        plan_bill(actual, plan_appliances(actual)), abs=1e-6
    )


@pytest.mark.parametrize("day", [1, 2, 3, 4])
def test_replanning_without_a_better_forecast_changes_nothing(day: int) -> None:
    actual, forecast = world(day)
    once = plan_appliances(replace(actual, pv_kw=forecast))
    result = replan_day(actual, forecast, nowcast=False)
    assert plan_bill(actual, result.plan) == pytest.approx(plan_bill(actual, once), abs=1e-5)


@pytest.mark.parametrize("day", [1, 2, 3, 4])
def test_no_plan_beats_perfect_foresight_on_the_real_day(day: int) -> None:
    """Whatever was planned, the day actually lived cannot cost less than the true optimum."""
    actual, forecast = world(day)
    perfect = plan_bill(actual, plan_appliances(actual))
    once = plan_bill(actual, plan_appliances(replace(actual, pv_kw=forecast)))
    replanned = plan_bill(actual, replan_day(actual, forecast, pv_ceiling_kw=CEILING).plan)
    assert once >= perfect - 1e-6 and replanned >= perfect - 1e-6


def test_a_forecast_that_is_uniformly_too_low_is_corrected_by_the_nowcast() -> None:
    actual, _ = world()
    low = [0.5 * v for v in actual.pv_kw]  # promises half the sun that comes
    once = plan_bill(actual, plan_appliances(replace(actual, pv_kw=low)))
    result = replan_day(actual, low, pv_ceiling_kw=CEILING)
    perfect = plan_bill(actual, plan_appliances(actual))
    assert plan_bill(actual, result.plan) < once  # the nowcast helps here
    assert result.solves <= 3  # once the day is understood the plan stands: no re-solving
    assert plan_bill(actual, result.plan) >= perfect - 1e-6
    assert verify_appliance_plan(actual, result.plan, compare_with_baseline=False).passed


def test_a_failed_replan_leaves_the_previous_plan_in_force(monkeypatch: pytest.MonkeyPatch) -> None:
    actual, forecast = world()
    calls = {"n": 0}
    real = plan_appliances

    def flaky(inputs: DayInputs, committed: Committed | None = None):  # noqa: ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            return real(inputs, committed)
        if calls["n"] % 2 == 0:
            raise ApplianceOptimiserError("injected")
        return replace(real(inputs, committed), status=PlanStatus.INFEASIBLE)

    monkeypatch.setattr(replan_module, "plan_appliances", flaky)
    result = replan_day(actual, [0.5 * v for v in forecast], pv_ceiling_kw=CEILING)
    assert result.failed == result.solves - 1 > 0  # every replan failed
    first = real(replace(actual, pv_kw=[0.5 * v for v in forecast]))
    assert result.plan == first  # and the first plan is still the one in force


def test_a_forecast_of_the_wrong_length_is_refused() -> None:
    actual, forecast = world()
    with pytest.raises(ValueError, match="one value per slot"):
        replan_day(actual, forecast[:-1])


def test_an_infeasible_first_plan_is_returned_as_it_is() -> None:
    job = CycleJob(CycleSpec("heavy", (3.0,)), 0, 24)
    inputs = DayInputs([0.2] * 24, jobs=[job], max_grid_kw=1.0, pv_kw=[0.0] * 24)
    result = replan_day(inputs, [0.0] * 24)
    assert result.plan.status is PlanStatus.INFEASIBLE and (result.solves, result.failed) == (1, 0)


# --- in the backtest ---------------------------------------------------------------------


def backtest_data(days: int = 5):  # noqa: ANN202
    prices = synthetic_prices(START, days, 2)
    weather = synthetic_weather(START - timedelta(days=4), days + 8, 2)
    rad = radiation_by_hour(weather)
    return prices, rad, persistence(rad), temperature_by_hour(weather)


def test_replanning_in_the_backtest_needs_a_forecast() -> None:
    prices, rad, _, outdoor = backtest_data()
    with pytest.raises(ValueError, match="needs a radiation forecast"):
        run_appliance_backtest(
            prices, HOUSE, default_planners(with_battery=True), tz=UTC, radiation=rad,
            outdoor=outdoor, replan_hourly=True,
        )  # fmt: skip


def test_backtest_replanning_counts_solves_and_leaves_the_rules_alone() -> None:
    prices, rad, forecast, outdoor = backtest_data()
    kwargs = {"tz": UTC, "radiation": rad, "radiation_forecast": forecast, "outdoor": outdoor}
    planners = default_planners(with_battery=True)
    plain = {r.name: r for r in run_appliance_backtest(prices, HOUSE, planners, **kwargs)}
    replanned = {
        r.name: r
        for r in run_appliance_backtest(prices, HOUSE, planners, replan_hourly=True, **kwargs)
    }
    for name in ("run_immediately", "cheapest_start", "self_consumption"):
        assert replanned[name] == plain[name]  # rules are not replanned
        assert replanned[name].replan_solves == 0
    opt = replanned["milp_optimiser"]
    assert opt.replan_solves > opt.days and opt.replan_failed == 0
    assert opt.days_valid == opt.days and opt.days_fallback == 0


def test_backtest_replanning_without_new_information_costs_the_same() -> None:
    prices, rad, forecast, outdoor = backtest_data()
    kwargs = {"tz": UTC, "radiation": rad, "radiation_forecast": forecast, "outdoor": outdoor}
    planners = default_planners(with_battery=True)
    plain = {r.name: r for r in run_appliance_backtest(prices, HOUSE, planners, **kwargs)}
    still = {
        r.name: r
        for r in run_appliance_backtest(
            prices, HOUSE, planners, replan_hourly=True, nowcast=False, **kwargs
        )
    }
    assert math.isclose(
        still["milp_optimiser"].mean_cost_eur, plain["milp_optimiser"].mean_cost_eur, abs_tol=1e-5
    )
