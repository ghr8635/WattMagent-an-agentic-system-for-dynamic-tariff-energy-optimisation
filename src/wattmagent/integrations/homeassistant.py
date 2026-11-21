"""Advice mode for Home Assistant: publish a verified plan as information, never as a command.

What it does, and the rules that keep it harmless:

- It turns the answer of ``POST /v1/plans`` (appliance requests) into one **advice document**:
  a sensor state with the plan as attributes, plus one event, so an automation in Home Assistant
  can notify a person ("start the dishwasher at 02:00?") who then decides.
- It calls **no Home Assistant service**, so it cannot switch a device. The client only accepts
  ``POST /api/states/sensor.wattmagent_*`` and ``POST /api/events/wattmagent_*``: it cannot
  overwrite another entity or fire another event, and a test checks the code never mentions the
  services endpoint.
- **Dry run is the default.** Nothing is sent without ``--publish``.
- Every item carries ``expires_at``; advice that has passed it must not be acted on.
- A fallback plan is published as ``fallback`` (the safe default), a request that could not be
  planned as ``no_plan`` with no items.
- The token comes from the environment, never from an argument, is redacted in ``repr``, and is
  never part of an error message or an output. Redirects are not followed (they could carry the
  token elsewhere), requests time out, and the connection ignores proxy settings (Home Assistant
  is usually on the local network).

Home Assistant itself is not part of the tests here: they run against a local fake server. How a
real instance treats these calls is unverified (see docs/home-assistant.md).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

DEFAULT_ENTITY = "sensor.wattmagent_advice"
DEFAULT_EVENT = "wattmagent_advice"
MAX_ITEMS = 40  # keeps the state attributes a sensible size
TIMEOUT_S = 10.0

_ENTITY = re.compile(r"^sensor\.wattmagent_[a-z0-9_]{1,40}$")
_EVENT = re.compile(r"^wattmagent_[a-z0-9_]{1,40}$")


class PublishError(RuntimeError):
    """The advice could not be delivered. The message never contains the token."""


# --- the advice document ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Advice:
    state: str  # "ready", "fallback" or "no_plan"
    attributes: dict[str, Any]

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self.attributes["items"])

    def expired(self, now: datetime) -> bool:
        until = self.attributes.get("valid_until")
        return until is None or datetime.fromisoformat(until) <= now


def _aware(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError(f"timestamp without a time zone: {text!r}")
    return value


def build_advice(body: Mapping[str, Any], now: datetime) -> Advice:
    """Make advice from the JSON answer of ``POST /v1/plans``. ``now`` must be timezone-aware."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    status = str(body.get("status", ""))
    base: dict[str, Any] = {
        "source": "wattmagent",
        "mode": "advice",
        "actuation": False,  # this integration never switches anything
        "generated_at": now.isoformat(),
        "request_id": body.get("request_id"),
        "decision": body.get("decision"),
        "message": str(body.get("message", ""))[:1000],
        "items": [],
        "valid_until": None,
    }
    facts = body.get("facts")
    if status != "planned" or not isinstance(facts, Mapping):
        return Advice("no_plan", {**base, "reason": status or "unknown"})

    items: list[dict[str, Any]] = []
    for cycle in facts.get("cycles", []):
        start, end = _aware(cycle["start_at"]), _aware(cycle["end_at"])
        now_cost, base_cost = (
            cycle.get("cost_eur"),
            cycle.get("cost_if_started_at_window_start_eur"),
        )
        items.append(
            {
                "kind": "cycle",
                "appliance": cycle["appliance"],
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "energy_kwh": cycle["energy_kwh"],
                "cost_eur": now_cost,
                "saving_eur": None
                if now_cost is None or base_cost is None
                else round(base_cost - now_cost, 2),
                "expires_at": start.isoformat(),  # once the slot has begun it is no longer advice
            }
        )
    battery = facts.get("battery")
    if isinstance(battery, Mapping):
        for action, key in (("charge", "charge_at"), ("discharge", "discharge_at")):
            if battery.get(key):
                items.append(_hours_item("battery", action, battery[key]))
    if facts.get("tank_heating_at"):
        items.append(_hours_item("hot_water_tank", "heat", facts["tank_heating_at"]))

    items = items[:MAX_ITEMS]
    expiries = [_aware(i["expires_at"]) for i in items]
    state = "ready" if body.get("decision") == "optimised" else "fallback"
    attributes = {**base, "items": items}
    attributes["valid_until"] = max(expiries).isoformat() if expiries else None
    attributes["price_basis"] = facts.get("price_basis")
    return Advice(state if items else "no_plan", attributes)


def _hours_item(kind: str, action: str, stamps: Sequence[str]) -> dict[str, Any]:
    hours = sorted(_aware(s) for s in stamps)
    return {
        "kind": kind,
        "action": action,
        "hours": [h.isoformat() for h in hours],
        "expires_at": hours[-1].isoformat(),
    }


