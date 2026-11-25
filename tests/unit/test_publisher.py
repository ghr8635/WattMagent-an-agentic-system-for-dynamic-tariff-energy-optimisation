"""The scheduled publisher: which slots run, what is never repeated, and what stops it."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from tests.unit.test_homeassistant import PLAIN, body_for
from wattmagent.integrations.homeassistant import Advice, PublishError
from wattmagent.integrations.publisher import (
    MAX_ATTEMPTS,
    ConfigError,
    State,
    due_slots,
    idempotency_key,
    main,
    parse_config,
    run_once,
)

BERLIN = ZoneInfo("Europe/Berlin")
# a winter day: Berlin is UTC+1, so 14:10 local is 13:10 UTC
DAY = datetime(2026, 3, 10, tzinfo=BERLIN)


def at(hour: int, minute: int = 0, day: int = 10) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


RAW: dict[str, Any] = {
    "timezone": "Europe/Berlin",
    "job": [{"name": "dish", "text": "run the dishwasher before 8pm", "at": ["14:10", "18:30"]}],
}


def config(**over: Any):  # type: ignore[no-untyped-def]
    return parse_config({**RAW, **over})


class Sink:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[Advice, bool]] = []
        self.fail = fail

    def publish(self, advice: Advice, *, dry_run: bool = True) -> None:
        if self.fail:
            raise PublishError("could not reach Home Assistant: URLError")
        self.sent.append((advice, dry_run))


class Api:
    """A fake API: returns a planned answer and remembers what it was asked."""

    def __init__(self, **kw: Any) -> None:
        self.calls: list[tuple[str, str]] = []
        self.kw = kw
        self.error: Exception | None = None
        self.body: dict[str, Any] | None = None

    def __call__(self, text: str, key: str) -> dict[str, Any]:
        self.calls.append((text, key))
        if self.error:
            raise self.error
        return self.body or body_for(text, PLAIN, **self.kw)


# --- the configuration ------------------------------------------------------------------------


def test_a_valid_config_is_read_with_safe_defaults() -> None:
    cfg = config()
    assert cfg.publish is False and cfg.grace_minutes == 30 and cfg.max_publishes_per_day == 12
    assert [t.strftime("%H:%M") for t in cfg.jobs[0].at] == ["14:10", "18:30"]


@pytest.mark.parametrize(
    "change",
    [
        {"publish": "yes"},
        {"timezone": "Mars/Base"},
        {"grace_minutes": 0},
        {"grace_minutes": True},
        {"max_publishes_per_day": 1000},
        {"surprise": 1},
        {"job": []},
        {"job": [{"name": "A B", "text": "x", "at": ["10:00"]}]},
        {"job": [{"name": "a", "text": "", "at": ["10:00"]}]},
        {"job": [{"name": "a", "text": "x" * 501, "at": ["10:00"]}]},
        {"job": [{"name": "a", "text": "x", "at": ["25:00"]}]},
        {"job": [{"name": "a", "text": "x", "at": ["9:00"]}]},
        {"job": [{"name": "a", "text": "x", "at": ["10:00", "10:00"]}]},
        {"job": [{"name": "a", "text": "x", "at": []}]},
        {"job": [{"name": "a", "text": "x", "at": ["10:00"], "extra": 1}]},
        {"job": [{"name": "a", "text": "x", "at": ["10:00"]}] * 2},
        {"job": [{"name": f"j{i}", "text": "x", "at": ["10:00"]} for i in range(11)]},
    ],
)
def test_a_bad_config_is_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        config(**change)


def test_the_shipped_example_config_is_valid_and_a_dry_run() -> None:
    import tomllib

    raw = tomllib.loads(
        (Path(__file__).resolve().parents[2] / "config" / "publisher.example.toml").read_text()
    )
    cfg = parse_config(raw)
    assert cfg.publish is False and len(cfg.jobs) == 2


# --- which slots are due ----------------------------------------------------------------------


def test_a_slot_is_due_from_its_time_until_the_grace_period_ends() -> None:
    cfg, state = config(), State()
    assert due_slots(at(14, 9), cfg, state) == []
    assert [s.at.strftime("%H:%M") for s in due_slots(at(14, 10), cfg, state)] == ["14:10"]
    assert len(due_slots(at(14, 39), cfg, state)) == 1
    assert due_slots(at(14, 40), cfg, state) == []  # more than 30 minutes late: dropped


def test_the_grace_period_is_configurable() -> None:
    assert len(due_slots(at(14, 55), config(grace_minutes=60), State())) == 1


def test_a_slot_just_before_midnight_is_still_due_after_midnight() -> None:
    cfg = config(job=[{"name": "late", "text": "run the dishwasher", "at": ["23:50"]}])
    got = due_slots(at(0, 10, day=11), cfg, State())
    assert [s.at.day for s in got] == [10]  # yesterday's 23:50


def test_naive_time_is_refused() -> None:
    with pytest.raises(ValueError):
        due_slots(datetime(2026, 3, 10, 14, 10), config(), State())


def test_the_slot_follows_the_clock_across_daylight_saving() -> None:
    cfg = config()
    summer = datetime(2026, 7, 10, 14, 10, tzinfo=BERLIN).astimezone(UTC)  # 12:10 UTC
    assert len(due_slots(summer, cfg, State())) == 1


# --- one pass ---------------------------------------------------------------------------------


def test_a_due_slot_plans_once_and_publishes_when_enabled() -> None:
    api, sink, state = Api(), Sink(), State()
    out = run_once(at(14, 12), config(publish=True), state, api, sink)
    assert [o.status for o in out] == ["done"] and len(api.calls) == 1 and len(sink.sent) == 1
    advice, dry = sink.sent[0]
    assert advice.state == "ready" and dry is False
    assert api.calls[0][0] == "run the dishwasher before 8pm"
    # the same slot is not run again, in this process or after a restart
    assert run_once(at(14, 20), config(publish=True), state, api, sink) == []
    assert len(sink.sent) == 1


def test_the_default_is_a_dry_run_that_plans_but_sends_nothing_real() -> None:
    api, sink, state = Api(), Sink(), State()
    out = run_once(at(14, 12), config(), state, api, sink)
    assert out[0].status == "done" and "dry run" in out[0].detail
    assert sink.sent[0][1] is True  # dry_run flag passed to the publisher
    assert state.published == {} and state.valid_until is None  # nothing counted as published


def test_the_state_file_stops_a_restart_from_repeating_a_slot(tmp_path: Path) -> None:
    path = tmp_path / "s" / "state.json"
    api, sink = Api(), Sink()
    run_once(at(14, 12), config(publish=True), State(path), api, sink)
    reloaded = State.load(path)
    assert run_once(at(14, 15), config(publish=True), reloaded, api, sink) == []
    assert len(api.calls) == 1
    assert oct(path.stat().st_mode & 0o777) in ("0o600", "0o666")  # 0600 (Windows ignores modes)
    assert json.loads(path.read_text())["valid_until"] is not None


def test_a_failed_slot_is_retried_a_minute_later_up_to_three_times() -> None:
    api, sink, state = Api(), Sink(), State()
    api.error = PublishError("the WattMagent API answered 503")
    cfg = config(publish=True)
    assert run_once(at(14, 10), cfg, state, api, sink)[0].status == "failed"
    assert run_once(at(14, 10) + timedelta(seconds=30), cfg, state, api, sink) == []  # too soon
    assert run_once(at(14, 11), cfg, state, api, sink)[0].status == "failed"
    assert run_once(at(14, 12), cfg, state, api, sink)[0].status == "failed"
    assert len(api.calls) == MAX_ATTEMPTS
    assert run_once(at(14, 20), cfg, state, api, sink) == []  # gave up
    assert sink.sent == []


def test_a_retry_that_works_ends_the_slot() -> None:
    api, sink, state = Api(), Sink(), State()
    api.error = PublishError("the WattMagent API answered 503")
    run_once(at(14, 10), config(publish=True), state, api, sink)
    api.error = None
    out = run_once(at(14, 12), config(publish=True), state, api, sink)
    assert out[0].status == "done" and state.slots[out[0].slot]["attempts"] == 2
    assert run_once(at(14, 14), config(publish=True), state, api, sink) == []


def test_a_retry_cannot_run_after_the_grace_period() -> None:
    api, sink, state = Api(), Sink(), State()
    api.error = PublishError("down")
    run_once(at(14, 10), config(publish=True), state, api, sink)
    assert run_once(at(14, 45), config(publish=True), state, api, sink) == []


def test_a_publish_failure_is_recorded_not_raised_and_not_counted() -> None:
    state = State()
    out = run_once(at(14, 10), config(publish=True), state, Api(), Sink(fail=True))
    assert out[0].status == "failed" and "could not reach" in out[0].detail
    assert state.published == {} and state.valid_until is None


def test_odd_answers_from_the_api_fail_cleanly() -> None:
    for body in (
        {"status": "planned", "decision": "optimised", "facts": {"cycles": [{}]}},
        {"facts": 5},
    ):
        api = Api()
        api.body = body
        out = run_once(at(14, 10), config(publish=True), State(), api, Sink())
        assert out[0].status in ("failed", "done")  # never an exception
    api = Api()
    api.error = TypeError("odd")
    assert run_once(at(14, 10), config(publish=True), State(), api, Sink())[0].status == "failed"


def test_the_stop_file_halts_everything_and_the_slot_is_not_made_up(tmp_path: Path) -> None:
    stop = tmp_path / "stop"
    stop.write_text("")
    api, sink, state = Api(), Sink(), State()
    assert run_once(at(14, 10), config(publish=True), state, api, sink, stop_file=stop) == []
    assert api.calls == [] and sink.sent == []  # not even planned
    stop.unlink()
    assert (
        run_once(at(14, 20), config(publish=True), state, api, sink, stop_file=stop) != []
    )  # inside grace
    assert (
        run_once(at(15, 30), config(publish=True), State(), api, sink, stop_file=stop) == []
    )  # slot gone


def test_a_worse_advice_does_not_overwrite_a_valid_one() -> None:
    api, sink, state = Api(), Sink(), State()
    cfg = config(publish=True)
    run_once(at(14, 10), cfg, state, api, sink)  # a ready advice, valid for hours
    assert state.valid_until is not None  # taken from the advice itself
    # the canned plan is dated by the other test module's clock; pin its validity to ours
    state.valid_until = (at(18, 30) + timedelta(hours=3)).isoformat()
    api.kw = {"age_h": 100}  # stale prices: the API answers with a fallback
    out = run_once(at(18, 30), cfg, state, api, sink)
    assert out[0].status == "skipped" and "still valid" in out[0].detail
    assert len(sink.sent) == 1


def test_a_fallback_is_published_when_nothing_valid_is_out() -> None:
    api, sink, state = Api(age_h=100), Sink(), State()
    out = run_once(at(14, 10), config(publish=True), state, api, sink)
    assert out[0].status == "done" and sink.sent[0][0].state == "fallback"


def test_a_ready_advice_replaces_a_valid_one() -> None:
    api, sink, state = Api(), Sink(), State()
    cfg = config(publish=True)
    run_once(at(14, 10), cfg, state, api, sink)
    run_once(at(18, 30), cfg, state, api, sink)
    assert [a.state for a, _ in sink.sent] == ["ready", "ready"]


def test_expired_advice_no_longer_blocks_a_fallback() -> None:
    api, sink = Api(age_h=100), Sink()
    state = State(valid_until=(at(14, 10) - timedelta(hours=1)).isoformat())
    out = run_once(at(14, 10), config(publish=True), state, api, sink)
    assert out[0].status == "done"


def test_the_daily_cap_stops_real_publishing() -> None:
    cfg = config(
        publish=True,
        max_publishes_per_day=2,
        job=[{"name": "dish", "text": "run the dishwasher", "at": ["10:00", "11:00", "12:00"]}],
    )
    api, sink, state = Api(), Sink(), State()
    outs = [run_once(at(h, 5), cfg, state, api, sink)[0] for h in (10, 11, 12)]
    assert [o.status for o in outs] == ["done", "done", "skipped"]
    assert "limit" in outs[2].detail and len(sink.sent) == 2
    # the next day has its own count
    cfg_next = run_once(at(10, 5, day=11), cfg, state, api, sink)
    assert cfg_next[0].status == "done"


def test_a_dry_run_is_not_counted_against_the_cap() -> None:
    cfg = config(
        max_publishes_per_day=1,
        job=[{"name": "dish", "text": "run the dishwasher", "at": ["10:00", "11:00"]}],
    )
    api, sink, state = Api(), Sink(), State()
    outs = [run_once(at(h, 5), cfg, state, api, sink)[0] for h in (10, 11)]
    assert [o.status for o in outs] == ["done", "done"]


def test_the_request_to_the_api_carries_a_stable_idempotency_key() -> None:
    api, state = Api(), State()
    run_once(at(14, 10), config(), state, api, Sink())
    key = api.calls[0][1]
    slot = due_slots(at(14, 10), config(), State())[0]
    assert key == idempotency_key(slot) and 8 <= len(key) <= 128
    assert all(c.isalnum() or c in "._-" for c in key)
    other = due_slots(at(18, 30), config(), State())[0]
    assert idempotency_key(other) != key


def test_old_slots_are_pruned_from_the_state(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State(path)
    state.slots["dish|2026-03-01T14:10"] = {"status": "done"}
    state.published["2026-03-01"] = 3
    run_once(at(14, 10), config(publish=True), state, Api(), Sink())
    saved = json.loads(path.read_text())
    assert "dish|2026-03-01T14:10" not in saved["slots"] and "2026-03-01" not in saved["published"]
    assert "2026-03-10" in saved["published"]


def test_a_damaged_state_file_is_an_error_not_a_silent_reset(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError):
        State.load(path)
    path.write_text("[1]")
    with pytest.raises(ConfigError):
        State.load(path)
    assert State.load(tmp_path / "missing.json").slots == {}


# --- the command line -------------------------------------------------------------------------


def write_config(tmp_path: Path, publish: str = "false") -> Path:
    p = tmp_path / "pub.toml"
    p.write_text(
        f'timezone = "Europe/Berlin"\npublish = {publish}\n\n[[job]]\nname = "dish"\n'
        'text = "run the dishwasher"\nat = ["14:10"]\n'
    )
    return p


def test_check_prints_the_schedule_and_the_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(write_config(tmp_path)), "--check"], env={}) == 0
    out = capsys.readouterr().out
    assert "dish: 14:10 (Europe/Berlin)" in out and "DRY RUN" in out
    assert main(["--config", str(write_config(tmp_path, "true")), "--check"], env={}) == 0
    assert "PUBLISH" in capsys.readouterr().out


def test_the_command_line_refuses_missing_keys_and_a_bad_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(write_config(tmp_path)), "--once"], env={}) == 2
    assert "WATTMAGENT_ADVICE_API_KEY" in capsys.readouterr().err
    env = {"WATTMAGENT_ADVICE_API_KEY": "k" * 20}
    assert main(["--config", str(write_config(tmp_path, "true")), "--once"], env=env) == 2
    assert "WATTMAGENT_HA_TOKEN" in capsys.readouterr().err
    bad = tmp_path / "bad.toml"
    bad.write_text('publish = "yes"\n')
    assert main(["--config", str(bad), "--check"], env={}) == 1
    assert main(["--config", str(tmp_path / "nope.toml"), "--check"], env={}) == 1


def test_a_once_run_with_nothing_due_exits_cleanly(tmp_path: Path) -> None:
    env = {"WATTMAGENT_ADVICE_API_KEY": "k" * 20}
    args = ["--config", str(write_config(tmp_path)), "--once", "--state", str(tmp_path / "s.json"),
            "--stop-file", str(tmp_path / "stop")]  # fmt: skip
    assert main(args, env=env) == 0


def test_the_publisher_never_logs_secrets_or_calls_services() -> None:
    src = (
        Path(__file__).resolve().parents[2] / "src/wattmagent/integrations/publisher.py"
    ).read_text()
    assert "/api/services" not in src
    log_lines = [ln for ln in src.splitlines() if ln.strip().startswith("log.")]
    assert log_lines and not any(
        "token" in ln.lower() or "api_key" in ln.lower() for ln in log_lines
    )


# --- the API client over real HTTP ------------------------------------------------------------


def test_the_fetch_sends_the_key_and_idempotency_header_and_reads_the_answer() -> None:
    from tests.unit.test_homeassistant import fake_server
    from wattmagent.integrations.publisher import make_fetch

    with fake_server() as fake:
        fake.reply = {"status": "planned"}
        got = make_fetch(fake.url, "api-key-123")("run it", "sched-dish-abc12345")
    assert got == {"status": "planned"}
    req = fake.requests[0]
    assert req["path"] == "/v1/plans" and req["body"] == {"text": "run it"}
    assert req["headers"]["x-api-key"] == "api-key-123"
    assert req["headers"]["idempotency-key"] == "sched-dish-abc12345"


def test_the_fetch_maps_errors_and_never_follows_a_redirect() -> None:
    from tests.unit.test_homeassistant import fake_server
    from wattmagent.integrations.publisher import make_fetch

    with fake_server() as fake:
        fake.status = 503
        with pytest.raises(PublishError, match="503") as caught:
            make_fetch(fake.url, "api-key-123")("x", "idem-key-1234")
        assert "api-key-123" not in str(caught.value)
    with fake_server() as target, fake_server() as fake:
        fake.redirect_to = target.url + "/steal"
        with pytest.raises(PublishError):
            make_fetch(fake.url, "api-key-123")("x", "idem-key-1234")
    assert target.requests == []
    with pytest.raises(PublishError, match="could not reach"):
        make_fetch("http://127.0.0.1:9", "api-key-123")("x", "idem-key-1234")
