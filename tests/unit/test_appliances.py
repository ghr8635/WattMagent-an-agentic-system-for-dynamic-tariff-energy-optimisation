from __future__ import annotations

import itertools
import math
import random
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from wattmagent.appliances.backtest import (
    day_slices,
    default_planners,
    format_summary,
    run_appliance_backtest,
)
from wattmagent.appliances.baselines import cheapest_start, run_immediately, thermostat
from wattmagent.appliances.catalogue import (
    ConfigError,
    default_catalogue,
    parse_catalogue,
)
from wattmagent.appliances.household import (
    build_day_inputs,
    load_household,
    parse_household,
)
from wattmagent.appliances.models import (
    AppliancePlan,
    CycleJob,
    CycleSpec,
    DayInputs,
    TankSpec,
    tank_step,
)
from wattmagent.appliances.optimiser import plan_appliances
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.simulate import appliance_kw, cost_eur, place, simulate_tank
from wattmagent.appliances.verifier import ApplianceRule, verify_appliance_plan
from wattmagent.data.prices import synthetic_prices
from wattmagent.planning.household import PlanStatus
from wattmagent.safety.audit import InMemoryAuditLog
from wattmagent.safety.pipeline import Decision, PlanRejected

BERLIN = ZoneInfo("Europe/Berlin")
EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "households" / "example.toml"
WASH = CycleSpec("wash", (0.9, 0.5))
DISH = CycleSpec("dish", (0.55, 0.25, 0.25))
TANK = TankSpec("tank", 200.0, 3.0, 45.0, 60.0, 52.0, loss_kw_per_k=0.0019)
DRAW = [0, 0, 0, 0, 0, 0.5, 3, 4, 2, 1, 0.5, 0.5, 1, 0.5, 0.5, 0.5, 1, 2, 3, 3.5, 2.5, 1.5, 0.5, 0]


def draws(daily: float = 4.0) -> list[float]:
    total = sum(DRAW)
    return [daily * w / total for w in DRAW]


def random_prices(seed: int, n: int = 24) -> list[float]:
    rng = random.Random(seed)  # noqa: S311
    return [rng.uniform(-0.05, 0.40) for _ in range(n)]


# --- models ---------------------------------------------------------------------------------


def test_invalid_specs_are_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        CycleSpec("x", ())
    with pytest.raises(ValueError, match="never draws"):
        CycleSpec("x", (0.0, 0.0))
    with pytest.raises(ValueError, match="not negative"):
        CycleSpec("x", (1.0, -1.0))
    with pytest.raises(ValueError, match="shorter than"):
        CycleJob(WASH, 5, 6)
    with pytest.raises(ValueError, match="inside the comfort band"):
        TankSpec("t", 100, 2, 45, 60, 70)
    with pytest.raises(ValueError, match="below t_max"):
        TankSpec("t", 100, 2, 60, 45, 50)
    with pytest.raises(ValueError, match="unique"):
        DayInputs([0.1] * 24, jobs=[CycleJob(WASH, 0, 24), CycleJob(WASH, 0, 24)])
    with pytest.raises(ValueError, match="beyond"):
        DayInputs([0.1] * 24, jobs=[CycleJob(WASH, 0, 30)])
    with pytest.raises(ValueError, match="one value per slot"):
        DayInputs([0.1] * 24, tank=TANK)


def test_tank_step_conserves_energy() -> None:
    # 1 kWh of heat into 200 l raises it by 1 / (0.2 * 4.186/3.6 ...) K, with no loss or draw.
    spec = replace(TANK, loss_kw_per_k=0.0)
    rise = tank_step(spec, 50.0, 1.0, 0.0) - 50.0
    assert rise == pytest.approx(1.0 / (200.0 * 4.186 / 3600.0))
    assert tank_step(spec, 50.0, 0.0, 1.0) < 50.0  # a draw cools the tank
    standby = tank_step(TANK, 50.0, 0.0, 0.0)
    assert standby < 50.0  # standby loss alone cools it


# --- optimiser ------------------------------------------------------------------------------


def brute_force_cycles(inputs: DayInputs) -> float:
    """Cheapest total over every combination of starts (no grid limit)."""
    best = math.inf
    for starts in itertools.product(*(job.starts for job in inputs.jobs)):
        total = 0.0
        for job, s in zip(inputs.jobs, starts, strict=True):
            total += sum(inputs.prices[s + i] * kw for i, kw in enumerate(job.spec.profile_kw))
        best = min(best, total)
    return best


