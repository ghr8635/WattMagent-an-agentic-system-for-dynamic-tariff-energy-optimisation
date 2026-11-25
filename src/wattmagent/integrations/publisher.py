"""A scheduled advice publisher: plan at set times each day and hand the advice to Home Assistant.

It automates the manual command in ``homeassistant.py`` and keeps its limits: advice only, no
service call, dry run by default. On top of that, the rules that matter for something that runs
unattended (each one is tested):

- **Dry run unless the config says ``publish = true``.** A dry run still plans and logs.
- **Kill switch.** While the stop file exists nothing is planned or sent; a slot that passes during
  the stop is missed, not made up later.
- **Each slot runs once.** The state file remembers finished slots, so a restart never repeats one.
  A slot that fails is retried up to three times, a minute apart, and only inside its grace period;
  a slot more than the grace period (default 30 min) old is dropped, never run late.
- **A good advice is not overwritten by a worse one.** ``fallback`` and ``no_plan`` go out only
  when the advice already in Home Assistant has expired; ``ready`` always is.
- **A daily cap** on real publishes (default 12) stops a loop or a bad config from flooding it.
- **Idempotent planning.** The request to the API carries a key made from the slot, so a retry after
  a lost answer does not plan twice.
- The API key and token come from the environment only and never appear in a log line or error.

Home Assistant is not part of the tests here: they use fakes. The loop has not run for days on a
real host; that is unverified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time as clock
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wattmagent.integrations.homeassistant import (
    Advice,
    HomeAssistantPublisher,
    PublishError,
    _NoRedirect,
    build_advice,
    check_base_url,
)

log = logging.getLogger("wattmagent.publisher")

MAX_JOBS = 10
MAX_TIMES_PER_JOB = 24
MAX_TEXT = 500
MAX_ATTEMPTS = 3
RETRY_AFTER_S = 60
KEEP_DAYS = 3
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(ValueError):
    """The publisher configuration cannot be used."""


# --- configuration ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    text: str
    at: tuple[time, ...]


@dataclass(frozen=True, slots=True)
class PublisherConfig:
    tz: ZoneInfo
    jobs: tuple[Job, ...]
    publish: bool = False
    grace_minutes: int = 30
    max_publishes_per_day: int = 12


def load_config(path: Path) -> PublisherConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    return parse_config(raw)


def parse_config(raw: Mapping[str, Any]) -> PublisherConfig:
    unknown = set(raw) - {"timezone", "publish", "grace_minutes", "max_publishes_per_day", "job"}
    if unknown:
        raise ConfigError(f"unknown settings: {', '.join(sorted(unknown))}")
    try:
        tz = ZoneInfo(str(raw.get("timezone", "Europe/Berlin")))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown time zone {raw.get('timezone')!r}") from exc
    publish = raw.get("publish", False)
    if not isinstance(publish, bool):
        raise ConfigError("publish must be true or false")
    grace, cap = raw.get("grace_minutes", 30), raw.get("max_publishes_per_day", 12)
    if not (isinstance(grace, int) and not isinstance(grace, bool) and 1 <= grace <= 180):
        raise ConfigError("grace_minutes must be a whole number from 1 to 180")
    if not (isinstance(cap, int) and not isinstance(cap, bool) and 1 <= cap <= 100):
        raise ConfigError("max_publishes_per_day must be a whole number from 1 to 100")
    items = raw.get("job", [])
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_JOBS:
        raise ConfigError(f"give between 1 and {MAX_JOBS} [[job]] tables")
    jobs: list[Job] = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) - {"name", "text", "at"}:
            raise ConfigError("a job holds only name, text and at")
        name, text, at = item.get("name"), item.get("text"), item.get("at")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigError("job names use a-z, 0-9, _ and -, at most 40 characters")
        if any(j.name == name for j in jobs):
            raise ConfigError(f"duplicate job name {name!r}")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= MAX_TEXT:
            raise ConfigError(f"job {name}: text must be 1 to {MAX_TEXT} characters")
        if not isinstance(at, list) or not 1 <= len(at) <= MAX_TIMES_PER_JOB:
            raise ConfigError(f"job {name}: at must list 1 to {MAX_TIMES_PER_JOB} times")
        times: list[time] = []
        for stamp in at:
            m = _TIME.fullmatch(stamp) if isinstance(stamp, str) else None
            if m is None:
                raise ConfigError(f"job {name}: {stamp!r} is not a time like 14:05")
            times.append(time(int(m.group(1)), int(m.group(2))))
        if len(set(times)) != len(times):
            raise ConfigError(f"job {name}: a time is listed twice")
        jobs.append(Job(name, text.strip(), tuple(sorted(times))))
    return PublisherConfig(tz, tuple(jobs), publish, grace, cap)


# --- state ------------------------------------------------------------------------------------


@dataclass(slots=True)
class State:
    """What the publisher remembers between runs (a small JSON file, written atomically)."""

    path: Path | None = None
    slots: dict[str, dict[str, Any]] = field(default_factory=dict)
    published: dict[str, int] = field(default_factory=dict)  # local date -> real publishes
    valid_until: str | None = None  # of the last advice that was really published

    @classmethod
    def load(cls, path: Path) -> State:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"the state file {path} is unreadable: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"the state file {path} is not a JSON object")
        slots, pub, until = raw.get("slots", {}), raw.get("published", {}), raw.get("valid_until")
        if not isinstance(slots, dict) or not isinstance(pub, dict):
            raise ConfigError(f"the state file {path} has an unexpected layout")
        return cls(path, slots, {str(k): int(v) for k, v in pub.items()}, until)

    def save(self, today: date) -> None:
        cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
        self.slots = {k: v for k, v in self.slots.items() if k.split("|", 1)[1][:10] >= cutoff}
        self.published = {k: v for k, v in self.published.items() if k >= cutoff}
        if self.path is None:
            return
        body = json.dumps(
            {"slots": self.slots, "published": self.published, "valid_until": self.valid_until},
            indent=1,
            sort_keys=True,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, self.path)


# --- which slots are due ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Slot:
    job: Job
    at: datetime  # local, timezone-aware

    @property
    def key(self) -> str:
        return f"{self.job.name}|{self.at.isoformat(timespec='minutes')}"


def due_slots(now: datetime, config: PublisherConfig, state: State) -> list[Slot]:
    """Slots whose time has come, are still inside the grace period and are not finished."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(config.tz)
    grace = timedelta(minutes=config.grace_minutes)
    out: list[Slot] = []
    for day in (local.date() - timedelta(days=1), local.date()):
        for job in config.jobs:
            for t in job.at:
                at = datetime.combine(day, t, tzinfo=config.tz)
                slot = Slot(job, at)
                if not at <= now < at + grace:
                    continue
                rec = state.slots.get(slot.key)
                if rec is not None and not _retryable(rec, now):
                    continue
                out.append(slot)
    return sorted(out, key=lambda s: s.at)


