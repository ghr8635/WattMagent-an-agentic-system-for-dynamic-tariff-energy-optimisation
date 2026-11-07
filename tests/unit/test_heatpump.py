"""Heat pump heating a building that stores heat in its mass."""

from __future__ import annotations

import json
import math
import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.baselines import cheapest_start, heatpump_thermostat, run_immediately
from wattmagent.appliances.catalogue import ConfigError, default_catalogue
from wattmagent.appliances.household import build_day_inputs, load_household, parse_household
from wattmagent.appliances.models import (
    AppliancePlan,
    BuildingSpec,
    CycleJob,
    CycleSpec,
    DayInputs,
    building_step,
)
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.simulate import appliance_kw, cost_eur, simulate_building
from wattmagent.appliances.verifier import ApplianceRule, verify_appliance_plan
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import synthetic_weather, temperature_by_hour
from wattmagent.planning.household import PlanStatus
from wattmagent.safety.pipeline import Decision

BERLIN = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "households" / "example_heat_pump.toml"
HOUSE = BuildingSpec("house", 10.0, 0.12, 6.0, 20.0, 22.0, 21.0)


def outdoor_cold(n: int = 24) -> list[float]:
    return [-2.0 + 6.0 * math.sin(math.pi * (h - 6) / 12) ** 2 if h > 6 else -2.0 for h in range(n)]


def prices(seed: int, n: int = 24) -> list[float]:
    rng = random.Random(seed)  # noqa: S311
    return [rng.uniform(-0.02, 0.35) for _ in range(n)]


def day(seed: int = 1, **kw: object) -> DayInputs:
    return DayInputs(prices(seed), building=HOUSE, outdoor_c=outdoor_cold(), **kw)  # type: ignore[arg-type]


# --- models ---------------------------------------------------------------------------------


def test_invalid_buildings_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        BuildingSpec("b", 0, 0.1, 5, 20, 22, 21)
    with pytest.raises(ValueError, match="below t_max"):
        BuildingSpec("b", 10, 0.1, 5, 22, 20, 21)
    with pytest.raises(ValueError, match="comfort band"):
        BuildingSpec("b", 10, 0.1, 5, 20, 22, 25)
    with pytest.raises(ValueError, match="cop_min"):
        BuildingSpec("b", 10, 0.1, 5, 20, 22, 21, cop_min=0.5)
    with pytest.raises(ValueError, match="cop_at_7c"):
        BuildingSpec("b", 10, 0.1, 5, 20, 22, 21, cop_at_7c=9.0, cop_max=5.0)
    with pytest.raises(ValueError, match="outdoor_c"):
        DayInputs([0.1] * 24, building=HOUSE)
    with pytest.raises(ValueError, match="outdoor_c"):
        DayInputs([0.1] * 24, building=HOUSE, outdoor_c=[float("nan")] * 24)


def test_cop_rises_with_outdoor_temperature_and_is_clipped() -> None:
    cops = [HOUSE.cop(t) for t in (-30, -10, 0, 7, 15, 40)]
    assert cops == sorted(cops)
    assert HOUSE.cop(7.0) == pytest.approx(3.5)
    assert HOUSE.cop(-30) == HOUSE.cop_min and HOUSE.cop(40) == HOUSE.cop_max


def test_building_step_physics() -> None:
    # Heat equal to the loss leaves the room unchanged; 1 kWh into 10 kWh/K warms it 0.1 K.
    loss = HOUSE.ua_kw_per_k * (21.0 - 0.0)
    assert building_step(HOUSE, 21.0, loss, 0.0) == pytest.approx(21.0)
    assert building_step(HOUSE, 21.0, loss + 1.0, 0.0) == pytest.approx(21.1)
    assert building_step(HOUSE, 21.0, 0.0, 0.0) < 21.0


