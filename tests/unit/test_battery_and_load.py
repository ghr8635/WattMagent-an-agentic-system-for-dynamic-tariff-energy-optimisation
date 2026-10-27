from __future__ import annotations

import pytest

from wattmagent.data.load import synthetic_load_kw
from wattmagent.devices.battery import BatterySession, BatterySpec


def test_charge_stores_less_than_it_draws() -> None:
    b = BatterySession(BatterySpec(initial_soc=0.5))
    drawn = b.step(2.0)  # 2 kW for 1 h
    assert drawn == pytest.approx(2.0)
    assert b.stored_kwh == pytest.approx(5.0 + 2.0 * 0.95)


def test_discharge_delivers_less_than_it_takes() -> None:
    b = BatterySession(BatterySpec(initial_soc=0.5))
    out = b.step(-2.0)
    assert out == pytest.approx(-2.0)
    assert b.stored_kwh == pytest.approx(5.0 - 2.0 / 0.95)


def test_power_limit_capacity_and_reserve_are_enforced() -> None:
    spec = BatterySpec(capacity_kwh=10, max_power_kw=5, initial_soc=0.5)
    b = BatterySession(spec)
    assert b.step(50.0) == pytest.approx(5.0)  # clamped to 5 kW
    for _ in range(10):
        b.step(5.0)
    assert b.stored_kwh == pytest.approx(spec.max_kwh)  # full: nothing more is drawn
    assert b.step(5.0) == pytest.approx(0.0)
    for _ in range(10):
        b.step(-5.0)
    assert b.stored_kwh == pytest.approx(spec.min_kwh)  # reserve is never used
    assert b.step(-5.0) == pytest.approx(0.0)


def test_discharge_never_exceeds_demand() -> None:
    b = BatterySession(BatterySpec())
    assert b.step(-5.0, max_delivery_kwh=1.2) == pytest.approx(-1.2)  # no export
    assert b.step(-5.0, max_delivery_kwh=0.0) == 0.0


def test_invalid_battery_specs_are_rejected() -> None:
    for bad in (
        {"capacity_kwh": 0},
        {"charge_efficiency": 1.2},
        {"min_soc": 0.9, "max_soc": 0.5},
        {"initial_soc": 0.05},  # below the reserve
        {"wear_eur_per_kwh": -0.01},
    ):
        with pytest.raises(ValueError):
            BatterySpec(**bad)  # type: ignore[arg-type]


def test_load_profile_adds_up_to_the_daily_energy() -> None:
    load = synthetic_load_kw(list(range(24)), daily_kwh=9.0)
    assert sum(load) == pytest.approx(9.0)
    assert min(load) > 0 and max(load) == load[19]  # evening peak
    with pytest.raises(ValueError):
        synthetic_load_kw([24])
    with pytest.raises(ValueError):
        synthetic_load_kw([1], daily_kwh=-1)