@pytest.mark.parametrize("seed", range(40))
def test_cycles_match_brute_force(seed: int) -> None:
    inputs = DayInputs(random_prices(seed), jobs=[CycleJob(WASH, 6, 20), CycleJob(DISH, 18, 24)])
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    assert plan.planned_cost_eur == pytest.approx(brute_force_cycles(inputs), abs=1e-6)
    assert cost_eur(inputs.prices, appliance_kw(inputs, plan)) == pytest.approx(
        plan.planned_cost_eur or 0.0, abs=1e-6
    )
    assert verify_appliance_plan(inputs, plan).passed


def test_cycle_runs_in_the_cheapest_window() -> None:
    prices = [0.30] * 24
    prices[13], prices[14] = 0.01, 0.01
    plan = plan_appliances(DayInputs(prices, jobs=[CycleJob(WASH, 8, 18)]))
    assert plan.cycle_kw["wash"] == place(WASH.profile_kw, 13, 24)


@pytest.mark.parametrize("seed", range(25))
def test_tank_plan_is_valid_and_not_dearer_than_the_thermostat(seed: int) -> None:
    inputs = DayInputs(random_prices(seed), tank=TANK, tank_draw_kwh=draws())
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.OPTIMAL
    report = verify_appliance_plan(inputs, plan)
    assert report.passed, report.violations
    temps = simulate_tank(inputs, plan.tank_kw)
    assert all(TANK.t_min_c - 1e-3 <= t <= TANK.t_max_c + 1e-3 for t in temps)
    assert temps[-1] >= TANK.t_initial_c - 1e-3
    base = run_immediately(inputs)
    assert not verify_appliance_plan(inputs, base, compare_with_baseline=False).violations
    assert (plan.planned_cost_eur or 0) <= cost_eur(inputs.prices, base.tank_kw) + 1e-6
    # the solver's own cost agrees with the independent simulation
    assert cost_eur(inputs.prices, appliance_kw(inputs, plan)) == pytest.approx(
        plan.planned_cost_eur or 0.0, abs=1e-6
    )


def test_modulating_tank_is_never_dearer_than_on_off() -> None:
    for seed in range(10):
        prices = random_prices(seed)
        on_off = DayInputs(prices, tank=TANK, tank_draw_kwh=draws())
        modulating = DayInputs(prices, tank=replace(TANK, modulating=True), tank_draw_kwh=draws())
        a, b = plan_appliances(on_off), plan_appliances(modulating)
        assert (b.planned_cost_eur or 0) <= (a.planned_cost_eur or 0) + 1e-6
        assert verify_appliance_plan(modulating, b).passed


def test_tank_moves_its_heating_to_a_cheap_hour_when_the_band_allows() -> None:
    # The 15 K band stores only about 3.5 kWh, so heating can move by an hour or two, not by
    # half a day: with hour 7 cheap the morning heating moves from 8 to 7.
    prices = [0.30] * 24
    prices[7] = 0.02
    plan = plan_appliances(DayInputs(prices, tank=TANK, tank_draw_kwh=draws()))
    assert plan.tank_kw[7] > 0 and plan.tank_kw[8] == 0


def test_grid_limit_forces_cycles_apart() -> None:
    prices = [0.30] * 24
    for t in (12, 13):
        prices[t] = 0.01  # both cycles would love these two hours
    jobs = [
        CycleJob(CycleSpec("a", (2.0, 2.0)), 8, 18),
        CycleJob(CycleSpec("b", (2.0, 2.0)), 8, 18),
    ]
    free = plan_appliances(DayInputs(prices, jobs=jobs))
    limited = plan_appliances(DayInputs(prices, jobs=jobs, max_grid_kw=3.0))
    assert max(appliance_kw(DayInputs(prices, jobs=jobs), free)) == pytest.approx(4.0)
    assert max(appliance_kw(DayInputs(prices, jobs=jobs), limited)) <= 3.0 + 1e-6
    assert (limited.planned_cost_eur or 0) > (free.planned_cost_eur or 0)


