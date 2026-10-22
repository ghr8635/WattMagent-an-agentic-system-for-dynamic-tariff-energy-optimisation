from __future__ import annotations

from datetime import datetime, timedelta

from wattmagent.backtest.runner import run_backtest, run_day, window_slices
from wattmagent.data.prices import PricePoint, synthetic_prices
from wattmagent.devices.ev import EVSession, EVSpec
from wattmagent.planning.baselines import BASELINES, charge_immediately, cheapest_hours


def flat(price: float, hours: int, start: datetime = datetime(2025, 1, 1)) -> list[PricePoint]:
    return [PricePoint(start + timedelta(hours=i), price) for i in range(hours)]


def test_spec_window_wraps_midnight_and_energy_need() -> None:
    s = EVSpec(plug_in_hour=18, depart_hour=7)
    assert s.window_hours == 13
    assert abs(s.grid_energy_needed_kwh - (0.5 * 60 / 0.92)) < 1e-9
    assert EVSpec(plug_in_hour=9, depart_hour=9).window_hours == 24


def test_spec_validation() -> None:
    for bad in (dict(efficiency=0.0), dict(capacity_kwh=-1), dict(target_soc=1.5)):
        try:
            EVSpec(**bad)  # type: ignore[arg-type]
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_session_clamps_power_and_capacity() -> None:
    s = EVSession(EVSpec(max_charge_kw=10.0, efficiency=1.0, arrival_soc=0.9, capacity_kwh=100.0))
    assert s.charge(50.0) == 10.0  # clamped to charger limit
    assert s.charge(-5.0) == 0.0  # negative request is ignored
    s2 = EVSession(EVSpec(max_charge_kw=50.0, efficiency=1.0, arrival_soc=0.98, capacity_kwh=100.0))
    assert abs(s2.charge(50.0) - 2.0) < 1e-9  # only 2 kWh of room left
    assert abs(s2.soc - 1.0) < 1e-9


def test_session_losses_make_grid_energy_exceed_stored() -> None:
    s = EVSession(EVSpec(efficiency=0.9, max_charge_kw=10.0, capacity_kwh=100.0, arrival_soc=0.0))
    drawn = s.charge(10.0)
    assert drawn == 10.0 and abs(s.stored_kwh - 9.0) < 1e-9


def test_baselines_deliver_exact_energy_and_respect_limits() -> None:
    spec = EVSpec()
    prices = [0.1 + 0.01 * (i % 5) for i in range(spec.window_hours)]
    for planner in (charge_immediately, cheapest_hours):
        sched = planner(prices, spec)
        assert len(sched) == len(prices)
        assert max(sched) <= spec.max_charge_kw + 1e-9 and min(sched) >= 0.0
        assert abs(sum(sched) - spec.grid_energy_needed_kwh) < 1e-6


def test_cheapest_hours_picks_cheapest_slots() -> None:
    spec = EVSpec(
        max_charge_kw=10.0, efficiency=1.0, arrival_soc=0.0, target_soc=0.2, capacity_kwh=100.0
    )  # needs 20 kWh = two full hours
    sched = cheapest_hours([5.0, 1.0, 9.0, 2.0, 7.0], spec)
    assert [i for i, kw in enumerate(sched) if kw > 0] == [1, 3]


def test_hindsight_never_costs_more_than_immediate_on_any_seed() -> None:
    spec = EVSpec()
    for seed in range(5):
        prices = synthetic_prices(datetime(2025, 3, 1), 30, seed)
        res = {r.name: r for r in run_backtest(prices, spec, BASELINES)}
        imm = res["charge_immediately"]
        hind = res["cheapest_hours (hindsight bound)"]
        assert hind.total_cost_eur <= imm.total_cost_eur + 1e-9
        assert imm.days_target_met == imm.days and hind.days_target_met == hind.days


def test_flat_prices_give_equal_cost() -> None:
    spec = EVSpec()
    prices = flat(0.2, 24 * 5)
    res = run_backtest(prices, spec, BASELINES)
    assert abs(res[0].total_cost_eur - res[1].total_cost_eur) < 1e-9


def test_infeasible_window_is_reported_not_hidden() -> None:
    spec = EVSpec(max_charge_kw=1.0)  # 13 h x 1 kW cannot deliver ~32.6 kWh
    day = run_day(flat(0.2, 13, datetime(2025, 1, 1, 18)), spec, cheapest_hours)
    assert day.target_met is False


def test_billing_uses_actual_prices_not_the_planners_view() -> None:
    spec = EVSpec(
        max_charge_kw=10.0,
        efficiency=1.0,
        arrival_soc=0.0,
        target_soc=0.1,
        capacity_kwh=100.0,
        plug_in_hour=0,
        depart_hour=3,
    )  # 10 kWh, 3 slots
    window = [PricePoint(datetime(2025, 1, 1, h), p) for h, p in enumerate([1.0, 2.0, 3.0])]
    # The planner is told slot 2 is cheapest, but slot 0 is truly cheapest.
    lying_view = lambda actual: [9.0, 9.0, 0.0]  # noqa: E731
    day = run_day(window, spec, cheapest_hours, view=lying_view)
    assert abs(day.cost_eur - 10.0 * 3.0) < 1e-9  # billed 3.0 EUR/kWh, not 0


def test_window_slices_and_too_short_series() -> None:
    spec = EVSpec()
    prices = synthetic_prices(datetime(2025, 1, 1), 3, 0)
    sl = list(window_slices(prices, spec))
    assert sl and all(prices[s][0].start.hour == 18 for s in sl)
    try:
        run_backtest(flat(0.1, 5), spec, BASELINES)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_gapped_series_is_rejected() -> None:
    p = flat(0.1, 30)
    gapped = p[:10] + p[12:]
    try:
        run_backtest(gapped, EVSpec(), BASELINES)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
