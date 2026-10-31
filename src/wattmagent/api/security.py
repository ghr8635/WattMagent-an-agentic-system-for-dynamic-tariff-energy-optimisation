"""API keys: constant-time comparison, and a short non-secret id used in logs and limits.

Keys are never logged or stored by the API. ``key_id`` is a truncated SHA-256 of the key:
enough to tell clients apart in logs, useless for recovering the key.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

MIN_KEY_LENGTH = 16


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class KeyRing:
    def __init__(self, keys: Iterable[str]) -> None:
        cleaned = [k.strip() for k in keys if k.strip()]
        if not cleaned:
            raise ValueError("at least one API key is required (WATTMAGENT_API_KEYS)")
        short = [k for k in cleaned if len(k) < MIN_KEY_LENGTH]
        if short:
            raise ValueError(f"API keys must be at least {MIN_KEY_LENGTH} characters long")
        self._keys = cleaned

    def authenticate(self, provided: str | None) -> str | None:
        """The key id of a valid key, else None. Every stored key is compared, never early-exit."""
        if not provided:
            return None
        candidate = provided.encode("utf-8")
        match: str | None = None
        for key in self._keys:
            if hmac.compare_digest(candidate, key.encode("utf-8")):
                match = key_id(key)
        return match
