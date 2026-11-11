"""Fixed loads: never scheduled, they add to the household's base load."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wattmagent.appliances.agent import run_appliance_agent
from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.catalogue import ConfigError, default_catalogue, parse_fixed
from wattmagent.appliances.household import (
    base_load_series,
    build_day_inputs,
    load_household,
    parse_household,
)
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.simulate import plan_bill
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import radiation_by_hour, synthetic_weather, temperature_by_hour
from wattmagent.graph.context import AgentContext

ROOT = Path(__file__).resolve().parents[2]
DETAILED = ROOT / "config" / "households" / "example_detailed.toml"
HOURS = list(range(24))


def test_the_default_catalogue_has_every_class() -> None:
    cat = default_catalogue()
    assert {"washing_machine", "dishwasher", "tumble_dryer_heat_pump"} <= set(cat.cycles)
    assert {"water_heater_200l", "water_heater_80l"} <= set(cat.tanks)
    assert {"heat_pump_efficient_house", "heat_pump_older_house"} <= set(cat.heatpumps)
    assert {"home_battery_5kwh", "home_battery_10kwh"} <= set(cat.batteries)
    assert {"refrigerator", "freezer", "induction_cooking", "standby_network"} <= set(cat.fixed)


def test_catalogue_fixed_loads_are_sane() -> None:
    for model in default_catalogue().fixed.values():
        assert len(model.hourly_kw) == 24
        assert all(math.isfinite(v) and v >= 0 for v in model.hourly_kw)
        assert 0.3 < model.daily_kwh < 5.0, model.key  # household appliances, not a factory


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({}, "missing hourly_kw"),
        ({"hourly_kw": [0.1] * 23}, "expected 24 values"),
        ({"hourly_kw": [0.1] * 23 + [-0.1]}, ">= 0"),
        ({"hourly_kw": [0.1] * 23 + [float("nan")]}, "finite"),
        ({"hourly_kw": [0.1] * 23 + [float("inf")]}, "finite"),
        ({"hourly_kw": [0.1] * 23 + ["x"]}, "not a number"),
        ({"hourly_kw": [0.1] * 24, "colour": "red"}, "unknown key"),
    ],
)
def test_bad_fixed_load_definitions_are_refused(table: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_fixed("x", table)


def test_the_detailed_example_adds_up() -> None:
    household = load_household(DETAILED)
    cat = default_catalogue().fixed
    expected = sum(cat[e.model.key].daily_kwh * e.count for e in household.fixed)
    assert household.fixed_daily_kwh == pytest.approx(expected)
    assert sum(base_load_series(household, HOURS)) == pytest.approx(expected)


def test_count_and_overrides_work_and_are_additive_with_the_catch_all() -> None:
    plain = parse_household({"fixed": [{"appliance": "freezer"}]})
    two = parse_household({"fixed": [{"appliance": "freezer", "count": 2}]})
    assert sum(base_load_series(two, HOURS)) == pytest.approx(
        2 * sum(base_load_series(plain, HOURS))
    )
    mine = parse_household({"fixed": [{"appliance": "freezer", "hourly_kw": [0.1] * 24}]})
    assert sum(base_load_series(mine, HOURS)) == pytest.approx(2.4)
    both = parse_household({"daily_base_load_kwh": 9.0, "fixed": [{"appliance": "freezer"}]})
    assert sum(base_load_series(both, HOURS)) == pytest.approx(9.0 + 1.2)
    custom = parse_household(
        {
            "catalogue": {"fixed": {"sauna": {"hourly_kw": [0.0] * 20 + [6.0, 0.0, 0.0, 0.0]}}},
            "fixed": [{"appliance": "sauna", "name": "garden sauna"}],
        }
    )
    assert sum(base_load_series(custom, HOURS)) == pytest.approx(6.0)


def test_no_load_at_all_means_an_empty_series() -> None:
    assert base_load_series(parse_household({}), HOURS) == []


def test_profiles_follow_the_local_clock_hour_of_each_slot() -> None:
    household = parse_household({"fixed": [{"appliance": "tv_entertainment"}]})  # evenings 18-22
    series = base_load_series(household, [17, 18, 22, 23, 0, 18 + 24])
    assert series[0] == 0.0 and series[1] == 0.15 and series[2] == 0.15
    assert series[3] == 0.0 and series[4] == 0.0 and series[5] == 0.15


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"appliance": "nope"}, "unknown appliance"),
        ({}, "unknown appliance"),
        ({"appliance": "freezer", "count": 0}, "count"),
        ({"appliance": "freezer", "count": 21}, "count"),
        ({"appliance": "freezer", "count": True}, "count"),
        ({"appliance": "freezer", "count": 1.5}, "count"),
        ({"appliance": "freezer", "colour": "red"}, "unknown key"),
    ],
)
def test_bad_fixed_entries_are_refused(entry: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_household({"fixed": [entry]})


def test_structural_mistakes_are_refused() -> None:
    with pytest.raises(ConfigError, match="list"):
        parse_household({"fixed": {"appliance": "freezer"}})
    with pytest.raises(ConfigError, match="unique"):
        parse_household({"fixed": [{"appliance": "freezer"}, {"appliance": "freezer"}]})
    parse_household(  # a repeated appliance is fine once it has its own name
        {"fixed": [{"appliance": "freezer"}, {"appliance": "freezer", "name": "garage freezer"}]}
    )


def _inputs(with_fixed: bool):  # noqa: ANN202
    start = datetime(2025, 6, 10, tzinfo=UTC)
    prices = [p.eur_per_kwh for p in synthetic_prices(start, 1, 3)][:24]
    sun = [w.shortwave_w_m2 for w in synthetic_weather(start, 1, 3)[:24]]
    household = load_household(DETAILED)
    if not with_fixed:
        household = replace(household, fixed=())
    return build_day_inputs(household, prices, HOURS, None, sun)


def test_more_household_load_never_lowers_the_whole_bill() -> None:
    with_fixed, without = _inputs(True), _inputs(False)
    assert with_fixed.base_load_kw and not without.base_load_kw
    a, b = plan_appliances(with_fixed), plan_appliances(without)
    assert a.planned_cost_eur is not None and b.planned_cost_eur is not None
    assert a.planned_cost_eur >= b.planned_cost_eur - 1e-9
    assert a.planned_cost_eur == pytest.approx(plan_bill(with_fixed, a), abs=1e-6)


def test_the_connection_limit_counts_the_fixed_loads() -> None:
    from wattmagent.appliances.models import CycleJob, CycleSpec, DayInputs

    prices = [0.30] * 24
    prices[18] = 0.05  # the cheapest hour is also the cooking hour
    job = CycleJob(CycleSpec("wash", (1.0,)), 0, 24)

    def start_hour(base: list[float]) -> int:
        inputs = DayInputs(prices, jobs=[job], base_load_kw=base, max_grid_kw=2.5)
        plan = plan_appliances(inputs)
        return max(range(24), key=lambda t: plan.cycle_kw["wash"][t])

    quiet = [1.0] * 24
    cooking = [1.0] * 24
    cooking[18] = 2.0  # 2.0 + 1.0 > 2.5: the cycle cannot run then
    assert start_hour(quiet) == 18
    assert start_hour(cooking) != 18


def test_the_appliance_agent_uses_the_fixed_loads_too() -> None:
    household = load_household(DETAILED)
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2025, 1, 3, 15, 20, tzinfo=tz)
    prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=tz), 5, 1)
    ctx = AgentContext(prices, now.replace(hour=14), tz)
    resp = run_appliance_agent("run the dishwasher by 10pm", household, ctx, now)
    assert resp.status == "planned"


def test_backtest_with_listed_fixed_loads_is_all_valid() -> None:
    household = load_household(DETAILED)
    start = datetime(2025, 5, 1, tzinfo=UTC)
    prices = synthetic_prices(start, 6, 4)
    weather = synthetic_weather(start, 6, 4)
    results = run_appliance_backtest(
        prices,
        household,
        default_planners(),
        tz=UTC,
        outdoor=temperature_by_hour(weather),
        radiation=radiation_by_hour(weather),
    )
    assert all(r.days_valid == r.days for r in results)
    by = {r.name: r for r in results}
    assert by["milp_optimiser"].mean_cost_eur <= by["run_immediately"].mean_cost_eur + 1e-9