# --- optimiser ------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(20))
def test_plan_is_valid_and_agrees_with_the_independent_simulation(seed: int) -> None:
    inputs = day(seed)
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    report = verify_appliance_plan(inputs, plan)
    assert report.passed, report.violations
    indoor = simulate_building(inputs, plan.heatpump_kw)
    assert all(HOUSE.t_min_c - 1e-3 <= t <= HOUSE.t_max_c + 1e-3 for t in indoor)
    assert indoor[-1] >= HOUSE.t_initial_c - 1e-3
    assert cost_eur(inputs.prices, appliance_kw(inputs, plan)) == pytest.approx(
        plan.planned_cost_eur or 0.0, abs=1e-6
    )
    for rule in (run_immediately(inputs), cheapest_start(inputs)):
        valid = not verify_appliance_plan(inputs, rule, compare_with_baseline=False).violations
        if valid:
            assert (plan.planned_cost_eur or 0) <= cost_eur(
                inputs.prices, appliance_kw(inputs, rule)
            ) + 1e-6


@pytest.mark.parametrize("seed", range(5))
def test_no_random_feasible_schedule_beats_the_optimiser(seed: int) -> None:
    inputs = day(seed)
    plan = plan_appliances(inputs)
    best = plan.planned_cost_eur or 0.0
    rng = random.Random(seed)  # noqa: S311
    base = heatpump_thermostat(inputs)
    beaten = tried = 0
    for _ in range(300):
        series = [max(0.0, v * rng.uniform(0.2, 2.0)) for v in base]
        candidate = AppliancePlan(PlanStatus.RULE, {}, [], series)
        if verify_appliance_plan(inputs, candidate, compare_with_baseline=False).passed:
            tried += 1
            beaten += cost_eur(inputs.prices, series) < best - 1e-6
    assert beaten == 0
    assert tried >= 20  # enough random feasible schedules were actually compared


def test_heat_is_bought_when_it_is_cheap_and_when_the_cop_is_good() -> None:
    flat = DayInputs([0.20] * 24, building=HOUSE, outdoor_c=outdoor_cold())
    plan = plan_appliances(flat)
    cop = flat.cop
    warm = max(range(24), key=lambda t: cop[t])
    cold = min(range(24), key=lambda t: cop[t])
    heat = [kw * c for kw, c in zip(plan.heatpump_kw, cop, strict=True)]
    # with flat prices the efficient hours carry more heat than the worst hours
    assert (
        sum(heat[t] for t in range(24) if cop[t] >= cop[warm] - 0.2)
        > sum(heat[t] for t in range(24) if cop[t] <= cop[cold] + 0.2) * 0.5
    )
    cheap = [0.30] * 24
    cheap[3] = cheap[4] = -0.05
    plan2 = plan_appliances(DayInputs(cheap, building=HOUSE, outdoor_c=outdoor_cold()))
    assert plan2.heatpump_kw[3] + plan2.heatpump_kw[4] > 0


def test_a_building_that_cannot_be_kept_warm_is_infeasible() -> None:
    weak = replace(HOUSE, max_heat_kw=0.5)
    inputs = DayInputs([0.1] * 24, building=weak, outdoor_c=[-15.0] * 24)
    assert plan_appliances(inputs).status is PlanStatus.INFEASIBLE


def test_the_model_cannot_cool_so_a_hot_day_is_infeasible() -> None:
    inputs = DayInputs([0.1] * 24, building=HOUSE, outdoor_c=[32.0] * 24)
    assert plan_appliances(inputs).status is PlanStatus.INFEASIBLE


def test_heat_pump_together_with_tank_cycles_and_a_grid_limit() -> None:
    from wattmagent.appliances.models import TankSpec

    tank = TankSpec("t", 200.0, 3.0, 45.0, 60.0, 52.0, loss_kw_per_k=0.0019)
    draws = [4.0 / 24] * 24
    inputs = DayInputs(
        prices(7),
        jobs=[CycleJob(CycleSpec("w", (0.9, 0.5)), 8, 18)],
        tank=tank,
        tank_draw_kwh=draws,
        building=HOUSE,
        outdoor_c=outdoor_cold(),
        base_load_kw=[1.0] * 24,
        max_grid_kw=4.5,
    )
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    assert verify_appliance_plan(inputs, plan).passed
    total = appliance_kw(inputs, plan)
    assert max(t + 1.0 for t in total) <= 4.5 + 1e-6


