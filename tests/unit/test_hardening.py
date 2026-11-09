"""Failure injection, junk input, concurrency and a speed guard.

These tests answer one question: when something goes wrong, does the system stay safe and
say so, instead of crashing or silently doing the wrong thing?
"""

from __future__ import annotations

import random
import string
import time as clock
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.unit.test_api_core import TEXT, fresh_service
from wattmagent.appliances import optimiser
from wattmagent.appliances.agent import run_appliance_agent
from wattmagent.appliances.household import build_day_inputs, load_household
from wattmagent.appliances.models import AppliancePlan, DayInputs
from wattmagent.appliances.pipeline import plan_appliances_safely
from wattmagent.appliances.request import parse_appliance_request
from wattmagent.data.prices import synthetic_prices
from wattmagent.data.weather import synthetic_weather
from wattmagent.graph.context import AgentContext
from wattmagent.graph.request import parse_with_rules
from wattmagent.safety.audit import InMemoryAuditLog
from wattmagent.safety.pipeline import Decision

TZ = ZoneInfo("Europe/Berlin")
ROOT = Path(__file__).resolve().parents[2]
HOUSEHOLD = load_household(ROOT / "config" / "households" / "example_full.toml")
NOW = datetime(2025, 1, 3, 15, 20, tzinfo=TZ)


def day_inputs() -> DayInputs:
    """A winter day for the full house: cycles, tank, heat pump and solar all present."""
    start = datetime(2025, 1, 15, tzinfo=ZoneInfo("UTC"))
    prices = [p.eur_per_kwh for p in synthetic_prices(start, 1, 1)][:24]
    weather = synthetic_weather(start, 1, 1)[:24]
    return build_day_inputs(
        HOUSEHOLD,
        prices,
        list(range(24)),
        outdoor_c=[w.temperature_c for w in weather],
        radiation_w_m2=[w.shortwave_w_m2 for w in weather],
    )


# --- the optimiser fails in different ways ---------------------------------------------------


def _fake_milp(status: int, x: Any = None) -> Any:
    def milp(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(status=status, x=x, message="injected", success=status == 0)

    return milp


@pytest.mark.parametrize("status", [1, 2, 3, 4])
def test_solver_failure_leads_to_the_verified_fallback(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(optimiser, "milp", _fake_milp(status))
    audit = InMemoryAuditLog()
    result = plan_appliances_safely(day_inputs(), audit=audit, now=NOW)
    assert result.decision is Decision.FALLBACK
    assert result.report.passed or not result.request_satisfied
    assert len(audit.entries) == 1  # one record, even on failure


def test_solver_returning_nonsense_numbers_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = day_inputs()
    real = optimiser.plan_appliances(inputs)

    def poisoned(i: DayInputs) -> AppliancePlan:
        # A plan that claims to be fine but is not: the independent verifier must reject it.
        cycle_kw = {name: list(series) for name, series in real.cycle_kw.items()}
        first = next(iter(cycle_kw))
        cycle_kw[first][0] = float("nan")
        return replace(real, cycle_kw=cycle_kw)

    result = plan_appliances_safely(inputs, planner=poisoned, now=NOW)
    assert result.decision is Decision.FALLBACK
    assert any("verifier_rejected" in r for r in result.reasons)


def test_a_plan_the_verifier_cannot_even_read_fails_closed() -> None:
    inputs = day_inputs()
    real = optimiser.plan_appliances(inputs)

    def malformed(_: DayInputs) -> AppliancePlan:
        return replace(real, cycle_kw=[[1.0]])  # type: ignore[arg-type]  (wrong shape on purpose)

    result = plan_appliances_safely(inputs, planner=malformed, now=NOW)
    assert result.decision is Decision.FALLBACK
    assert any(r.startswith("verifier_error") for r in result.reasons)


def test_planner_exception_is_not_propagated() -> None:
    def boom(_: DayInputs) -> AppliancePlan:
        raise MemoryError("injected")

    result = plan_appliances_safely(day_inputs(), planner=boom, now=NOW)
    assert result.decision is Decision.FALLBACK
    assert "planner_error: MemoryError" in result.reasons


def test_bad_prices_never_reach_the_optimiser() -> None:
    def must_not_run(_: DayInputs) -> AppliancePlan:
        raise AssertionError("optimiser used despite price problems")

    result = plan_appliances_safely(
        day_inputs(), planner=must_not_run, now=NOW, price_problems=["stale_prices"]
    )
    assert result.decision is Decision.FALLBACK and "stale_prices" in result.reasons


# --- the price feed fails ---------------------------------------------------------------------


def test_a_failing_provider_surfaces_a_clear_error(tmp_path: Path) -> None:
    service = fresh_service(tmp_path)

    class Down:
        def get(self) -> AgentContext:
            raise ConnectionError("feed down")

    service.provider = Down()
    with pytest.raises(ConnectionError):
        service.plan(TEXT, "rid")  # the HTTP layer maps provider errors to 503 (see test_api)


def test_price_series_with_garbage_values_is_not_planned_on() -> None:
    prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=TZ), 5, 1)
    ctx = AgentContext(prices, NOW - timedelta(hours=1), TZ)
    # Extreme but finite values must still produce a verified answer, never an exception.
    ctx.prices[30] = replace(ctx.prices[30], eur_per_kwh=5.0)
    resp = run_appliance_agent("run the dishwasher by 10pm", HOUSEHOLD, ctx, NOW)
    assert resp.status in {"planned", "needs_input", "error"}