def _retryable(rec: Mapping[str, Any], now: datetime) -> bool:
    if rec.get("status") != "failed" or int(rec.get("attempts", 0)) >= MAX_ATTEMPTS:
        return False
    try:
        last = datetime.fromisoformat(str(rec.get("at")))
    except ValueError:
        return True
    return now - last >= timedelta(seconds=RETRY_AFTER_S)


# --- one pass ---------------------------------------------------------------------------------

Fetch = Callable[[str, str], dict[str, Any]]  # (text, idempotency key) -> the API's answer


class AdviceSink(Protocol):
    def publish(self, advice: Advice, *, dry_run: bool = True) -> Any: ...


@dataclass(frozen=True, slots=True)
class Outcome:
    slot: str
    status: str  # done, skipped, failed
    detail: str


def idempotency_key(slot: Slot) -> str:
    digest = hashlib.sha256(slot.key.encode()).hexdigest()[:32]
    return f"sched-{slot.job.name}-{digest}"


def run_once(
    now: datetime,
    config: PublisherConfig,
    state: State,
    fetch: Fetch,
    publisher: AdviceSink,
    *,
    stop_file: Path | None = None,
) -> list[Outcome]:
    """Handle every due slot once. Never raises for a failing slot: it is recorded and logged."""
    if stop_file is not None and stop_file.exists():
        log.warning("stop file %s exists: nothing is planned or published", stop_file)
        return []
    outcomes: list[Outcome] = []
    local_date = now.astimezone(config.tz).date()
    for slot in due_slots(now, config, state):
        outcome = _handle(slot, now, config, state, fetch, publisher)
        outcomes.append(outcome)
        log.info("%s: %s (%s)", outcome.slot, outcome.status, outcome.detail)
    if outcomes:
        state.save(local_date)
    return outcomes


