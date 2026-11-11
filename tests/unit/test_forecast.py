"""Forecasts from past data, and replaying a day on a forecast instead of the truth."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.household import load_household, parse_household
from wattmagent.data.forecast import (
    forecast_skill,
    lagged_mean_forecast,
    mean_of_last_days,
    persistence,
)
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import radiation_by_hour, synthetic_weather, temperature_by_hour

T0 = datetime(2025, 3, 1, tzinfo=UTC)


def series(values: list[float]) -> dict[datetime, float]:
    return {T0 + timedelta(hours=i): v for i, v in enumerate(values)}


# --- the forecasts ----------------------------------------------------------------------------


def test_persistence_is_yesterdays_value_and_the_first_day_has_no_forecast() -> None:
    actual = series([float(i) for i in range(72)])
    f = persistence(actual)
    assert set(f) == {T0 + timedelta(hours=i) for i in range(24, 72)}
    assert f[T0 + timedelta(hours=30)] == 6.0


def test_mean_of_last_days_is_the_average_of_the_same_hour() -> None:
    actual = series([float(i) for i in range(96)])
    f = mean_of_last_days(actual, 3)
    assert set(f) == {T0 + timedelta(hours=i) for i in range(72, 96)}
    assert f[T0 + timedelta(hours=75)] == pytest.approx((51 + 27 + 3) / 3)


def test_a_forecast_never_uses_the_hour_itself_or_anything_after_it() -> None:
    actual = series([float(i % 24) for i in range(120)])
    cut = T0 + timedelta(hours=60)
    poisoned = {t: (-999.0 if t >= cut else v) for t, v in actual.items()}
    for make in (persistence, lambda a: mean_of_last_days(a, 2)):
        clean, dirty = make(actual), make(poisoned)
        for t in clean:
            if t <= cut:  # forecast for hour t may only see hours before t - 24 h
                assert dirty[t] == clean[t], t


def test_gaps_in_the_past_leave_that_hour_without_a_forecast() -> None:
    actual = series([1.0] * 72)
    del actual[T0 + timedelta(hours=10)]
    f = persistence(actual)
    assert T0 + timedelta(hours=34) not in f
    assert T0 + timedelta(hours=35) in f


@pytest.mark.parametrize("lags", [[], [0], [-24], [24, 0]])
def test_bad_lags_are_refused(lags: list[int]) -> None:
    with pytest.raises(ValueError):
        lagged_mean_forecast(series([1.0] * 48), lags)


def test_bad_day_count_is_refused() -> None:
    with pytest.raises(ValueError):
        mean_of_last_days(series([1.0] * 48), 0)


# --- the error measure --------------------------------------------------------------------------


def test_a_perfect_forecast_has_no_error() -> None:
    a = series([0.0, 100.0, 300.0, 0.0])
    s = forecast_skill(a, a)
    assert (s.mae, s.rmse, s.bias, s.hours) == (0.0, 0.0, 0.0, 2)


def test_error_measures_on_known_numbers() -> None:
    actual = series([100.0, 200.0, 0.0, 400.0])
    forecast = series([150.0, 100.0, 0.0, 400.0])  # +50, -100; night hour and exact hit ignored
    s = forecast_skill(actual, forecast)
    assert s.hours == 3  # 100, 200 and 400 W/m2 are active; the zero hour is not
    assert s.mae == pytest.approx(50.0)
    assert s.rmse == pytest.approx(math.sqrt((50**2 + 100**2) / 3))
    assert s.bias == pytest.approx(-50 / 3)


def test_night_hours_do_not_flatter_the_skill() -> None:
    day = [0.0] * 10 + [500.0] * 4 + [0.0] * 10
    actual, forecast = series(day * 2), series([0.0] * 48)
    assert forecast_skill(actual, forecast).mae == pytest.approx(500.0)


def test_nothing_to_compare_is_an_error() -> None:
    with pytest.raises(ValueError):
        forecast_skill(series([0.0, 0.0]), series([0.0, 0.0]))


# --- replaying on a forecast ----------------------------------------------------------------------


def solar_house():  # noqa: ANN201
    return parse_household(
        {
            "daily_base_load_kwh": 6.0,
            "cycle": [{"appliance": "washing_machine", "earliest_hour": 0, "deadline_hour": 24}],
            "solar": {"kwp": 6.0},
        }
    )


def days_of_data(n: int = 8):  # noqa: ANN201
    prices = synthetic_prices(T0, n, 3)
    weather = synthetic_weather(T0 - timedelta(days=4), n + 6, 3)
    return prices, radiation_by_hour(weather), temperature_by_hour(weather)


def test_a_perfect_forecast_changes_nothing() -> None:
    prices, radiation, _ = days_of_data()
    house, planners = solar_house(), default_planners()
    plain = run_appliance_backtest(prices, house, planners, tz=UTC, radiation=radiation)
    oracle = run_appliance_backtest(
        prices, house, planners, tz=UTC, radiation=radiation, radiation_forecast=dict(radiation)
    )
    assert plain == oracle


def test_a_forecast_without_a_full_day_skips_that_day_for_every_planner() -> None:
    prices, radiation, _ = days_of_data(8)
    forecast = persistence(radiation, 24)
    cut = T0 + timedelta(days=3)
    gappy = {t: v for t, v in forecast.items() if not (cut <= t < cut + timedelta(hours=1))}
    results = run_appliance_backtest(
        prices, solar_house(), default_planners(), tz=UTC, radiation=radiation,
        radiation_forecast=gappy,
    )  # fmt: skip
    full = run_appliance_backtest(
        prices, solar_house(), default_planners(), tz=UTC, radiation=radiation,
        radiation_forecast=forecast,
    )  # fmt: skip
    assert {r.days for r in results} == {full[0].days - 1}
    assert len({r.days for r in results}) == 1  # the same days for every strategy


def test_a_rule_that_ignores_the_sun_costs_the_same_whatever_the_forecast() -> None:
    prices, radiation, _ = days_of_data()
    house = solar_house()
    blind = {t: 0.0 for t in radiation}
    a = run_appliance_backtest(
        prices, house, default_planners(), tz=UTC, radiation=radiation, radiation_forecast=blind
    )
    b = run_appliance_backtest(
        prices, house, default_planners(), tz=UTC, radiation=radiation,
        radiation_forecast=dict(radiation),
    )  # fmt: skip
    by_a = {r.name: r for r in a}
    by_b = {r.name: r for r in b}
    assert by_a["run_immediately"].mean_cost_eur == pytest.approx(
        by_b["run_immediately"].mean_cost_eur
    )


def test_not_knowing_the_sun_never_beats_knowing_it() -> None:
    prices, radiation, _ = days_of_data(10)
    house, planners = solar_house(), default_planners()
    blind = {t: 0.0 for t in radiation}
    oracle = {t: radiation[t] for t in blind}
    seen = {
        r.name: r
        for r in run_appliance_backtest(
            prices, house, planners, tz=UTC, radiation=radiation, radiation_forecast=blind
        )
    }
    knew = {
        r.name: r
        for r in run_appliance_backtest(
            prices, house, planners, tz=UTC, radiation=radiation, radiation_forecast=oracle
        )
    }
    assert seen["milp_optimiser"].mean_cost_eur >= knew["milp_optimiser"].mean_cost_eur - 1e-9


def test_a_forecast_error_that_breaks_the_connection_limit_shows_as_invalid_days() -> None:
    house = parse_household(
        {
            "max_grid_kw": 2.5,
            "catalogue": {"cycle": {"heavy": {"profile_kw": [3.0]}}},
            "cycle": [{"appliance": "heavy", "earliest_hour": 0, "deadline_hour": 24}],
            "solar": {"kwp": 6.0},
        }
    )
    prices = synthetic_prices(T0, 4, 1)
    dark = {p.start: 0.0 for p in prices}
    sunny_forecast = {p.start: 800.0 for p in prices}  # promises 3.8 kW of PV every hour
    results = run_appliance_backtest(
        prices, house, default_planners(), tz=UTC, radiation=dark,
        radiation_forecast=sunny_forecast,
    )  # fmt: skip
    by = {r.name: r for r in results}
    # the plan needs the promised sun to stay under 2.5 kW; the real day is dark
    assert by["milp_optimiser"].days_valid == 0 and by["milp_optimiser"].days > 0
    honest = run_appliance_backtest(
        prices, house, default_planners(), tz=UTC, radiation=dark, radiation_forecast=dict(dark)
    )
    assert all(r.days_fallback == r.days for r in honest if r.name == "milp_optimiser")


def test_a_forecast_needs_a_household_with_solar() -> None:
    prices, radiation, _ = days_of_data()
    with pytest.raises(ValueError, match="solar"):
        run_appliance_backtest(
            prices, parse_household({}), default_planners(), tz=UTC,
            radiation_forecast=radiation,
        )  # fmt: skip


def test_a_forecast_covering_no_day_is_an_error() -> None:
    prices, radiation, _ = days_of_data()
    with pytest.raises(ValueError, match="no complete day"):
        run_appliance_backtest(
            prices, solar_house(), default_planners(), tz=UTC, radiation=radiation,
            radiation_forecast={},
        )  # fmt: skip


def test_the_example_household_replays_on_persistence() -> None:
    from pathlib import Path

    house = load_household(
        Path(__file__).resolve().parents[2] / "config/households/example_detailed.toml"
    )
    prices, radiation, outdoor = days_of_data(6)
    results = run_appliance_backtest(
        prices, house, default_planners(), tz=UTC, outdoor=outdoor, radiation=radiation,
        radiation_forecast=persistence(radiation),
    )  # fmt: skip
    assert all(r.days_valid == r.days and r.days_fallback == 0 for r in results)
