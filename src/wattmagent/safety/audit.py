"""Audit log: one structured record per planning decision.

Records hold numbers and rule names only: no names, addresses or free text from users.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1


class AuditLog(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


class InMemoryAuditLog:
    """For tests and for callers that want to inspect decisions directly."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(self, entry: dict[str, Any]) -> None:
        self.entries.append(entry)


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