def _handle(
    slot: Slot,
    now: datetime,
    config: PublisherConfig,
    state: State,
    fetch: Fetch,
    publisher: AdviceSink,
) -> Outcome:
    key = slot.key
    prior = state.slots.get(key, {})
    attempts = int(prior.get("attempts", 0)) + 1

    def record(status: str, detail: str) -> Outcome:
        state.slots[key] = {
            "status": status,
            "attempts": attempts,
            "at": now.isoformat(),
            "detail": detail,
        }
        return Outcome(key, status, detail)

    today = now.astimezone(config.tz).date().isoformat()
    if config.publish and state.published.get(today, 0) >= config.max_publishes_per_day:
        return record("skipped", "daily publish limit reached")
    try:
        body = fetch(slot.job.text, idempotency_key(slot))
        advice = build_advice(body, now)
    except (PublishError, ValueError, KeyError, TypeError) as exc:
        return record("failed", str(exc)[:200])
    if advice.state != "ready" and _still_valid(state.valid_until, now):
        return record("skipped", f"{advice.state}: the earlier advice is still valid, kept")
    try:
        publisher.publish(advice, dry_run=not config.publish)
    except PublishError as exc:
        return record("failed", str(exc)[:200])
    if config.publish:
        state.published[today] = state.published.get(today, 0) + 1
        state.valid_until = advice.attributes.get("valid_until")
    return record(
        "done",
        f"{advice.state}, {len(advice.items)} items" + ("" if config.publish else ", dry run"),
    )


def _still_valid(valid_until: str | None, now: datetime) -> bool:
    if not valid_until:
        return False
    try:
        return datetime.fromisoformat(valid_until) > now
    except ValueError:
        return False


# --- command line -----------------------------------------------------------------------------


def make_fetch(api_url: str, api_key: str) -> Fetch:
    url = check_base_url(api_url) + "/v1/plans"

    def fetch(text: str, idem: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310  (scheme checked in check_base_url)
            url,
            data=json.dumps({"text": text}).encode("utf-8"),
            method="POST",
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
                "Idempotency-Key": idem,
            },
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=30.0) as response:  # nosec B310
                body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
                return body
        except urllib.error.HTTPError as exc:
            raise PublishError(f"the WattMagent API answered {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PublishError(
                f"could not reach the WattMagent API: {type(exc).__name__}"
            ) from None

    return fetch


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    environ = os.environ if env is None else env
    p = argparse.ArgumentParser(
        description="Publish WattMagent advice to Home Assistant on a schedule."
    )
    p.add_argument(
        "--config",
        required=True,
        type=Path,
        help="publisher TOML (see config/publisher.example.toml)",
    )
    p.add_argument("--state", type=Path, default=Path(".state/publisher.json"))
    p.add_argument("--stop-file", type=Path, default=Path(".state/publisher.stop"))
    p.add_argument("--api-url", default=environ.get("WATTMAGENT_API_URL", "http://localhost:8000"))
    p.add_argument("--ha-url", default=environ.get("WATTMAGENT_HA_URL", ""))
    p.add_argument(
        "--once",
        action="store_true",
        help="handle what is due now, then exit (for cron or a timer)",
    )
    p.add_argument(
        "--check", action="store_true", help="validate the config, print the schedule, exit"
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.check:
            for job in config.jobs:
                times = ", ".join(t.strftime("%H:%M") for t in job.at)
                print(f"{job.name}: {times} ({config.tz.key})")
            print("PUBLISH" if config.publish else "DRY RUN (publish = false)")
            return 0
        key = environ.get("WATTMAGENT_ADVICE_API_KEY", "")
        token = environ.get("WATTMAGENT_HA_TOKEN", "")
        if not key:
            print("set WATTMAGENT_ADVICE_API_KEY (one of the API keys)", file=sys.stderr)
            return 2
        if config.publish and not (token and args.ha_url):
            print(
                "publish = true needs WATTMAGENT_HA_URL (or --ha-url) and WATTMAGENT_HA_TOKEN",
                file=sys.stderr,
            )
            return 2
        publisher = HomeAssistantPublisher(
            args.ha_url or "http://homeassistant.invalid:8123", token or "dry-run-placeholder-token"
        )
        fetch = make_fetch(args.api_url, key)
        state = State.load(args.state)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    log.info("publisher started: %s", "PUBLISHING" if config.publish else "dry run")
    try:
        while True:
            run_once(datetime.now(UTC), config, state, fetch, publisher, stop_file=args.stop_file)
            if args.once:
                return 0
            clock.sleep(30)
    except KeyboardInterrupt:
        log.info("stopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
