"""Measured consumption from Home Assistant history: read-only, for display next to the plan.

A cumulative energy sensor (kWh, or Wh, that only counts up: a grid import or whole-house meter)
is read from Home Assistant's history and turned into energy per hour. Rules:

- **Read-only.** The only request is ``GET /api/history/period/<time>`` for one entity that looks
  like ``sensor.<name>``. It calls no service and changes nothing in Home Assistant.
- **No guessing.** An hour is reported only when the meter had a reading at or before its start
  and at or before its end and did not go backwards. Anything else, a gap, an
  ``unavailable`` meter, a reset, an hour not over yet, is ``None``, never an estimate.
- The token comes from the environment, is redacted in ``repr`` and never appears in an error.
  Redirects are not followed, requests time out, proxy settings are ignored.

The layout of the history reply is written from Home Assistant's documentation, not from a real
instance: tests use a local fake server. How a real instance answers is unverified.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from wattmagent.integrations.homeassistant import TIMEOUT_S, _NoRedirect, check_base_url

_ENTITY = re.compile(r"^sensor\.[a-z0-9_]{1,60}$")
_UNITS = {"kWh": 1.0, "Wh": 0.001}
MAX_HOURS = 26  # a day is at most 25 hours; one more is slack
MAX_READINGS = 50_000  # a sane bound on what one reply may hold


class HistoryError(RuntimeError):
    """Home Assistant could not be read. The message never contains the token."""


Reading = tuple[datetime, float]


def parse_history(payload: Any) -> list[Reading]:
    """Readings (time, value in the sensor's unit), oldest first, from a history reply.

    Entries whose state is not a finite number (``unavailable``, ``unknown``) are skipped.
    """
    if not isinstance(payload, list) or not payload:
        return []
    series = payload[0]
    if not isinstance(series, list):
        raise HistoryError("unexpected history reply")
    out: list[Reading] = []
    for row in series[:MAX_READINGS]:
        if not isinstance(row, dict):
            continue
        stamp = row.get("last_changed") or row.get("last_updated")
        try:
            at = datetime.fromisoformat(str(stamp))
            value = float(str(row.get("state")))
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None or value != value or value in (float("inf"), float("-inf")):
            continue
        out.append((at.astimezone(UTC), value))
    out.sort(key=lambda r: r[0])
    return out


def value_at(readings: list[Reading], at: datetime) -> float | None:
    """The meter's last reading at or before ``at``; None if it had none yet."""
    best: float | None = None
    for when, value in readings:
        if when > at:
            break
        best = value
    return best


def hourly_kwh(
    readings: list[Reading], starts: list[datetime], now: datetime, *, unit_factor: float = 1.0
) -> list[float | None]:
    """Energy per hour (kWh) for hours that start at ``starts``; None where it cannot be known."""
    out: list[float | None] = []
    for start in starts:
        end = start + timedelta(hours=1)
        before, after = value_at(readings, start), value_at(readings, end)
        if end > now or before is None or after is None or after < before:
            out.append(None)  # not over, no reading, or the meter went backwards (a reset)
        else:
            out.append(round((after - before) * unit_factor, 4))
    return out


@dataclass(slots=True)
class HomeAssistantHistory:
    base_url: str
    token: str = field(repr=False)
    entity_id: str = ""
    unit: str = "kWh"

    @property
    def description(self) -> str:
        return f"Home Assistant history, {self.entity_id}"

    def __post_init__(self) -> None:
        self.base_url = check_base_url(self.base_url)
        if len(self.token) < 20:
            raise ValueError("the Home Assistant token looks too short to be a real one")
        if not _ENTITY.fullmatch(self.entity_id):
            raise ValueError("the energy entity must look like sensor.<name>")
        if self.unit not in _UNITS:
            raise ValueError("the energy unit must be kWh or Wh")

    def path_for(self, start: datetime, end: datetime) -> str:
        """The one kind of request this client sends (what a test or a dry run can show)."""
        query = urlencode(
            {
                "filter_entity_id": self.entity_id,
                "end_time": end.astimezone(UTC).isoformat(),
                "minimal_response": "",
                "no_attributes": "",
            }
        )
        return f"/api/history/period/{quote(start.astimezone(UTC).isoformat())}?{query}"

    def readings(self, start: datetime, end: datetime) -> list[Reading]:
        path = self.path_for(start, end)
        if not path.startswith("/api/history/period/"):
            raise HistoryError("refusing a request outside the history read")  # defence in depth
        request = urllib.request.Request(  # noqa: S310  (scheme checked in check_base_url)
            self.base_url + path,
            method="GET",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:  # nosec B310
                if response.status != 200:
                    raise HistoryError(f"Home Assistant answered {response.status}")
                raw = response.read(8_000_000)
        except urllib.error.HTTPError as exc:
            raise HistoryError(f"Home Assistant answered {exc.code} for the history") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HistoryError(f"could not reach Home Assistant: {type(exc).__name__}") from None
        try:
            return parse_history(json.loads(raw))
        except ValueError:
            raise HistoryError("Home Assistant sent a history that is not valid JSON") from None

    def hourly_kwh(self, starts: list[datetime], now: datetime) -> list[float | None]:
        """Energy per hour for the given hour starts (UTC-aware), from one history request."""
        if not starts or len(starts) > MAX_HOURS:
            raise ValueError("between 1 and 26 hours are read at a time")
        end = min(starts[-1] + timedelta(hours=1), now)
        # the reply holds the state at the start time first (Home Assistant's default)
        readings = self.readings(starts[0], max(end, starts[0] + timedelta(seconds=1)))
        return hourly_kwh(readings, starts, now, unit_factor=_UNITS[self.unit])