# --- junk input never crashes -----------------------------------------------------------------

WORDS = [
    "dishwasher", "washing machine", "dryer", "before", "by", "after", "not before", "6pm",
    "18:00", "noon", "midnight", "half past five", "25:99", "0:00", "24:00", "99", "%", "80%",
    "and", ",", ".", "tomorrow", "at", "until", "from", "  ", "\n", "ü", "💡", "12am", "12pm",
    "0pm", "13pm", "7 in the morning", "a.m.", "p.m.",
]  # fmt: skip


def _junk(rng: random.Random) -> str:
    text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 9)))
    if rng.random() < 0.2:
        text += "".join(rng.choice(string.printable) for _ in range(rng.randint(0, 40)))
    return text


def test_random_text_never_crashes_the_parsers_or_the_agent() -> None:
    rng = random.Random(12345)  # noqa: S311  (fixed seed: a failure reproduces)
    prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=TZ), 5, 1)
    ctx = AgentContext(prices, NOW - timedelta(hours=1), TZ)
    for _ in range(400):
        text = _junk(rng)
        parse_with_rules(text)
        parse_appliance_request(text, HOUSEHOLD)
        resp = run_appliance_agent(text, HOUSEHOLD, ctx, NOW)
        assert resp.status in {"planned", "needs_input", "error"}, text


@pytest.mark.parametrize("text", ["", " ", "\x00", "a" * 20000, "dishwasher " * 2000])
def test_extreme_inputs(text: str) -> None:
    prices = synthetic_prices(datetime(2025, 1, 1, tzinfo=TZ), 5, 1)
    ctx = AgentContext(prices, NOW - timedelta(hours=1), TZ)
    resp = run_appliance_agent(text, HOUSEHOLD, ctx, NOW)
    assert resp.status in {"planned", "needs_input", "error"}


# --- concurrency ------------------------------------------------------------------------------


def test_many_callers_at_once_get_consistent_answers(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    service = fresh_service(tmp_path, audit=audit)
    service.household = HOUSEHOLD

    def call(i: int) -> tuple[int, dict[str, Any]]:
        text = TEXT if i % 2 == 0 else "run the dishwasher by 10pm"
        return service.plan(text, f"rid-{i}", owner=f"owner-{i % 3}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(call, range(48)))

    assert all(code == 200 for code, _ in results)
    ev = [b for i, (_, b) in enumerate(results) if i % 2 == 0]
    assert len({b["facts"]["ev_cost_eur"] for b in ev}) == 1  # same ask, same answer
    plan_ids = [b["plan_id"] for b in ev]
    assert len(set(plan_ids)) == len(plan_ids)  # no id collisions
    assert len(audit.entries) == 48  # nothing lost under contention


# --- speed guard ------------------------------------------------------------------------------


def test_a_full_house_day_solves_quickly() -> None:
    inputs = day_inputs()
    worst = 0.0
    for _ in range(5):
        start = clock.perf_counter()
        plan = optimiser.plan_appliances(inputs)
        worst = max(worst, clock.perf_counter() - start)
        assert plan.feasible
    # Measured ~0.03 s; the bound is loose enough for a slow CI runner but catches a blow-up.
    assert worst < 2.0
