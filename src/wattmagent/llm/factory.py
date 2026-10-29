"""Building the real model client from settings, and checking which models answer."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from wattmagent.core.errors import WattMagentError
from wattmagent.llm.budget import TokenBudget
from wattmagent.llm.cache import DiskCache
from wattmagent.llm.client import HttpxTransport, LLMClient

if TYPE_CHECKING:
    from wattmagent.core.config import Settings

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def resolve_token(base_url: str, token: str) -> str:
    """The token to send. A local server (for example Ollama) needs none, a remote one does."""
    if token:
        return token
    if urlparse(base_url).hostname in _LOCAL_HOSTS:
        return "local"
    raise WattMagentError("WATTMAGENT_HF_TOKEN is not set")


def build_client(settings: Settings) -> LLMClient:
    token = resolve_token(settings.hf_base_url, settings.hf_token.get_secret_value())
    transport = HttpxTransport(settings.hf_base_url, token, settings.request_timeout_s)
    return LLMClient(
        transport,
        settings.model_chain,
        TokenBudget(settings.daily_token_budget),
        cache=DiskCache(settings.cache_dir),
        max_tokens=settings.max_output_tokens,
    )


def check_models(settings: Settings) -> list[tuple[str, bool, str]]:
    """Ask every configured model one trivial question. Returns (model, ok, detail).

    The token is never printed. Each model is tried alone, without cache, so the result
    says whether *that* model answers on your token right now.
    """
    token = resolve_token(settings.hf_base_url, settings.hf_token.get_secret_value())
    out: list[tuple[str, bool, str]] = []
    for model in settings.model_chain:
        transport = HttpxTransport(settings.hf_base_url, token, settings.request_timeout_s)
        client = LLMClient(
            transport, [model], TokenBudget(settings.daily_token_budget), max_tokens=8
        )
        started = time.monotonic()
        try:
            reply = client.complete([{"role": "user", "content": "Reply with the single word: ok"}])
        except WattMagentError as exc:
            out.append((model, False, f"{type(exc).__name__}: {exc}"))
        else:
            out.append(
                (
                    model,
                    True,
                    f"{time.monotonic() - started:.1f}s, replied {reply.text.strip()[:20]!r}",
                )
            )
    return out
