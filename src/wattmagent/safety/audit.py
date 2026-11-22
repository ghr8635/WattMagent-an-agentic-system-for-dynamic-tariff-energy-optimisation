"""Audit log: one structured record per planning decision.

Records hold numbers and rule names only: no names, addresses or free text from users.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1
_TAIL_BYTES = 1 << 20  # how much of the end of the file ``recent`` reads


class AuditLog(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


class InMemoryAuditLog:
    """For tests and for callers that want to inspect decisions directly."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` records, newest first."""
        return self.entries[::-1][: max(limit, 0)]


class JsonlAuditLog:
    """Append-only JSON Lines file; one line per decision, flushed immediately."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def recent(self, limit: int) -> list[dict[str, Any]]:
        """The newest ``limit`` records, newest first. Reads only the end of the file, so a
        long log stays cheap, and skips a line cut off at the start of that tail or damaged."""
        if limit <= 0:
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                start = max(0, size - _TAIL_BYTES)
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            return []
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line of a tail is usually cut in the middle
        out: list[dict[str, Any]] = []
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict):
                out.append(entry)
                if len(out) == limit:
                    break
        return out
