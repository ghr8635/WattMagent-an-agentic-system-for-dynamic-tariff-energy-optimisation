"""Idempotency keys: a repeated request returns the stored answer instead of running again.

Scope is (caller, key). The same key with a *different* request is a client error, not a
replay. A request that is still running is reported as in flight rather than run twice.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MAX_ENTRIES = 10_000


class Kind(StrEnum):
    FRESH = "fresh"  # first time: the caller must run the request, then call finish/abort
    REPLAY = "replay"  # same key and same request: return ``stored``
    CONFLICT = "conflict"  # same key, different request
    IN_FLIGHT = "in_flight"  # same key and request, still running


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Begin:
    kind: Kind
    stored: StoredResponse | None = None


@dataclass(slots=True)
class _Entry:
    fingerprint: str
    created: float
    stored: StoredResponse | None = None


class IdempotencyStore:
    def __init__(
        self, ttl_s: float = 86_400.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl must be positive")
        self._ttl = ttl_s
        self._clock = clock
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if now - e.created >= self._ttl]
        for k in expired:
            del self._entries[k]
        while len(self._entries) >= MAX_ENTRIES:  # oldest first (dicts keep insertion order)
            del self._entries[next(iter(self._entries))]

    def begin(self, scope: str, key: str, fingerprint: str) -> Begin:
        now = self._clock()
        with self._lock:
            self._purge(now)
            entry = self._entries.get((scope, key))
            if entry is None:
                self._entries[(scope, key)] = _Entry(fingerprint, now)
                return Begin(Kind.FRESH)
            if entry.fingerprint != fingerprint:
                return Begin(Kind.CONFLICT)
            if entry.stored is None:
                return Begin(Kind.IN_FLIGHT)
            return Begin(Kind.REPLAY, entry.stored)

    def finish(self, scope: str, key: str, stored: StoredResponse) -> None:
        with self._lock:
            entry = self._entries.get((scope, key))
            if entry is not None:
                entry.stored = stored

    def abort(self, scope: str, key: str) -> None:
        """The request failed unexpectedly: forget it so the client can retry."""
        with self._lock:
            entry = self._entries.get((scope, key))
            if entry is not None and entry.stored is None:
                del self._entries[(scope, key)]