def test_base_load_counts_against_the_limit() -> None:
    inputs = DayInputs(
        [0.1] * 24, jobs=[CycleJob(WASH, 0, 24)], base_load_kw=[2.0] * 24, max_grid_kw=2.5
    )
    plan = plan_appliances(inputs)  # wash needs 0.9 kW: 2.9 > 2.5, impossible in any slot
    assert plan.status is PlanStatus.INFEASIBLE


def test_unreachable_comfort_is_reported_infeasible() -> None:
    inputs = DayInputs([0.1] * 24, tank=TANK, tank_draw_kwh=draws(60.0))  # far more than 3 kW
    plan = plan_appliances(inputs)
    assert plan.status is PlanStatus.INFEASIBLE
    assert plan.message


def test_empty_day_costs_nothing() -> None:
    plan = plan_appliances(DayInputs([0.1] * 24))
    assert plan.status is PlanStatus.OPTIMAL and plan.planned_cost_eur == 0.0


# --- baselines ------------------------------------------------------------------------------


def test_baselines_order_and_validity() -> None:
    inputs = DayInputs(
        random_prices(3),
        jobs=[CycleJob(WASH, 6, 20), CycleJob(DISH, 18, 24)],
        tank=TANK,
        tank_draw_kwh=draws(),
    )
    imm, cheap, opt = run_immediately(inputs), cheapest_start(inputs), plan_appliances(inputs)
    for p in (imm, cheap, opt):
        assert verify_appliance_plan(inputs, p, compare_with_baseline=False).passed
    cost = [cost_eur(inputs.prices, appliance_kw(inputs, p)) for p in (imm, cheap, opt)]
    assert cost[2] <= cost[1] + 1e-9 <= cost[0] + 1e-9
    assert imm.cycle_kw["wash"] == place(WASH.profile_kw, 6, 24)


def test_thermostat_without_tank_is_empty_and_modulating_one_stays_valid() -> None:
    assert thermostat(DayInputs([0.1] * 24)) == []
    inputs = DayInputs([0.1] * 24, tank=replace(TANK, modulating=True), tank_draw_kwh=draws())
    assert verify_appliance_plan(
        inputs, run_immediately(inputs), compare_with_baseline=False
    ).passed


# --- verifier: deliberately broken plans -----------------------------------------------------


def day() -> DayInputs:
    return DayInputs(
        random_prices(1),
        jobs=[CycleJob(WASH, 8, 18)],
        tank=TANK,
        tank_draw_kwh=draws(),
        base_load_kw=[1.0] * 24,
        max_grid_kw=6.0,
    )


def good() -> AppliancePlan:
    return plan_appliances(day())


def rules(plan: AppliancePlan) -> set[str]:
    return set(verify_appliance_plan(day(), plan).rules)


def test_good_plan_passes() -> None:
    assert verify_appliance_plan(day(), good()).passed


def test_attack_cycle_shifted_outside_window() -> None:
    plan = replace(good(), cycle_kw={"wash": place(WASH.profile_kw, 20, 24)})
    assert ApplianceRule.CYCLE_WINDOW.value in rules(plan)


def test_attack_cycle_split_in_two() -> None:
    series = [0.0] * 24
    series[9], series[12] = 0.9, 0.5  # paused between the two hours
    assert ApplianceRule.CYCLE_SHAPE.value in rules(replace(good(), cycle_kw={"wash": series}))


def test_attack_cycle_missing_scaled_or_twice() -> None:
    assert ApplianceRule.CYCLE_SHAPE.value in rules(replace(good(), cycle_kw={"wash": [0.0] * 24}))
    half = [v / 2 for v in place(WASH.profile_kw, 9, 24)]
    assert ApplianceRule.CYCLE_SHAPE.value in rules(replace(good(), cycle_kw={"wash": half}))
    twice = [
        a + b
        for a, b in zip(place(WASH.profile_kw, 9, 24), place(WASH.profile_kw, 14, 24), strict=True)
    ]
    assert ApplianceRule.CYCLE_SHAPE.value in rules(replace(good(), cycle_kw={"wash": twice}))


def test_attack_wrong_shape_unknown_job_nan() -> None:
    assert ApplianceRule.SHAPE.value in rules(replace(good(), cycle_kw={}))
    assert ApplianceRule.SHAPE.value in rules(replace(good(), cycle_kw={"ghost": [0.0] * 24}))
    assert ApplianceRule.SHAPE.value in rules(replace(good(), tank_kw=[0.0] * 5))
    nan = good().tank_kw[:]
    nan[0] = float("nan")
    assert ApplianceRule.NOT_FINITE.value in rules(replace(good(), tank_kw=nan))


