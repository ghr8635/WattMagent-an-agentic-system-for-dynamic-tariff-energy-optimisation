"""Rooftop solar: cost model, optimiser consistency, verifier, config, backtest, CLI."""

from __future__ import annotations

import contextlib
import io
import itertools
import math
import random
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wattmagent.appliances.backtest import default_planners, run_appliance_backtest
from wattmagent.appliances.baselines import run_immediately
from wattmagent.appliances.catalogue import ConfigError
from wattmagent.appliances.household import (
    SolarModel,
    build_day_inputs,
    load_household,
    parse_household,
    pv_power_kw,
)
from wattmagent.appliances.models import BuildingSpec, CycleJob, CycleSpec, DayInputs, TankSpec
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.simulate import appliance_kw, cost_eur, min_import_kw, plan_cost
from wattmagent.appliances.verifier import ApplianceRule, verify_appliance_plan
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import (
    radiation_by_hour,
    solar_yield_kwh_per_kwp,
    synthetic_weather,
    temperature_by_hour,
)
from wattmagent.planning.household import PlanStatus
from wattmagent.safety.pipeline import Decision

BERLIN = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parents[2]
FULL = ROOT / "config" / "households" / "example_full.toml"
WASH = CycleSpec("wash", (0.9, 0.5))
DISH = CycleSpec("dish", (0.55, 0.25, 0.25))


