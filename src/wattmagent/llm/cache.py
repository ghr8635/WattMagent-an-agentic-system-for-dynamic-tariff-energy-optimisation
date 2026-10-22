"""Exact-match on-disk cache for LLM completions (layer 1 of the caching plan)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def make_key(model: str, messages: list[dict[str, str]], max_tokens: int) -> str:
    raw = json.dumps([model, messages, max_tokens], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class DiskCache:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, key: str) -> dict[str, object] | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data: dict[str, object] = json.loads(p.read_text(encoding="utf-8"))
            return data
        except (OSError, json.JSONDecodeError):
            return None

    def set(self, key: str, value: dict[str, object]) -> None:
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path(key))  # atomic on POSIX