def test_attack_tank_too_hot_too_cold_and_ending_cold() -> None:
    always_on = replace(good(), tank_kw=[3.0] * 24)
    assert ApplianceRule.TANK_COMFORT.value in rules(always_on)
    never = replace(good(), tank_kw=[0.0] * 24)
    found = rules(never)
    assert ApplianceRule.TANK_COMFORT.value in found
    ends_cold = good().tank_kw[:]
    last = max(i for i, kw in enumerate(ends_cold) if kw > 0)
    ends_cold[last] = 0.0  # skip the last heating: the tank ends colder than it began
    cold = verify_appliance_plan(day(), replace(good(), tank_kw=ends_cold))
    assert {"tank_end", "tank_comfort"} & set(cold.rules)


def test_attack_tank_power_limits() -> None:
    over = good().tank_kw[:]
    over[5] = 99.0
    assert ApplianceRule.TANK_POWER.value in rules(replace(good(), tank_kw=over))
    partial = good().tank_kw[:]
    partial[5] = 1.0  # an on/off element cannot do 1 kW
    assert ApplianceRule.TANK_POWER.value in rules(replace(good(), tank_kw=partial))


def test_attack_grid_limit() -> None:
    inputs = replace(day(), max_grid_kw=1.5)  # base 1.0 + element 3.0 is far above
    plan = replace(good(), tank_kw=[3.0] * 24)
    assert ApplianceRule.GRID_LIMIT.value in verify_appliance_plan(inputs, plan).rules


def test_attack_plan_dearer_than_baseline() -> None:
    inputs = DayInputs([0.05] * 6 + [0.50] * 18, jobs=[CycleJob(WASH, 0, 24)])
    worst = replace(run_immediately(inputs), cycle_kw={"wash": place(WASH.profile_kw, 22, 24)})
    assert ApplianceRule.COST.value in verify_appliance_plan(inputs, worst).rules


def test_attack_no_schedule_and_tank_power_without_tank() -> None:
    assert ApplianceRule.NO_SCHEDULE.value in rules(AppliancePlan(PlanStatus.INFEASIBLE))
    inputs = DayInputs([0.1] * 24, jobs=[CycleJob(WASH, 0, 24)])
    plan = replace(run_immediately(inputs), tank_kw=[1.0] * 24)
    assert ApplianceRule.SHAPE.value in verify_appliance_plan(inputs, plan).rules


# --- pipeline -------------------------------------------------------------------------------