def sun(peak: float = 4.0, n: int = 24) -> list[float]:
    return [
        max(0.0, peak * math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0 for h in range(n)
    ]


def prices(seed: int, n: int = 24, low: float = -0.05) -> list[float]:
    rng = random.Random(seed)  # noqa: S311
    return [rng.uniform(low, 0.40) for _ in range(n)]


# --- inputs and cost model --------------------------------------------------------------------


def test_invalid_solar_inputs() -> None:
    with pytest.raises(ValueError, match="pv_kw"):
        DayInputs([0.1] * 24, pv_kw=[1.0] * 5)
    with pytest.raises(ValueError, match="pv_kw"):
        DayInputs([0.1] * 24, pv_kw=[-1.0] + [0.0] * 23)
    with pytest.raises(ValueError, match="export_price"):
        DayInputs([0.1] * 24, export_price_eur_kwh=-0.1)


def test_export_value_never_exceeds_the_import_price() -> None:
    inputs = DayInputs([0.30, 0.05, -0.04], pv_kw=[1.0, 1.0, 1.0], export_price_eur_kwh=0.08)
    assert inputs.export_value == [0.08, 0.05, -0.04]


def test_without_solar_cost_is_just_the_appliance_energy() -> None:
    inputs = DayInputs(prices(1), jobs=[CycleJob(WASH, 0, 24)], base_load_kw=[2.0] * 24)
    kw = appliance_kw(inputs, run_immediately(inputs))
    assert plan_cost(inputs, kw) == pytest.approx(cost_eur(inputs.prices, kw))


def test_plan_cost_uses_own_solar_first_and_exports_the_surplus() -> None:
    inputs = DayInputs(
        [0.30] * 3, pv_kw=[3.0, 3.0, 0.0], base_load_kw=[1.0, 1.0, 1.0], export_price_eur_kwh=0.08
    )
    # slot 0: demand 1 + 1 = 2 kW, solar 3 -> export 1 kW; slot 1: demand 1, export 2 kW;
    # slot 2: no solar, import 1 kW
    cost = plan_cost(inputs, [1.0, 0.0, 0.0])
    assert cost == pytest.approx(-0.08 * 1.0 - 0.08 * 2.0 + 0.30 * 1.0)


def test_plan_cost_curtails_when_exporting_would_cost_money() -> None:
    inputs = DayInputs([-0.10], pv_kw=[4.0], base_load_kw=[1.0], export_price_eur_kwh=0.08)
    # exporting at -0.10 would cost money; the surplus is curtailed. The cheapest split even
    # imports the whole demand at the negative price and curtails all solar.
    assert plan_cost(inputs, [0.0]) == pytest.approx(-0.10 * 1.0)


def test_min_import_accounts_for_solar() -> None:
    inputs = DayInputs([0.1] * 2, pv_kw=[0.0, 3.0], base_load_kw=[2.0, 2.0])
    assert min_import_kw(inputs, 0, 1.0) == pytest.approx(3.0)
    assert min_import_kw(inputs, 1, 1.0) == pytest.approx(0.0)


# --- optimiser ------------------------------------------------------------------------------


def brute_force(inputs: DayInputs) -> float:
    best = math.inf
    for starts in itertools.product(*(job.starts for job in inputs.jobs)):
        series = [0.0] * inputs.slots
        for job, s in zip(inputs.jobs, starts, strict=True):
            for i, kw in enumerate(job.spec.profile_kw):
                series[s + i] += kw
        best = min(best, plan_cost(inputs, series))
    return best


@pytest.mark.parametrize("seed", range(40))
def test_cycles_with_solar_match_brute_force(seed: int) -> None:
    export = random.Random(seed).choice([0.0, 0.05, 0.08, 0.5])  # noqa: S311
    inputs = DayInputs(
        prices(seed),
        jobs=[CycleJob(WASH, 6, 20), CycleJob(DISH, 8, 22)],
        base_load_kw=[0.4] * 24,
        pv_kw=sun(3.0),
        export_price_eur_kwh=export,
    )
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    assert plan.planned_cost_eur == pytest.approx(brute_force(inputs), abs=1e-6)
    assert plan_cost(inputs, appliance_kw(inputs, plan)) == pytest.approx(
        plan.planned_cost_eur or 0.0, abs=1e-6
    )
    assert verify_appliance_plan(inputs, plan).passed


def test_everything_together_is_consistent_with_the_independent_cost() -> None:
    tank = TankSpec("t", 200.0, 3.0, 45.0, 60.0, 52.0, loss_kw_per_k=0.0019)
    house = BuildingSpec("h", 10.0, 0.12, 6.0, 20.0, 22.0, 21.0)
    for seed in range(10):
        inputs = DayInputs(
            prices(seed, low=-0.02),
            jobs=[CycleJob(WASH, 8, 18)],
            tank=tank,
            tank_draw_kwh=[4.0 / 24] * 24,
            building=house,
            outdoor_c=[1.0 + 3 * math.sin(h / 4) for h in range(24)],
            base_load_kw=[0.5] * 24,
            pv_kw=sun(3.0),
            export_price_eur_kwh=0.08,
            max_grid_kw=12.0,
        )
        plan = plan_appliances(inputs)
        assert plan.status is PlanStatus.OPTIMAL
        report = verify_appliance_plan(inputs, plan)
        assert report.passed, report.violations
        assert plan_cost(inputs, appliance_kw(inputs, plan)) == pytest.approx(
            plan.planned_cost_eur or 0.0, abs=1e-6
        )


def test_a_flexible_load_moves_into_the_sun_when_export_pays_little() -> None:
    inputs = DayInputs(
        [0.30] * 24,
        jobs=[CycleJob(WASH, 0, 24)],
        base_load_kw=[0.2] * 24,
        pv_kw=sun(4.0),
        export_price_eur_kwh=0.0,
    )
    plan = plan_appliances(inputs)
    # every hour the machine runs is fully covered by the sun (any such window ties)
    for t, kw in enumerate(plan.cycle_kw["wash"]):
        if kw > 0:
            assert inputs.pv[t] >= inputs.base_kw[t] + kw - 1e-9
    no_sun = replace(inputs, pv_kw=[])
    shifted = plan_cost(inputs, appliance_kw(inputs, plan))
    unshifted = plan_cost(inputs, [0.0] * 24)
    assert shifted == pytest.approx(unshifted)  # the wash cost nothing extra
    assert plan_appliances(no_sun).planned_cost_eur == pytest.approx(0.30 * WASH.energy_kwh)


def test_a_high_export_price_removes_the_reason_to_self_consume() -> None:
    # exporting earns as much as importing costs (capped at the import price), so timing
    # no longer matters and the cheapest plan equals the cheapest by price alone
    p = [0.30] * 24
    inputs = DayInputs(p, jobs=[CycleJob(WASH, 0, 24)], pv_kw=sun(4.0), export_price_eur_kwh=5.0)
    plan = plan_appliances(inputs)
    flat = plan_cost(inputs, [0.0] * 24)
    assert (plan.planned_cost_eur or 0) == pytest.approx(flat + 0.30 * WASH.energy_kwh)


def test_negative_prices_with_solar_stay_bounded() -> None:
    inputs = DayInputs(
        [-0.10] * 24,
        jobs=[CycleJob(WASH, 0, 24)],
        base_load_kw=[0.3] * 24,
        pv_kw=sun(5.0),
        export_price_eur_kwh=0.08,
    )
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    assert math.isfinite(plan.planned_cost_eur or math.nan)


def test_grid_limit_counts_grid_import_so_solar_makes_room() -> None:
    big = CycleSpec("big", (2.0,))
    sunny = [0.0] * 24
    sunny[12] = 2.0
    inputs = DayInputs(
        [0.1] * 24,
        jobs=[CycleJob(big, 0, 24)],
        base_load_kw=[3.0] * 24,
        pv_kw=sunny,
        max_grid_kw=3.5,  # base 3 + big 2 = 5 kW: only possible while solar supplies 1.5+
        export_price_eur_kwh=0.0,
    )
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    assert plan.cycle_kw["big"][12] == 2.0
    without = replace(inputs, pv_kw=[])
    assert plan_appliances(without).status is PlanStatus.INFEASIBLE


# --- verifier -------------------------------------------------------------------------------


def test_attack_grid_limit_with_solar_and_dearer_than_baseline() -> None:
    sunny = DayInputs(
        [0.1] * 24,
        jobs=[CycleJob(CycleSpec("big", (2.0,)), 0, 24)],
        base_load_kw=[3.0] * 24,
        pv_kw=sun(2.0),
        max_grid_kw=3.5,
    )
    bad = run_immediately(sunny)  # starts at slot 0, no sun
    assert (
        ApplianceRule.GRID_LIMIT.value
        in verify_appliance_plan(sunny, bad, compare_with_baseline=False).rules
    )
    cheap_then_dear = DayInputs(
        [0.05] * 6 + [0.50] * 18,
        jobs=[CycleJob(WASH, 0, 24)],
        base_load_kw=[0.5] * 24,
        pv_kw=sun(1.0),
    )
    from wattmagent.appliances.simulate import place

    worst = replace(
        run_immediately(cheap_then_dear), cycle_kw={"wash": place(WASH.profile_kw, 22, 24)}
    )
    assert ApplianceRule.COST.value in verify_appliance_plan(cheap_then_dear, worst).rules


def test_pipeline_audit_records_solar_without_exposing_values() -> None:
    inputs = DayInputs(
        prices(2), jobs=[CycleJob(WASH, 6, 20)], base_load_kw=[0.4] * 24, pv_kw=sun(3.0)
    )
    out = plan_appliances_safely(inputs)
    assert out.decision is Decision.OPTIMISED and out.audit["inputs"]["solar"] is True
    assert "pv" not in out.audit["inputs"]


# --- household config -------------------------------------------------------------------------


def test_full_example_loads_with_solar_and_plans() -> None:
    hh = load_household(FULL)
    assert hh.solar == SolarModel(6.0, 0.8, 0.08) and hh.heatpump and hh.tank
    ghi = [0.0] * 24
    ghi[12] = 800.0
    inputs = build_day_inputs(hh, [0.2] * 24, list(range(24)), [2.0] * 24, ghi)
    assert inputs.pv_kw[12] == pytest.approx(pv_power_kw(hh.solar, 800.0)) == pytest.approx(3.84)
    assert inputs.export_price_eur_kwh == 0.08 and inputs.building is not None
    assert plan_appliances_safely(inputs).decision is Decision.OPTIMISED
    plain = build_day_inputs(hh, [0.2] * 24, list(range(24)), [2.0] * 24)
    assert not plain.pv_kw and plain.export_price_eur_kwh == 0.0
    with pytest.raises(ValueError, match="radiation_w_m2"):
        build_day_inputs(hh, [0.2] * 24, list(range(24)), None, [1.0] * 3)


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ({"solar": "x"}, "expected a table"),
        ({"solar": {"kwp": 5, "tilt": 30}}, "unknown key"),
        ({"solar": {}}, "kwp is required"),
        ({"solar": {"kwp": "big"}}, "not a number"),
        ({"solar": {"kwp": 0}}, "kwp"),
        ({"solar": {"kwp": 500}}, "kwp"),
        ({"solar": {"kwp": 5, "performance_ratio": 1.5}}, "performance_ratio"),
        ({"solar": {"kwp": 5, "export_price_eur_kwh": -1}}, "negative"),
    ],
)
def test_bad_solar_config(raw: dict[str, object], text: str) -> None:
    with pytest.raises(ConfigError, match=text):
        parse_household(raw)


