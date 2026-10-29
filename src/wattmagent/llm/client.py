"""LLM client for Hugging Face Inference Providers (OpenAI-compatible router).

Design:
- ``Transport`` is the only thing that touches the network; tests inject a fake.
- Ordered model fallback chain; 402 (credits) and 401 (auth) stop the chain
  immediately because other models on the same token would fail the same way.
- Exact-match disk cache and a daily token budget guard the free tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from wattmagent.core.errors import (
    AuthError,
    CreditsExhausted,
    LLMError,
    ModelUnavailable,
    ProviderUnavailable,
)
from wattmagent.core.types import Usage
from wattmagent.llm.budget import TokenBudget
from wattmagent.llm.cache import DiskCache, make_key

log = logging.getLogger(__name__)

Message = dict[str, str]


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached: bool = False


class Transport(Protocol):
    def chat(self, model: str, messages: list[Message], max_tokens: int) -> dict[str, object]:
        """Return the raw OpenAI-style response dict. Raise LLMError subclasses on failure."""
        ...


class HttpxTransport:
    """Real transport. httpx is imported lazily so unit tests do not need it."""

    def __init__(self, base_url: str, token: str, timeout_s: float) -> None:
        import httpx

        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {token}"},
        )

    def chat(self, model: str, messages: list[Message], max_tokens: int) -> dict[str, object]:
        import httpx

        try:
            r = self._client.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "temperature": 0.0,
                },
            )
        except httpx.TimeoutException as e:
            raise ModelUnavailable(f"{model}: timeout") from e
        except httpx.HTTPError as e:
            raise ModelUnavailable(f"{model}: {type(e).__name__}") from e
        if r.status_code == 402:
            raise CreditsExhausted("provider credits exhausted")
        if r.status_code in (401, 403):
            raise AuthError("token missing or rejected")
        if r.status_code in (400, 404, 408, 429) or r.status_code >= 500:
            raise ModelUnavailable(f"{model}: HTTP {r.status_code} {_short(r.text)}".strip())
        if r.status_code != 200:
            raise LLMError(f"{model}: unexpected HTTP {r.status_code}")
        data: dict[str, object] = r.json()
        return data


def _short(text: str, limit: int = 160) -> str:
    """One-line, length-limited excerpt of a provider's error body, for diagnostics."""
    flat = " ".join(text.split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _estimate_tokens(messages: list[Message], max_tokens: int) -> int:
    chars = sum(len(m["content"]) for m in messages)
    return chars // 3 + max_tokens  # deliberately pessimistic


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse(model: str, data: dict[str, object]) -> Completion:
    """Validate the provider's JSON structurally; anything unexpected means 'try next model'."""
    choices = data.get("choices")
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ModelUnavailable(f"{model}: malformed response")
    usage = data.get("usage")
    counts = usage if isinstance(usage, dict) else {}
    return Completion(
        text=content,
        model=model,
        prompt_tokens=_as_int(counts.get("prompt_tokens")),
        completion_tokens=_as_int(counts.get("completion_tokens")),
    )


class LLMClient:
    def __init__(
        self,
        transport: Transport,
        models: list[str],
        budget: TokenBudget,
        cache: DiskCache | None = None,
        max_tokens: int = 400,
    ) -> None:
        if not models:
            raise ValueError("at least one model required")
        self._t = transport
        self._models = models
        self._budget = budget
        self._cache = cache
        self._max_tokens = max_tokens

    def complete(self, messages: list[Message], usage: Usage | None = None) -> Completion:
        usage = usage if usage is not None else Usage()
        last_err: Exception | None = None
        for model in self._models:
            key = make_key(model, messages, self._max_tokens)
            if self._cache and (hit := self._cache.get(key)):
                usage.cached_calls += 1
                return Completion(
                    text=str(hit["text"]),
                    model=model,
                    prompt_tokens=0,
                    completion_tokens=0,
                    cached=True,
                )
            self._budget.check(_estimate_tokens(messages, self._max_tokens))
            try:
                comp = _parse(model, self._t.chat(model, messages, self._max_tokens))
            except (CreditsExhausted, AuthError):
                raise  # same token everywhere: do not burn the chain
            except ModelUnavailable as e:
                log.warning("model failed, trying next", extra={"msg_model": model})
                last_err = e
                continue
            self._budget.record(comp.prompt_tokens + comp.completion_tokens)
            usage.prompt_tokens += comp.prompt_tokens
            usage.completion_tokens += comp.completion_tokens
            usage.llm_calls += 1
            if self._cache:
                self._cache.set(key, {"text": comp.text})
            return comp
        raise ProviderUnavailable(f"all models failed: {last_err}")