def test_pipeline_uses_the_optimiser_and_audits_once() -> None:
    audit = InMemoryAuditLog()
    out = plan_appliances_safely(day(), audit=audit, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert out.decision is Decision.OPTIMISED and out.request_satisfied
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry["kind"] == "appliances" and entry["inputs"]["cycles"] == 1
    assert "wash" not in str(entry)  # appliance names stay out of the audit log


def test_pipeline_falls_back_when_the_planner_breaks_or_lies() -> None:
    def crash(_: DayInputs) -> AppliancePlan:
        raise RuntimeError("boom")

    def liar(inputs: DayInputs) -> AppliancePlan:
        return replace(run_immediately(inputs), tank_kw=[3.0] * inputs.slots)

    for planner, reason in ((crash, "planner_error"), (liar, "verifier_rejected")):
        audit = InMemoryAuditLog()
        out = plan_appliances_safely(day(), planner=planner, audit=audit)
        assert out.decision is Decision.FALLBACK
        assert out.reasons[0].startswith(reason)
        assert out.report.passed and len(audit.entries) == 1


def test_pipeline_reports_an_unreachable_comfort_band() -> None:
    inputs = DayInputs([0.1] * 24, tank=TANK, tank_draw_kwh=draws(60.0))
    out = plan_appliances_safely(inputs)
    assert out.decision is Decision.FALLBACK and not out.request_satisfied
    assert out.reasons[0].startswith("infeasible_request")


def test_pipeline_refuses_when_even_the_fallback_breaks_a_hard_limit() -> None:
    inputs = DayInputs(
        [0.1] * 24,
        jobs=[CycleJob(CycleSpec("a", (5.0,)), 0, 24), CycleJob(CycleSpec("b", (5.0,)), 0, 1)],
        max_grid_kw=6.0,
    )

    # run_immediately starts both at slot 0 (10 kW > 6 kW); the optimiser could separate them
    # but a broken planner forces the fallback.
    def crash(_: DayInputs) -> AppliancePlan:
        raise RuntimeError("x")

    audit = InMemoryAuditLog()
    with pytest.raises(PlanRejected):
        plan_appliances_safely(inputs, planner=crash, audit=audit)
    assert audit.entries[0]["outcome"] == "rejected"


# --- catalogue and household config ----------------------------------------------------------


def test_shipped_catalogue_is_complete_and_sane() -> None:
    cat = default_catalogue()
    assert {"washing_machine", "dishwasher", "tumble_dryer_heat_pump"} <= set(cat.cycles)
    assert {"water_heater_200l", "water_heater_80l"} <= set(cat.tanks)
    for c in cat.cycles.values():
        assert c.description and CycleSpec(c.key, c.profile_kw)
    for t in cat.tanks.values():
        assert t.description and sum(t.draw_shape) > 0
        spec = TankSpec(t.key, t.volume_l, t.power_kw, t.t_min_c, t.t_max_c, t.t_initial_c)
        # a day's draw can in principle be replaced by the element
        assert t.daily_draw_kwh < spec.power_kw * 24


def test_example_household_loads_and_plans() -> None:
    hh = load_household(EXAMPLE)
    assert hh.name == "Example flat" and len(hh.cycles) == 3 and hh.tank is not None
    inputs = build_day_inputs(hh, [0.2] * 24, list(range(24)))
    assert [j.earliest for j in inputs.jobs] == [8, 19, 9]
    assert inputs.jobs[1].deadline == 24
    out = plan_appliances_safely(inputs)
    assert out.decision is Decision.OPTIMISED and out.report.passed


def test_adding_an_appliance_is_a_config_entry() -> None:
    raw = {
        "catalogue": {"cycle": {"my_washer": {"profile_kw": [1.0, 0.6], "description": "mine"}}},
        "cycle": [{"appliance": "my_washer", "earliest_hour": 6, "deadline_hour": 12}],
    }
    hh = parse_household(raw)
    assert hh.cycles[0].model.profile_kw == (1.0, 0.6)
    assert "my_washer" not in default_catalogue().cycles  # the shipped catalogue is untouched


def test_household_overrides_and_repeated_appliances() -> None:
    raw = {
        "cycle": [
            {"appliance": "dishwasher", "name": "dish_a", "earliest_hour": 6, "deadline_hour": 12},
            {"appliance": "dishwasher", "name": "dish_b", "profile_kw": [2.0], "deadline_hour": 20},
        ],
        "tank": {"appliance": "water_heater_80l", "daily_draw_kwh": 1.0, "modulating": True},
    }
    hh = parse_household(raw)
    assert [e.name for e in hh.cycles] == ["dish_a", "dish_b"]
    assert hh.cycles[1].model.profile_kw == (2.0,)
    assert hh.tank is not None and hh.tank.modulating and hh.tank.daily_draw_kwh == 1.0


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ({"colour": 1}, "unknown key"),
        ({"timezone": "Mars/Base"}, "unknown time zone"),
        ({"max_grid_kw": -1}, "positive"),
        ({"daily_base_load_kwh": "x"}, ">= 0"),
        ({"cycle": [{"appliance": "toaster"}]}, "unknown appliance"),
        ({"cycle": [{"appliance": "dishwasher", "typo": 1}]}, "unknown key"),
        ({"cycle": [{"appliance": "dishwasher", "earliest_hour": 25}]}, "0 to 24"),
        (
            {"cycle": [{"appliance": "dishwasher", "earliest_hour": 10, "deadline_hour": 11}]},
            "shorter",
        ),
        ({"cycle": [{"appliance": "dishwasher"}, {"appliance": "dishwasher"}]}, "unique"),
        ({"cycle": [{"appliance": "dishwasher", "profile_kw": [0, 0]}]}, "not all zero"),
        ({"cycle": ["x"]}, "expected a table"),
        ({"tank": {"appliance": "boiler"}}, "unknown appliance"),
        ({"tank": {"appliance": "water_heater_80l", "t_initial_c": 90}}, "comfort band"),
        ({"tank": {"appliance": "water_heater_80l", "bogus": 1}}, "unknown key"),
        ({"tank": {"appliance": "water_heater_80l", "draw_shape": [1, 2]}}, "24 values"),
        ({"tank": {"appliance": "water_heater_80l", "modulating": "yes"}}, "true or false"),
        ({"catalogue": {"cycle": {"x": {"profile_kw": "abc"}}}}, "list of numbers"),
        ({"catalogue": {"cycle": {"x": {}}}}, "required"),
        ({"catalogue": {"tank": {"x": {"volume_l": 1}}}}, "missing"),
        ({"catalogue": {"gadget": {}}}, "unknown key"),
    ],
)
def test_bad_household_files_fail_with_a_clear_message(raw: dict[str, object], text: str) -> None:
    with pytest.raises(ConfigError, match=text):
        parse_household(raw)