def test_solar_defaults() -> None:
    hh = parse_household({"solar": {"kwp": 3}})
    assert hh.solar == SolarModel(3.0, 0.8, 0.08)


# --- weather helpers, backtest, CLI --------------------------------------------------------------


def test_yield_helper() -> None:
    pts = synthetic_weather(datetime(2025, 6, 1, tzinfo=UTC), 10, 1)
    expected = sum(p.shortwave_w_m2 for p in pts) / 1000 * 0.8
    assert solar_yield_kwh_per_kwp(pts, 0.8) == pytest.approx(expected)
    assert len(radiation_by_hour(pts)) == 240


def run(start: datetime, days: int) -> dict[str, object]:
    hh = load_household(FULL)
    pr = synthetic_prices(start.replace(tzinfo=BERLIN), days, 3)
    w = synthetic_weather(pr[0].start.astimezone(UTC) - timedelta(days=1), days + 3, 3)
    res = run_appliance_backtest(
        pr, hh, default_planners(), tz=BERLIN,
        outdoor=temperature_by_hour(w), radiation=radiation_by_hour(w),
    )  # fmt: skip
    return {r.name: r for r in res}


def test_summer_backtest_with_solar_is_valid_and_the_optimiser_is_cheapest() -> None:
    res = run(datetime(2025, 6, 2), 10)
    imm, opt = res["run_immediately"], res["milp_optimiser"]
    assert opt.days_valid == opt.days and opt.days_fallback == 0  # type: ignore[attr-defined]
    assert opt.mean_cost_eur < imm.mean_cost_eur  # type: ignore[attr-defined]
    assert opt.mean_cost_eur < res["cheapest_start"].mean_cost_eur  # type: ignore[attr-defined]


