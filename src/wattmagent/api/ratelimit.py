"""Sliding-window rate limit per caller, in memory.

In-memory means the limit is per process. With several workers or replicas each one
counts separately; a shared store (for example Redis) is the production upgrade and is
listed in docs/production-checklist.md.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0 or window_s <= 0:
            raise ValueError("limit and window must be positive")
        self._limit = limit
        self._window = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, who: str) -> float:
        """Count one request. Returns 0.0 if allowed, else the seconds until it would be."""
        now = self._clock()
        with self._lock:
            hits = self._hits.setdefault(who, deque())
            while hits and now - hits[0] >= self._window:
                hits.popleft()
            if len(hits) >= self._limit:
                return max(self._window - (now - hits[0]), 0.001)
            hits.append(now)
            return 0.0
