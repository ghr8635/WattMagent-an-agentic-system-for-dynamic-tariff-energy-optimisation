"""Small shared types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Usage:
    """Token and call accounting for one agent request."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cached_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
