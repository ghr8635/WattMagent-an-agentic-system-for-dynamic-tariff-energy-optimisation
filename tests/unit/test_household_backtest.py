from __future__ import annotations

import contextlib
import io
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from wattmagent.backtest.__main__ import main
from wattmagent.backtest.household import (
    Scenario,
    day_slices,
    default_planners,
    format_household_summary,
    run_household_backtest,
)
from wattmagent.data.prices import PricePoint, synthetic_prices
from wattmagent.devices.battery import BatterySpec
from wattmagent.devices.ev import EVSpec

BERLIN = ZoneInfo("Europe/Berlin")
EV = EVSpec()


def prices(days: int, start: datetime = datetime(2024, 1, 1, tzinfo=UTC)) -> list[PricePoint]:
    return synthetic_prices(start, days, seed=11)


def test_day_slices_cover_daylight_saving_days() -> None:
    p = prices(300, datetime(2024, 3, 1, tzinfo=UTC))  # spans 31 Mar and 27 Oct 2024
    lengths = {sl.stop - sl.start for sl in day_slices(p, EV, BERLIN)}
    assert lengths == {23, 24, 25}


def test_strategies_rank_as_expected_and_meet_the_target() -> None:
    p = prices(40)
    res = {
        r.name: r
        for r in run_household_backtest(
            p, Scenario(EV, BatterySpec(wear_eur_per_kwh=0.0)), default_planners(True), tz=BERLIN
        )
    }
    assert all(r.days_target_met == r.days and r.days_fallback == 0 for r in res.values())
    imm, cheap = res["ev_immediately"], res["ev_cheapest_hours"]
    ev_only, full = res["milp_ev_only"], res["milp_ev_and_battery"]
    assert cheap.mean_net_eur < imm.mean_net_eur
    assert ev_only.mean_net_eur == pytest.approx(cheap.mean_net_eur, abs=1e-6)
    assert full.mean_net_eur <= ev_only.mean_net_eur + 1e-9
    assert full.mean_battery_kwh > 0 and imm.mean_battery_kwh == 0


def test_billing_uses_actual_prices_not_the_planners_view() -> None:
    p = prices(40)
    scenario = Scenario(EV, BatterySpec())
    true_view = run_household_backtest(
        p, scenario, {"m": default_planners(True)["milp_ev_and_battery"]}, tz=BERLIN
    )
    wrong_view = run_household_backtest(
        p,
        scenario,
        {"m": default_planners(True)["milp_ev_and_battery"]},
        view=lambda actual: list(actual)[::-1],  # planner sees prices in reverse order
        tz=BERLIN,
    )
    assert wrong_view[0].mean_net_eur > true_view[0].mean_net_eur


def test_infeasible_days_are_counted_and_use_the_fallback() -> None:
    weak = EVSpec(max_charge_kw=3.0, arrival_soc=0.0, target_soc=1.0)
    res = run_household_backtest(
        prices(20), Scenario(weak, BatterySpec()), default_planners(True), tz=BERLIN
    )
    for r in res:
        assert r.days_fallback == r.days or r.name in ("ev_immediately", "ev_cheapest_hours")
    full = next(r for r in res if r.name == "milp_ev_and_battery")
    assert full.days_fallback == full.days and full.days_target_met == 0  # reported, not hidden


def test_gappy_series_and_short_series_are_rejected() -> None:
    p = prices(10)
    with pytest.raises(ValueError, match="gap-free"):
        run_household_backtest(p[:30] + p[32:], Scenario(EV), default_planners(False), tz=BERLIN)
    with pytest.raises(ValueError, match="too short"):
        run_household_backtest(p[:10], Scenario(EV), default_planners(False), tz=BERLIN)


def test_table_lists_every_strategy() -> None:
    res = run_household_backtest(
        prices(10), Scenario(EV, BatterySpec()), default_planners(True), tz=BERLIN
    )
    table = format_household_summary(res, baseline="ev_immediately")
    for name in ("ev_immediately", "ev_cheapest_hours", "milp_ev_only", "milp_ev_and_battery"):
        assert name in table


def run_cli(*args: str) -> str:
    buf, old = io.StringIO(), sys.argv
    sys.argv = ["wattmagent.backtest", *args]
    try:
        with contextlib.redirect_stdout(buf):
            main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_cli_household_mode_is_labelled() -> None:
    out = run_cli("--synthetic", "--days", "8", "--seed", "2", "--household")
    assert "SYNTHETIC" in out and "milp_ev_and_battery" in out and "wear" in out


def test_cli_household_without_battery_has_no_battery_strategy() -> None:
    out = run_cli("--synthetic", "--days", "8", "--household", "--battery-kwh", "0")
    assert "milp_ev_only" in out and "milp_ev_and_battery" not in out
