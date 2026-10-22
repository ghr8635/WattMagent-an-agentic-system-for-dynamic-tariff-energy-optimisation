"""Daily token budget so a bug or a burst can never drain the free tier."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date

from wattmagent.core.errors import BudgetExceeded


class TokenBudget:
    def __init__(self, daily_limit: int, today: Callable[[], date] = date.today) -> None:
        self._limit = daily_limit
        self._today = today
        self._day = today()
        self._used = 0
        self._lock = threading.Lock()

    def _roll(self) -> None:
        if (d := self._today()) != self._day:
            self._day, self._used = d, 0

    @property
    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self._limit - self._used)

    def check(self, estimated: int) -> None:
        with self._lock:
            self._roll()
            if self._used + estimated > self._limit:
                raise BudgetExceeded(f"daily budget {self._limit} would be exceeded")

    def record(self, tokens: int) -> None:
        with self._lock:
            self._roll()
            self._used += tokens
