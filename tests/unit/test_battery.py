"""The home battery: model, optimiser, verifier, baselines, configuration."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.baselines import run_immediately, self_consumption
from wattmagent.appliances.catalogue import ConfigError
from wattmagent.appliances.household import build_day_inputs, load_household, parse_household
from wattmagent.appliances.models import AppliancePlan, BatterySpec, CycleJob, CycleSpec, DayInputs
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.simulate import plan_bill, simulate_battery
from wattmagent.appliances.verifier import ApplianceRule, verify_appliance_plan
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import radiation_by_hour, synthetic_weather
from wattmagent.planning.household import PlanStatus
from wattmagent.safety.pipeline import Decision

ROOT = Path(__file__).resolve().parents[2]
BATTERY = BatterySpec("test", capacity_kwh=10.0, max_charge_kw=5.0, max_discharge_kw=5.0)
# cheap night, dear evening: a spread large enough to pay for the losses and the wear
SPREAD = [0.10] * 6 + [0.20] * 11 + [0.40] * 4 + [0.20] * 3


def day(
    prices: list[float] | None = None,
    battery: BatterySpec | None = BATTERY,
    base_kw: float = 0.5,
    **kw: object,
) -> DayInputs:
    p = prices or SPREAD
    return DayInputs(prices=p, base_load_kw=[base_kw] * len(p), battery=battery, **kw)  # type: ignore[arg-type]


def rules(inputs: DayInputs, plan: AppliancePlan) -> set[str]:
    return set(verify_appliance_plan(inputs, plan, compare_with_baseline=False).rules)


def idle(n: int) -> list[float]:
    return [0.0] * n


# --- the spec ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"capacity_kwh": 0.0},
        {"max_charge_kw": -1.0},
        {"min_soc": 1.0},
        {"min_soc": 0.6, "initial_soc": 0.5},
        {"initial_soc": 1.2},
        {"charge_efficiency": 0.0},
        {"discharge_efficiency": 1.1},
        {"wear_eur_per_kwh": -0.01},
        {"wear_eur_per_kwh": float("nan")},
    ],
)
def test_the_spec_rejects_impossible_values(change: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        replace(BATTERY, **change)


# --- the optimiser ----------------------------------------------------------------------------


def test_it_buys_cheap_and_covers_the_expensive_hours() -> None:
    inputs = day()
    plan = plan_appliances(inputs)
    report = verify_appliance_plan(inputs, plan)
    assert report.passed, report.violations
    assert sum(plan.battery_charge_kw[:6]) > 1.0  # charges in the cheap night
    assert sum(plan.battery_discharge_kw[17:21]) > 1.0  # discharges in the dear evening
    idle_cost = plan_bill(inputs, run_immediately(inputs))
    assert plan_bill(inputs, plan) < idle_cost - 0.1


def test_the_optimiser_cost_equals_the_independent_recomputation() -> None:
    for seed in range(8):
        rng = random.Random(seed)  # noqa: S311  (test data)
        prices = [round(rng.uniform(-0.05, 0.45), 4) for _ in range(24)]
        inputs = day(prices, base_kw=rng.uniform(0.2, 1.5))
        plan = plan_appliances(inputs)
        assert plan.planned_cost_eur is not None
        assert plan.planned_cost_eur == pytest.approx(plan_bill(inputs, plan), abs=1e-6)
        assert verify_appliance_plan(inputs, plan).passed


def test_a_high_wear_cost_keeps_the_battery_idle() -> None:
    inputs = day(battery=replace(BATTERY, wear_eur_per_kwh=1.0))
    plan = plan_appliances(inputs)
    assert sum(plan.battery_charge_kw) == 0 and sum(plan.battery_discharge_kw) == 0


def test_a_flat_price_gives_nothing_to_gain() -> None:
    inputs = day([0.25] * 24)
    plan = plan_appliances(inputs)
    assert plan_bill(inputs, plan) == pytest.approx(plan_bill(inputs, run_immediately(inputs)))


def test_stored_energy_stays_in_range_and_ends_no_lower() -> None:
    inputs = day()
    plan = plan_appliances(inputs)
    stored = simulate_battery(inputs, plan.battery_charge_kw, plan.battery_discharge_kw)
    assert all(BATTERY.soc_min_kwh - 1e-6 <= v <= BATTERY.capacity_kwh + 1e-6 for v in stored)
    assert stored[-1] >= BATTERY.soc_initial_kwh - 1e-6


def test_it_never_charges_and_discharges_in_the_same_hour_even_at_negative_prices() -> None:
    prices = [-0.20] * 6 + [0.05] * 10 + [0.30] * 8
    inputs = day(prices)
    plan = plan_appliances(inputs)
    assert all(
        min(c, d) < 1e-6
        for c, d in zip(plan.battery_charge_kw, plan.battery_discharge_kw, strict=True)
    )
    assert verify_appliance_plan(inputs, plan).passed


def test_it_does_not_burn_energy_through_the_round_trip_loss_when_paid_to_import() -> None:
    # With free wear and a very negative price, charging and discharging together would "earn"
    # money by turning grid energy into losses. A real inverter cannot do that, so neither may
    # the plan.
    inputs = day([-0.50] * 24, battery=replace(BATTERY, wear_eur_per_kwh=0.0), base_kw=1.0)
    plan = plan_appliances(inputs)
    assert all(
        min(c, d) < 1e-6
        for c, d in zip(plan.battery_charge_kw, plan.battery_discharge_kw, strict=True)
    )
    assert verify_appliance_plan(inputs, plan).passed


def test_it_never_feeds_the_grid_from_the_battery() -> None:
    # a price spike with almost no household load: exporting would pay, but is not allowed
    prices = [0.05] * 10 + [1.5] * 4 + [0.05] * 10
    inputs = day(prices, base_kw=0.2)
    plan = plan_appliances(inputs)
    for t in range(24):
        assert plan.battery_discharge_kw[t] <= 0.2 + plan.battery_charge_kw[t] + 1e-6
    assert verify_appliance_plan(inputs, plan).passed


def test_charging_counts_against_the_grid_limit() -> None:
    inputs = day(max_grid_kw=2.0)
    plan = plan_appliances(inputs)
    for t in range(24):
        assert 0.5 + plan.battery_charge_kw[t] - plan.battery_discharge_kw[t] <= 2.0 + 1e-6
    assert verify_appliance_plan(inputs, plan).passed


def test_a_battery_never_costs_more_than_no_battery_on_the_same_bill() -> None:
    start = datetime(2025, 6, 10, tzinfo=UTC)
    prices = [p.eur_per_kwh for p in synthetic_prices(start, 1, 5)][:24]
    sun = [w.shortwave_w_m2 * 0.006 * 0.8 for w in synthetic_weather(start, 1, 5)[:24]]
    with_battery = day(prices, pv_kw=sun, export_price_eur_kwh=0.08)
    without = replace(with_battery, battery=None)
    a, b = plan_appliances(with_battery), plan_appliances(without)
    assert a.planned_cost_eur is not None and b.planned_cost_eur is not None
    assert a.planned_cost_eur <= b.planned_cost_eur + 1e-6
    assert sum(sun) > 5  # the day really has sun


def test_solar_surplus_is_stored_for_the_evening() -> None:
    sun = [0.0] * 8 + [1.0, 3.0, 5.0, 5.5, 5.5, 5.0, 3.0, 1.0] + [0.0] * 8
    inputs = day([0.30] * 24, base_kw=0.4, pv_kw=sun, export_price_eur_kwh=0.05)
    plan = plan_appliances(inputs)
    assert sum(plan.battery_charge_kw[8:16]) > 5.0  # stores midday surplus
    assert sum(plan.battery_discharge_kw[16:]) > 3.0  # and uses it in the evening
    assert verify_appliance_plan(inputs, plan).passed


def test_cycles_and_a_battery_plan_together() -> None:
    job = CycleJob(CycleSpec("wash", (1.0, 1.0)), 0, 24)
    inputs = day(jobs=[job])
    plan = plan_appliances(inputs)
    assert verify_appliance_plan(inputs, plan).passed
    assert sum(plan.cycle_kw["wash"]) == pytest.approx(2.0)


# --- the verifier is not fooled -----------------------------------------------------------------


def test_a_plan_without_battery_dispatch_is_the_idle_battery() -> None:
    inputs = day()
    assert not rules(inputs, run_immediately(inputs))


@pytest.mark.parametrize(
    ("charge", "discharge", "expected"),
    [
        ({3: 6.0}, {}, ApplianceRule.BATTERY_POWER),  # above the charge limit
        ({}, {20: 6.0}, ApplianceRule.BATTERY_POWER),  # above the discharge limit
        ({5: 3.0}, {5: 2.0}, ApplianceRule.BATTERY_POWER),  # both in one hour
        ({1: 5.0, 2: 5.0, 3: 5.0}, {}, ApplianceRule.BATTERY_SOC),  # overfull
        ({}, {1: 4.0, 2: 4.0}, ApplianceRule.BATTERY_SOC),  # below the floor
        ({}, {20: 1.0}, ApplianceRule.BATTERY_END),  # emptier than it began
        ({}, {20: 3.0}, ApplianceRule.BATTERY_END),
    ],
)
def test_broken_battery_plans_are_caught(
    charge: dict[int, float], discharge: dict[int, float], expected: ApplianceRule
) -> None:
    inputs = day(base_kw=3.5)
    bad = replace(
        run_immediately(inputs),
        battery_charge_kw=[charge.get(t, 0.0) for t in range(24)],
        battery_discharge_kw=[discharge.get(t, 0.0) for t in range(24)],
    )
    assert expected.value in rules(inputs, bad)


def test_exporting_battery_energy_is_caught() -> None:
    inputs = day(base_kw=0.2)
    bad = replace(
        run_immediately(inputs),
        battery_charge_kw=[3.0 if t == 2 else 0.0 for t in range(24)],
        battery_discharge_kw=[3.0 if t == 20 else 0.0 for t in range(24)],  # house uses 0.2
    )
    assert ApplianceRule.BATTERY_EXPORT.value in rules(inputs, bad)


def test_wrong_shapes_and_nonsense_are_caught() -> None:
    inputs = day()
    base = run_immediately(inputs)
    assert ApplianceRule.SHAPE.value in rules(inputs, replace(base, battery_charge_kw=[0.0]))
    assert ApplianceRule.NOT_FINITE.value in rules(
        inputs, replace(base, battery_charge_kw=[float("nan")] * 24)
    )
    no_battery = day(battery=None)
    ghost = replace(run_immediately(no_battery), battery_charge_kw=[1.0] * 24)
    assert ApplianceRule.SHAPE.value in rules(no_battery, ghost)


def test_the_cost_check_catches_a_battery_that_makes_the_bill_worse() -> None:
    inputs = day()
    # charge in the dear hours, discharge in the cheap ones: legal physics, bad money
    bad = replace(
        run_immediately(inputs),
        battery_charge_kw=[3.0 if t == 18 else 0.0 for t in range(24)],
        battery_discharge_kw=[3.0 if t == 2 else 0.0 for t in range(24)],
    )
    report = verify_appliance_plan(inputs, bad)
    assert (
        ApplianceRule.COST.value in report.rules or ApplianceRule.BATTERY_SOC.value in report.rules
    )


# --- the safety pipeline ------------------------------------------------------------------------


def test_the_pipeline_falls_back_to_an_idle_battery_when_the_planner_is_wrong() -> None:
    inputs = day()

    def greedy(i: DayInputs) -> AppliancePlan:
        n = i.slots
        return AppliancePlan(
            PlanStatus.OPTIMAL,
            battery_charge_kw=[5.0] * n,  # fills the battery and keeps charging: impossible
            battery_discharge_kw=idle(n),
            planned_cost_eur=0.0,
        )

    result = plan_appliances_safely(inputs, planner=greedy)
    assert result.decision is Decision.FALLBACK
    assert sum(result.plan.battery_charge_kw) == 0
    assert any("verifier_rejected" in r for r in result.reasons)


def test_the_audit_summary_mentions_the_battery_without_naming_it() -> None:
    from wattmagent.safety.audit import InMemoryAuditLog

    audit = InMemoryAuditLog()
    plan_appliances_safely(day(), audit=audit)
    summary = audit.entries[0]["inputs"]
    assert summary["battery"] is True
    assert "test" not in str(summary)


# --- the self-consumption baseline ---------------------------------------------------------------


def test_self_consumption_stores_surplus_and_covers_the_evening() -> None:
    sun = [0.0] * 8 + [1.0, 3.0, 5.0, 5.5, 5.5, 5.0, 3.0, 1.0] + [0.0] * 8
    inputs = day([0.30] * 24, base_kw=0.4, pv_kw=sun, export_price_eur_kwh=0.05)
    plan = self_consumption(inputs)
    assert verify_appliance_plan(inputs, plan, compare_with_baseline=False).passed
    assert sum(plan.battery_charge_kw) > 5.0 and sum(plan.battery_discharge_kw) > 3.0
    assert plan_bill(inputs, plan) < plan_bill(inputs, run_immediately(inputs))


def test_self_consumption_does_nothing_without_solar() -> None:
    inputs = day()
    plan = self_consumption(inputs)
    assert sum(plan.battery_charge_kw) == 0 and sum(plan.battery_discharge_kw) == 0


def test_self_consumption_without_a_battery_is_run_immediately() -> None:
    inputs = day(battery=None)
    assert self_consumption(inputs) == run_immediately(inputs)


# --- configuration -------------------------------------------------------------------------------


def test_the_example_household_loads_and_plans() -> None:
    household = load_household(ROOT / "config" / "households" / "example_battery.toml")
    assert household.battery is not None and household.battery.capacity_kwh == 10.0
    start = datetime(2025, 6, 10, tzinfo=UTC)
    prices = [p.eur_per_kwh for p in synthetic_prices(start, 1, 2)][:24]
    sun = [w.shortwave_w_m2 for w in synthetic_weather(start, 1, 2)[:24]]
    inputs = build_day_inputs(household, prices, list(range(24)), None, sun)
    assert inputs.battery is not None and inputs.battery.name == "home_battery_10kwh"
    assert verify_appliance_plan(inputs, plan_appliances(inputs)).passed


def test_a_battery_entry_can_be_overridden_or_added_in_the_household_file() -> None:
    household = parse_household(
        {"battery": {"appliance": "home_battery_5kwh", "wear_eur_per_kwh": 0.02}}
    )
    assert household.battery is not None and household.battery.wear_eur_per_kwh == 0.02
    custom = parse_household(
        {
            "catalogue": {
                "battery": {"mine": {"capacity_kwh": 7, "max_charge_kw": 3, "max_discharge_kw": 3}}
            },
            "battery": {"appliance": "mine"},
        }
    )
    assert custom.battery is not None and custom.battery.capacity_kwh == 7.0


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"battery": {"appliance": "nope"}}, "unknown appliance"),
        ({"battery": {}}, "unknown appliance"),
        ({"battery": {"appliance": "home_battery_5kwh", "colour": "red"}}, "unknown key"),
        ({"battery": {"appliance": "home_battery_5kwh", "min_soc": 0.9}}, "min_soc"),
        ({"battery": {"appliance": "home_battery_5kwh", "capacity_kwh": "big"}}, "not a number"),
        ({"battery": "yes"}, "expected a table"),
    ],
)
def test_bad_battery_configuration_is_refused_with_a_clear_message(
    raw: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_household(raw)


# --- a whole-year style replay -------------------------------------------------------------------


def test_backtest_shows_the_three_battery_strategies_all_valid() -> None:
    household = load_household(ROOT / "config" / "households" / "example_battery.toml")
    start = datetime(2025, 5, 1, tzinfo=UTC)
    days = 6
    prices = synthetic_prices(start, days, 4)
    weather = synthetic_weather(start, days, 4)
    results = run_appliance_backtest(
        prices,
        household,
        default_planners(with_battery=True),
        tz=UTC,
        outdoor={w.start: w.temperature_c for w in weather},
        radiation=radiation_by_hour(weather),
    )
    by_name = {r.name: r for r in results}
    assert list(by_name) == [
        "run_immediately",
        "cheapest_start",
        "self_consumption",
        "milp_optimiser",
    ]
    assert all(r.days_valid == r.days for r in results)
    assert (
        by_name["self_consumption"].mean_cost_eur <= by_name["run_immediately"].mean_cost_eur + 1e-9
    )
    assert (
        by_name["milp_optimiser"].mean_cost_eur <= by_name["self_consumption"].mean_cost_eur + 1e-9
    )