# --- verifier: deliberately broken plans -----------------------------------------------------


def good(inputs: DayInputs) -> AppliancePlan:
    return plan_appliances(inputs)


def test_attack_pump_above_its_limit_negative_or_wrong_length() -> None:
    inputs = day()
    plan = good(inputs)
    over = plan.heatpump_kw[:]
    over[3] = 50.0
    rules = verify_appliance_plan(inputs, replace(plan, heatpump_kw=over)).rules
    assert ApplianceRule.HEATPUMP_POWER.value in rules
    neg = plan.heatpump_kw[:]
    neg[3] = -1.0
    assert (
        ApplianceRule.HEATPUMP_POWER.value
        in verify_appliance_plan(inputs, replace(plan, heatpump_kw=neg)).rules
    )
    assert (
        ApplianceRule.SHAPE.value
        in verify_appliance_plan(inputs, replace(plan, heatpump_kw=[0.0] * 5)).rules
    )


def test_attack_house_too_cold_too_hot_and_ending_cold() -> None:
    inputs = day()
    plan = good(inputs)
    cold = verify_appliance_plan(inputs, replace(plan, heatpump_kw=[0.0] * 24)).rules
    assert ApplianceRule.INDOOR_COMFORT.value in cold
    limit = [HOUSE.max_heat_kw / c for c in inputs.cop]
    hot = verify_appliance_plan(inputs, replace(plan, heatpump_kw=limit)).rules
    assert ApplianceRule.INDOOR_COMFORT.value in hot
    ends_cold = plan.heatpump_kw[:]
    last = max(i for i, kw in enumerate(ends_cold) if kw > 0)
    ends_cold[last] = 0.0
    out = set(verify_appliance_plan(inputs, replace(plan, heatpump_kw=ends_cold)).rules)
    assert {"indoor_end", "indoor_comfort"} & out


def test_attack_pump_power_without_a_building_and_nan() -> None:
    plain = DayInputs([0.1] * 24, jobs=[CycleJob(CycleSpec("w", (0.9,)), 0, 24)])
    plan = replace(run_immediately(plain), heatpump_kw=[1.0] * 24)
    assert ApplianceRule.SHAPE.value in verify_appliance_plan(plain, plan).rules
    inputs = day()
    nan = good(inputs).heatpump_kw[:]
    nan[2] = float("nan")
    assert (
        ApplianceRule.NOT_FINITE.value
        in verify_appliance_plan(inputs, replace(good(inputs), heatpump_kw=nan)).rules
    )


def test_attack_pump_breaks_the_connection_limit() -> None:
    inputs = replace(day(), max_grid_kw=1.0)
    plan = replace(good(day()), heatpump_kw=[1.8] * 24)
    assert ApplianceRule.GRID_LIMIT.value in verify_appliance_plan(inputs, plan).rules


# --- pipeline -------------------------------------------------------------------------------


def test_pipeline_optimises_and_reports_heat_pump_in_the_audit_summary() -> None:
    out = plan_appliances_safely(day())
    assert out.decision is Decision.OPTIMISED and out.report.passed
    assert out.audit["inputs"]["heatpump"] is True


def test_pipeline_reports_an_unreachable_comfort_band() -> None:
    inputs = DayInputs([0.1] * 24, building=replace(HOUSE, max_heat_kw=0.5), outdoor_c=[-15.0] * 24)
    out = plan_appliances_safely(inputs)
    assert out.decision is Decision.FALLBACK and not out.request_satisfied


# --- catalogue and household ----------------------------------------------------------------


