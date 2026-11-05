"""Logging setup: one handler on the ``wattmagent`` logger, as plain text or JSON lines.

Without this, Python's root logger drops our INFO records, so request logs never appear.
Records carry these structured fields when given via ``extra=``; nothing else is
emitted, so request text, API keys and prices cannot leak through a careless extra.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

FIELDS = ("request_id", "method", "path", "status", "duration_ms", "key_id")
_HANDLER_NAME = "wattmagent-stream"


def _fields(record: logging.LogRecord) -> dict[str, object]:
    return {name: getattr(record, name) for name in FIELDS if hasattr(record, name)}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_fields(record),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{k}={v}" for k, v in _fields(record).items())
        return f"{line} {extras}" if extras else line


def configure_logging(fmt: str = "text", level: str = "INFO") -> None:
    """Install (or replace) our handler. Safe to call more than once."""
    if fmt not in ("text", "json"):
        raise ValueError(f"unknown log format: {fmt!r} (use 'text' or 'json')")
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        raise ValueError(f"unknown log level: {level!r}")
    logger = logging.getLogger("wattmagent")
    logger.setLevel(numeric)
    for handler in list(logger.handlers):
        if handler.get_name() == _HANDLER_NAME:
            logger.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    logger.addHandler(handler)
