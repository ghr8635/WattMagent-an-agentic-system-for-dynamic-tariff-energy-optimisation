from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from wattmagent.backtest.household import Scenario, simulate_day
from wattmagent.data.load import synthetic_load_kw
from wattmagent.data.prices import PricePoint
from wattmagent.devices.battery import BatterySpec
from wattmagent.devices.ev import EVSpec
from wattmagent.optimiser.household import plan_household
from wattmagent.planning.household import (
    HouseholdInputs,
    PlanStatus,
    ev_cheapest_hours,
    ev_immediately,
)

EV = EVSpec()
DAY0 = datetime(2025, 1, 1, 18, tzinfo=UTC)


def day_inputs(
    prices: list[float], battery: BatterySpec | None, ev: EVSpec = EV
) -> HouseholdInputs:
    """A 24 h day starting at 18:00: EV plugged in for the first 13 hours."""
    n = len(prices)
    hours = [(18 + i) % 24 for i in range(n)]
    return HouseholdInputs(
        prices=prices,
        load_kw=synthetic_load_kw(hours),
        ev_available=[i < ev.window_hours for i in range(n)],
        ev=ev,
        battery=battery,
    )


def random_prices(seed: int, n: int = 24, low: float = 0.0, high: float = 0.4) -> list[float]:
    rng = random.Random(seed)  # noqa: S311  (test data, not crypto)
    return [rng.uniform(low, high) for _ in range(n)]


def simulate(inputs: HouseholdInputs, plan, actual: list[float]):
    window = [PricePoint(DAY0 + timedelta(hours=i), p) for i, p in enumerate(actual)]
    return simulate_day(window, Scenario(inputs.ev, inputs.battery), plan, inputs.load_kw)


def test_ev_only_optimiser_matches_the_cheapest_hours_bound() -> None:
    for seed in range(20):
        inputs = day_inputs(random_prices(seed), battery=None)
        milp = simulate(inputs, plan_household(inputs), list(inputs.prices))
        rule = simulate(inputs, ev_cheapest_hours(inputs), list(inputs.prices))
        assert milp.target_met and rule.target_met
        assert milp.bill_eur == pytest.approx(rule.bill_eur, abs=1e-5)


def test_plan_agrees_with_the_simulator_including_negative_prices() -> None:
    """The optimiser's own cost must equal the physics simulator's bill + wear."""
    bat = BatterySpec(wear_eur_per_kwh=0.02)
    for seed in range(25):
        prices = random_prices(seed, low=-0.15, high=0.35)  # includes negative prices
        inputs = day_inputs(prices, bat)
        plan = plan_household(inputs)
        assert plan.status is PlanStatus.OPTIMAL and plan.planned_cost_eur is not None
        day = simulate(inputs, plan, prices)
        assert day.bill_eur + day.wear_eur == pytest.approx(plan.planned_cost_eur, abs=1e-5)
        assert day.target_met
        assert day.battery_end_kwh >= bat.initial_kwh - 1e-5  # no free energy from the end


def test_optimiser_is_never_worse_than_the_rules() -> None:
    bat = BatterySpec()
    for seed in range(20):
        prices = random_prices(seed + 100)
        inputs = day_inputs(prices, bat)
        opt = simulate(inputs, plan_household(inputs), prices)
        for rule in (ev_immediately, ev_cheapest_hours):
            ref = simulate(inputs, rule(inputs), prices)
            assert opt.bill_eur + opt.wear_eur <= ref.bill_eur + 1e-6


def test_battery_earns_money_on_a_wide_price_spread() -> None:
    prices = [0.30] * 24
    for i in range(6, 12):  # cheap morning block
        prices[i] = 0.05
    inputs = day_inputs(prices, BatterySpec(wear_eur_per_kwh=0.0))
    with_bat = plan_household(inputs)
    without = plan_household(replace(inputs, battery=None))
    assert with_bat.planned_cost_eur is not None and without.planned_cost_eur is not None
    assert with_bat.planned_cost_eur < without.planned_cost_eur - 0.1
    assert any(kw > 0 for kw in with_bat.battery_kw) and any(kw < 0 for kw in with_bat.battery_kw)


def test_high_wear_cost_keeps_the_battery_idle() -> None:
    inputs = day_inputs(random_prices(7), BatterySpec(wear_eur_per_kwh=5.0))
    plan = plan_household(inputs)
    assert all(abs(kw) < 1e-6 for kw in plan.battery_kw)


def test_scaling_all_prices_scales_the_cost() -> None:
    bat = BatterySpec(wear_eur_per_kwh=0.0)
    base = random_prices(3)
    a = plan_household(day_inputs(base, bat)).planned_cost_eur
    b = plan_household(day_inputs([2 * p for p in base], bat)).planned_cost_eur
    assert a is not None and b is not None
    assert b == pytest.approx(2 * a, rel=1e-6)


def test_ev_is_charged_only_while_plugged_in_and_within_charger_limit() -> None:
    for seed in range(15):
        inputs = day_inputs(random_prices(seed + 50), BatterySpec())
        plan = plan_household(inputs)
        for kw, ok in zip(plan.ev_kw, inputs.ev_available, strict=True):
            assert -1e-9 <= kw <= EV.max_charge_kw + 1e-9
            if not ok:
                assert kw == 0.0
        stored = sum(plan.ev_kw) * EV.efficiency
        assert stored == pytest.approx((EV.target_soc - EV.arrival_soc) * EV.capacity_kwh, abs=1e-5)


def test_unreachable_target_is_reported_not_approximated() -> None:
    ev = EVSpec(max_charge_kw=3.0, arrival_soc=0.0, target_soc=1.0)  # needs 60 kWh, can store ~36
    plan = plan_household(day_inputs([0.2] * 24, BatterySpec(), ev))
    assert plan.status is PlanStatus.INFEASIBLE and not plan.feasible
    assert plan.ev_kw == [] and plan.battery_kw == []
    assert "at most" in plan.message and "%" in plan.message


def test_nothing_to_charge_means_no_ev_power() -> None:
    ev = EVSpec(arrival_soc=0.8, target_soc=0.8)
    plan = plan_household(day_inputs(random_prices(1), None, ev))
    assert plan.status is PlanStatus.OPTIMAL and all(kw == 0.0 for kw in plan.ev_kw)


def test_short_and_long_daylight_saving_days_are_valid() -> None:
    for n in (23, 25):
        prices = random_prices(n, n=n)
        inputs = day_inputs(prices, BatterySpec())
        plan = plan_household(inputs)
        assert plan.status is PlanStatus.OPTIMAL and len(plan.ev_kw) == n
        assert simulate(inputs, plan, prices).target_met


def test_inputs_with_mismatched_lengths_are_rejected() -> None:
    with pytest.raises(ValueError):
        HouseholdInputs([0.1, 0.2], [1.0], [True, True], EV)
    with pytest.raises(ValueError):
        HouseholdInputs([], [], [], EV)