def test_backtest_requires_radiation_for_a_solar_household() -> None:
    hh = load_household(FULL)
    pr = synthetic_prices(datetime(2025, 6, 2, tzinfo=BERLIN), 4, 1)
    w = synthetic_weather(datetime(2025, 6, 1, tzinfo=UTC), 8, 1)
    with pytest.raises(ValueError, match="radiation data is required"):
        run_appliance_backtest(
            pr, hh, default_planners(), tz=BERLIN, outdoor=temperature_by_hour(w)
        )
    with pytest.raises(ValueError, match="no radiation for"):
        run_appliance_backtest(
            pr, hh, default_planners(), tz=BERLIN, outdoor=temperature_by_hour(w), radiation={}
        )


def cli(*args: str) -> str:
    from wattmagent.appliances.__main__ import main

    buf, old = io.StringIO(), sys.argv
    sys.argv = ["wattmagent.appliances", *args]
    try:
        with contextlib.redirect_stdout(buf):
            main()
    finally:
        sys.argv = old
    return buf.getvalue()


def test_cli_prints_the_solar_assumptions_and_the_net_bill_basis(tmp_path: Path) -> None:
    out = cli("--household", str(FULL), "--synthetic", "--days", "6")
    assert "solar: 6 kWp" in out and "ASSUMED" in out and "whole net grid bill" in out
    rows = ["timestamp,price_eur_mwh"]
    for d in range(1, 4):
        for h in range(24):
            rows.append(f"2025-01-{d:02d}T{h:02d}:00:00+00:00,{60 + 15 * (h % 6)}")
    csv = tmp_path / "p.csv"
    csv.write_text("\n".join(rows), encoding="utf-8")
    fixture = ROOT / "tests" / "fixtures" / "open_meteo_archive_berlin.json"
    out2 = cli("--household", str(FULL), "--csv", str(csv), "--weather-json", str(fixture))
    assert "model yield" in out2 and "weather file:" in out2