# --- delivery ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    method: str
    path: str
    body: dict[str, Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # a redirect would resend the token to a place we did not choose


def check_base_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("the Home Assistant URL must look like http://host:8123")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("the Home Assistant URL must not hold credentials, a query or a fragment")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


@dataclass(slots=True)
class HomeAssistantPublisher:
    base_url: str
    token: str = field(repr=False)
    entity_id: str = DEFAULT_ENTITY
    event_type: str = DEFAULT_EVENT

    def __post_init__(self) -> None:
        self.base_url = check_base_url(self.base_url)
        if len(self.token) < 20:
            raise ValueError("the Home Assistant token looks too short to be a real one")
        if not _ENTITY.fullmatch(self.entity_id):
            raise ValueError("entity id must look like sensor.wattmagent_<name>")
        if not _EVENT.fullmatch(self.event_type):
            raise ValueError("event type must look like wattmagent_<name>")

    def plan_calls(self, advice: Advice) -> list[Call]:
        """The exact requests that publishing would send (what a dry run shows)."""
        return [
            Call(
                "POST",
                f"/api/states/{self.entity_id}",
                {"state": advice.state, "attributes": advice.attributes},
            ),
            Call(
                "POST",
                f"/api/events/{self.event_type}",
                {"state": advice.state, **advice.attributes},
            ),
        ]

    def publish(self, advice: Advice, *, dry_run: bool = True) -> list[Call]:
        calls = self.plan_calls(advice)
        if dry_run:
            return calls
        for call in calls:
            self._send(call)
        return calls

    def _send(self, call: Call) -> None:
        if call.method != "POST" or not re.match(
            r"^/api/(states/sensor\.wattmagent_|events/wattmagent_)[a-z0-9_]+$", call.path
        ):
            raise PublishError("refusing a request outside advice mode")  # defence in depth
        request = urllib.request.Request(  # noqa: S310  (scheme checked in check_base_url)
            self.base_url + call.path,
            data=json.dumps(call.body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:  # nosec B310
                if not 200 <= response.status < 300:
                    raise PublishError(f"Home Assistant answered {response.status}")
        except urllib.error.HTTPError as exc:
            raise PublishError(f"Home Assistant answered {exc.code} for {call.path}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PublishError(f"could not reach Home Assistant: {type(exc).__name__}") from None


# --- command line -----------------------------------------------------------------------------


def _fetch_plan(api_url: str, api_key: str, text: str) -> dict[str, Any]:
    url = check_base_url(api_url) + "/v1/plans"
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps({"text": text}).encode("utf-8"),
        method="POST",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=30.0) as response:  # nosec B310
            body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return body
    except urllib.error.HTTPError as exc:
        raise PublishError(f"the WattMagent API answered {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PublishError(f"could not reach the WattMagent API: {type(exc).__name__}") from None


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    environ = os.environ if env is None else env
    parser = argparse.ArgumentParser(
        description="Publish a WattMagent plan to Home Assistant as advice (dry run by default)."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help='request to plan, e.g. "run the dishwasher before 8pm"')
    source.add_argument("--from-file", help="a saved answer of POST /v1/plans (JSON)")
    parser.add_argument(
        "--api-url", default=environ.get("WATTMAGENT_API_URL", "http://localhost:8000")
    )
    parser.add_argument("--ha-url", default=environ.get("WATTMAGENT_HA_URL", ""))
    parser.add_argument("--entity-id", default=DEFAULT_ENTITY)
    parser.add_argument("--event-type", default=DEFAULT_EVENT)
    parser.add_argument("--publish", action="store_true", help="really send; without it, a dry run")
    args = parser.parse_args(argv)

    try:
        if args.from_file:
            with open(args.from_file, encoding="utf-8") as handle:
                body = json.load(handle)
        else:
            key = environ.get("WATTMAGENT_ADVICE_API_KEY", "")
            if not key:
                print("set WATTMAGENT_ADVICE_API_KEY (one of the API keys)", file=sys.stderr)
                return 2
            body = _fetch_plan(args.api_url, key, args.text)
        advice = build_advice(body, datetime.now(UTC))
        token = environ.get("WATTMAGENT_HA_TOKEN", "")
        if args.publish and not (token and args.ha_url):
            print(
                "--publish needs WATTMAGENT_HA_URL (or --ha-url) and WATTMAGENT_HA_TOKEN",
                file=sys.stderr,
            )
            return 2
        publisher = HomeAssistantPublisher(
            args.ha_url or "http://homeassistant.invalid:8123",
            token or "dry-run-placeholder-token",
            args.entity_id,
            args.event_type,
        )
        calls = publisher.publish(advice, dry_run=not args.publish)
    except (PublishError, ValueError, OSError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        ("PUBLISHED" if args.publish else "DRY RUN (nothing was sent)")
        + f": state = {advice.state}"
    )
    for call in calls:
        print(f"{call.method} {call.path}")
    print(json.dumps(advice.attributes, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
