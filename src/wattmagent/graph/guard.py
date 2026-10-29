"""Stop calling the model once it is clearly unusable for this request.

Out of credits, a rejected token or an exhausted token budget will not fix themselves
between the parse step and the explain step, so the second call would only add delay
and log noise. The guard remembers the first such failure and re-raises it without
touching the network. A new guard is made for every request.
"""

from __future__ import annotations

from wattmagent.core.errors import AuthError, BudgetExceeded, CreditsExhausted
from wattmagent.core.types import Usage
from wattmagent.graph.request import Completer
from wattmagent.llm.client import Completion, Message


class GuardedCompleter:
    def __init__(self, inner: Completer) -> None:
        self._inner = inner
        self._failure: CreditsExhausted | AuthError | BudgetExceeded | None = None

    def complete(self, messages: list[Message], usage: Usage | None = None) -> Completion:
        if self._failure is not None:
            raise self._failure
        try:
            return self._inner.complete(messages, usage)
        except (CreditsExhausted, AuthError, BudgetExceeded) as exc:
            self._failure = exc
            raise