def test_bad_toml_names_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "home.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"home\.toml"):
        load_household(bad)
    unknown = tmp_path / "home2.toml"
    unknown.write_text('[[cycle]]\nappliance = "toaster"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"home2\.toml"):
        load_household(unknown)


def test_parse_catalogue_replaces_entries() -> None:
    base = default_catalogue()
    changed = parse_catalogue({"cycle": {"dishwasher": {"profile_kw": [1.0]}}}, base)
    assert changed.cycles["dishwasher"].profile_kw == (1.0,)
    assert base.cycles["dishwasher"].profile_kw != (1.0,)


def test_build_day_inputs_handles_short_and_long_days() -> None:
    hh = parse_household(
        {
            "cycle": [{"appliance": "dishwasher", "earliest_hour": 19, "deadline_hour": 24}],
            "tank": {"appliance": "water_heater_200l"},
        }
    )
    short = [h for h in range(24) if h != 2]  # 23 h day: local hour 2 does not exist
    inputs = build_day_inputs(hh, [0.2] * 23, short)
    assert inputs.slots == 23 and inputs.jobs[0].deadline == 23
    long = [*range(3), 2, *range(3, 24)]  # 25 h day: hour 2 occurs twice
    assert build_day_inputs(hh, [0.2] * 25, long).slots == 25
    assert sum(build_day_inputs(hh, [0.2] * 24, list(range(24))).tank_draw_kwh) == pytest.approx(
        4.0
    )
    with pytest.raises(ValueError, match="same length"):
        build_day_inputs(hh, [0.2] * 3, [0, 1])


# --- backtest -------------------------------------------------------------------------------


def test_backtest_on_synthetic_prices_ranks_strategies_and_stays_valid() -> None:
    hh = load_household(EXAMPLE)
    prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=BERLIN), 20, seed=5)
    audit = InMemoryAuditLog()
    res = {
        r.name: r for r in run_appliance_backtest(prices, hh, default_planners(audit), tz=BERLIN)
    }
    imm, cheap, opt = res["run_immediately"], res["cheapest_start"], res["milp_optimiser"]
    assert imm.days == cheap.days == opt.days >= 18
    assert opt.mean_cost_eur <= cheap.mean_cost_eur + 1e-9 <= imm.mean_cost_eur + 1e-9
    assert opt.days_valid == opt.days and opt.days_fallback == 0
    assert len(audit.entries) == opt.days
    text = format_summary(list(res.values()), baseline="run_immediately")
    assert "milp_optimiser" in text and "%" in text


def test_backtest_rejects_gaps_and_short_series() -> None:
    hh = load_household(EXAMPLE)
    prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=BERLIN), 3, seed=1)
    with pytest.raises(ValueError, match="gap-free"):
        run_appliance_backtest([prices[0], *prices[2:]], hh, default_planners(), tz=BERLIN)
    with pytest.raises(ValueError, match="too short"):
        run_appliance_backtest(prices[:10], hh, default_planners(), tz=BERLIN)


def test_day_slices_cover_daylight_saving_days() -> None:
    prices = synthetic_prices(datetime(2024, 3, 25, tzinfo=UTC), 10, seed=1)  # spans 31 Mar
    lengths = {sl.stop - sl.start for sl in day_slices(prices, BERLIN)}
    assert lengths == {23, 24}