def test_catalogue_has_heat_pumps_that_build() -> None:
    cat = default_catalogue()
    assert {"heat_pump_efficient_house", "heat_pump_older_house"} <= set(cat.heatpumps)
    for m in cat.heatpumps.values():
        assert m.description
        BuildingSpec(
            m.key, m.heat_capacity_kwh_per_k, m.ua_kw_per_k, m.max_heat_kw, m.t_min_c,
            m.t_max_c, m.t_initial_c, m.cop_at_7c, m.cop_slope_per_k, m.cop_min, m.cop_max,
        )  # fmt: skip


def test_example_heat_pump_household_loads_and_needs_weather_to_plan_the_pump() -> None:
    hh = load_household(EXAMPLE)
    assert hh.heatpump is not None
    hours = list(range(24))
    without = build_day_inputs(hh, [0.2] * 24, hours)
    assert without.building is None
    with_weather = build_day_inputs(hh, [0.2] * 24, hours, outdoor_c=[1.0] * 24)
    assert with_weather.building is not None and len(with_weather.outdoor_c) == 24
    with pytest.raises(ValueError, match="same length"):
        build_day_inputs(hh, [0.2] * 24, hours, outdoor_c=[1.0] * 3)


def test_heat_pump_overrides_and_errors() -> None:
    hh = parse_household(
        {"heatpump": {"appliance": "heat_pump_older_house", "heat_capacity_kwh_per_k": 4.0}}
    )
    assert hh.heatpump is not None and hh.heatpump.heat_capacity_kwh_per_k == 4.0
    own = parse_household(
        {
            "catalogue": {
                "heatpump": {
                    "mine": {
                        "heat_capacity_kwh_per_k": 8,
                        "ua_kw_per_k": 0.2,
                        "max_heat_kw": 7,
                        "t_min_c": 19,
                        "t_max_c": 22,
                        "t_initial_c": 20,
                    }
                }
            },
            "heatpump": {"appliance": "mine"},
        }
    )
    assert own.heatpump is not None and own.heatpump.cop_at_7c == 3.5
    for raw, text in (
        ({"heatpump": {"appliance": "furnace"}}, "unknown appliance"),
        ({"heatpump": "x"}, "expected a table"),
        ({"heatpump": {"appliance": "heat_pump_older_house", "bogus": 1}}, "unknown key"),
        ({"heatpump": {"appliance": "heat_pump_older_house", "t_initial_c": 30}}, "comfort band"),
        ({"heatpump": {"appliance": "heat_pump_older_house", "cop_min": 0.2}}, "cop_min"),
        ({"catalogue": {"heatpump": {"x": {"ua_kw_per_k": 1}}}}, "missing"),
        ({"catalogue": {"heatpump": {"x": 5}}}, "expected a table"),
    ):
        with pytest.raises(ConfigError, match=text):
            parse_household(raw)  # type: ignore[arg-type]


# --- weather and backtest --------------------------------------------------------------------


def test_synthetic_weather_is_deterministic_seasonal_and_dark_at_night() -> None:
    a = synthetic_weather(datetime(2025, 1, 1, tzinfo=UTC), 10, 3)
    assert a == synthetic_weather(datetime(2025, 1, 1, tzinfo=UTC), 10, 3)
    assert a != synthetic_weather(datetime(2025, 1, 1, tzinfo=UTC), 10, 4)
    winter = synthetic_weather(datetime(2025, 1, 1, tzinfo=UTC), 30, 1)
    summer = synthetic_weather(datetime(2025, 7, 1, tzinfo=UTC), 30, 1)
    mean = lambda pts: sum(p.temperature_c for p in pts) / len(pts)  # noqa: E731
    assert mean(summer) > mean(winter) + 8
    assert all(p.shortwave_w_m2 == 0 for p in winter if p.start.hour in (0, 1, 2, 3, 22, 23))
    assert max(p.shortwave_w_m2 for p in summer) > max(p.shortwave_w_m2 for p in winter)
    assert len(temperature_by_hour(winter)) == 30 * 24
    with pytest.raises(ValueError, match="positive"):
        synthetic_weather(datetime(2025, 1, 1, tzinfo=UTC), 0, 1)


