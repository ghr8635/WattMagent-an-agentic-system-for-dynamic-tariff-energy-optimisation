from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from wattmagent.core.errors import (
    AuthError,
    BudgetExceeded,
    CreditsExhausted,
    ModelUnavailable,
    ProviderUnavailable,
)
from wattmagent.core.types import Usage
from wattmagent.llm.budget import TokenBudget
from wattmagent.llm.cache import DiskCache
from wattmagent.llm.client import LLMClient, Message


def ok_response(text: str = "hi", pt: int = 10, ct: int = 5) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }


class FakeTransport:
    """Scripted transport: each model maps to a response dict or an exception."""

    def __init__(self, script: dict[str, object]) -> None:
        self.script = script
        self.calls: list[str] = []

    def chat(self, model: str, messages: list[Message], max_tokens: int) -> dict[str, object]:
        self.calls.append(model)
        result = self.script[model]
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[return-value]


MSG: list[Message] = [{"role": "user", "content": "What is Art. 17 GDPR?"}]


def make_client(script, models, cache=None, limit=100_000):
    t = FakeTransport(script)
    return t, LLMClient(t, models, TokenBudget(limit), cache=cache, max_tokens=50)


def test_fallback_to_second_model() -> None:
    t, c = make_client({"a": ModelUnavailable("down"), "b": ok_response("from b")}, ["a", "b"])
    u = Usage()
    comp = c.complete(MSG, u)
    assert comp.text == "from b" and comp.model == "b"
    assert t.calls == ["a", "b"]
    assert u.llm_calls == 1 and u.prompt_tokens == 10


def test_credits_exhausted_stops_chain() -> None:
    t, c = make_client({"a": CreditsExhausted("402"), "b": ok_response()}, ["a", "b"])
    try:
        c.complete(MSG)
        raise AssertionError("expected CreditsExhausted")
    except CreditsExhausted:
        pass
    assert t.calls == ["a"]  # b was never tried


def test_auth_error_stops_chain() -> None:
    t, c = make_client({"a": AuthError("401"), "b": ok_response()}, ["a", "b"])
    try:
        c.complete(MSG)
        raise AssertionError("expected AuthError")
    except AuthError:
        pass
    assert t.calls == ["a"]


def test_all_models_fail() -> None:
    _, c = make_client({"a": ModelUnavailable("x"), "b": ModelUnavailable("y")}, ["a", "b"])
    try:
        c.complete(MSG)
        raise AssertionError("expected ProviderUnavailable")
    except ProviderUnavailable:
        pass


def test_malformed_response_falls_back() -> None:
    t, c = make_client({"a": {"oops": 1}, "b": ok_response("fine")}, ["a", "b"])
    assert c.complete(MSG).text == "fine"
    assert t.calls == ["a", "b"]


def test_cache_hit_skips_network() -> None:
    with tempfile.TemporaryDirectory() as d:
        cache = DiskCache(Path(d))
        t, c = make_client({"a": ok_response("answer")}, ["a"], cache=cache)
        u = Usage()
        first = c.complete(MSG, u)
        second = c.complete(MSG, u)
        assert first.text == second.text == "answer"
        assert second.cached and not first.cached
        assert t.calls == ["a"]  # network hit exactly once
        assert u.llm_calls == 1 and u.cached_calls == 1


def test_budget_blocks_call_before_network() -> None:
    t, c = make_client({"a": ok_response()}, ["a"], limit=10)
    try:
        c.complete(MSG)
        raise AssertionError("expected BudgetExceeded")
    except BudgetExceeded:
        pass
    assert t.calls == []


def test_budget_rolls_over_at_midnight() -> None:
    today = [date(2026, 1, 1)]
    b = TokenBudget(100, today=lambda: today[0])
    b.record(100)
    assert b.remaining == 0
    today[0] += timedelta(days=1)
    assert b.remaining == 100