def run(start: datetime, days: int) -> dict[str, object]:
    hh = load_household(EXAMPLE)
    pr = synthetic_prices(start.replace(tzinfo=BERLIN), days, 5)
    wx = synthetic_weather(pr[0].start.astimezone(UTC).replace(hour=0), days + 3, 5)
    res = run_appliance_backtest(
        pr, hh, default_planners(), tz=BERLIN, outdoor=temperature_by_hour(wx)
    )
    return {r.name: r for r in res}


def test_winter_backtest_heats_every_day_and_the_optimiser_is_valid_and_cheapest() -> None:
    res = run(datetime(2025, 1, 5), 12)
    imm, opt = res["run_immediately"], res["milp_optimiser"]
    assert opt.heating_days == opt.days == imm.days > 0  # type: ignore[attr-defined]
    assert opt.days_valid == opt.days and opt.days_fallback == 0  # type: ignore[attr-defined]
    assert opt.mean_cost_eur < res["cheapest_start"].mean_cost_eur  # type: ignore[attr-defined]
    assert opt.mean_cost_eur < imm.mean_cost_eur  # type: ignore[attr-defined]


def test_summer_days_have_no_heating_need() -> None:
    res = run(datetime(2025, 7, 5), 8)
    assert all(r.heating_days == 0 for r in res.values())  # type: ignore[attr-defined]


def test_backtest_needs_weather_for_a_heat_pump_household_and_covering_every_day() -> None:
    hh = load_household(EXAMPLE)
    pr = synthetic_prices(datetime(2025, 1, 5, tzinfo=BERLIN), 5, 1)
    with pytest.raises(ValueError, match="outdoor temperatures are required"):
        run_appliance_backtest(pr, hh, default_planners(), tz=BERLIN)
    with pytest.raises(ValueError, match="no weather for"):
        run_appliance_backtest(pr, hh, default_planners(), tz=BERLIN, outdoor={})
    wx = temperature_by_hour(synthetic_weather(datetime(2025, 1, 4, tzinfo=UTC), 10, 1))
    naive = [replace(p, start=p.start.replace(tzinfo=None)) for p in pr]
    assert run_appliance_backtest(naive, hh, default_planners(), tz=None, outdoor=wx)


def cli(*args: str) -> str:
    import contextlib
    import io
    import sys

    from wattmagent.appliances.__main__ import main

    buf, old = io.StringIO(), sys.argv
    sys.argv = ["wattmagent.appliances", *args]
    try:
        with contextlib.redirect_stdout(buf):
            main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_cli_with_heat_pump_synthetic_and_with_a_saved_weather_file(tmp_path: Path) -> None:
    out = cli("--household", str(EXAMPLE), "--synthetic", "--days", "8")
    assert "SYNTHETIC weather" in out and "heating" in out and "milp_optimiser" in out

    rows = ["timestamp,price_eur_mwh"]
    for d in range(1, 4):
        for h in range(24):
            rows.append(f"2025-01-{d:02d}T{h:02d}:00:00+00:00,{60 + 15 * (h % 6)}")
    csv = tmp_path / "p.csv"
    csv.write_text("\n".join(rows), encoding="utf-8")
    fixture = ROOT / "tests" / "fixtures" / "open_meteo_archive_berlin.json"
    out2 = cli("--household", str(EXAMPLE), "--csv", str(csv), "--weather-json", str(fixture))
    assert "weather file: open_meteo_archive_berlin.json" in out2
    with pytest.raises(SystemExit):
        cli("--household", str(EXAMPLE), "--csv", str(csv))
    assert json.loads(fixture.read_text(encoding="utf-8"))["hourly_units"]["temperature_2m"] == "°C"
