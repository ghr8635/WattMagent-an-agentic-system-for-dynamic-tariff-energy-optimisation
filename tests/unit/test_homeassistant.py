"""Home Assistant advice mode, tested against a small fake server over real HTTP."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_appliance_agent import BATTERY_HOUSE, NOW, ctx, spread_prices
from wattmagent.appliances.agent import run_appliance_agent
from wattmagent.appliances.household import parse_household
from wattmagent.integrations import homeassistant as ha
from wattmagent.integrations.homeassistant import (
    Advice,
    Call,
    HomeAssistantPublisher,
    PublishError,
    build_advice,
)

TOKEN = "t0ken-" + "x" * 30  # the shape of a long-lived token; not a real one
PLAIN = parse_household({"cycle": [{"appliance": "dishwasher"}, {"appliance": "washing_machine"}]})


def body_for(text: str, household: Any, **kw: Any) -> dict[str, Any]:
    resp = run_appliance_agent(text, household, ctx(**kw), NOW)
    return {
        "status": resp.status,
        "message": resp.message,
        "decision": resp.decision,
        "facts": resp.facts,
        "request_id": resp.request_id,
    }


# --- a fake server ----------------------------------------------------------------------------


class Fake:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.reply: dict[str, Any] = {}
        self.redirect_to: str | None = None
        self.delay = 0.0
        self.url = ""


@contextmanager
def fake_server() -> Iterator[Fake]:
    state = Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802  (a followed redirect arrives as a GET)
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append({"path": self.path, "headers": headers, "body": None})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(raw),
                }
            )
            time.sleep(state.delay)
            if state.redirect_to:
                self.send_response(302)
                self.send_header("Location", state.redirect_to)
                self.end_headers()
                return
            out = json.dumps(state.reply).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args: object) -> None:  # keep the test output quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


# --- the advice document ----------------------------------------------------------------------


def test_a_planned_request_becomes_ready_advice_with_expiry() -> None:
    advice = build_advice(body_for("run the dishwasher before 8pm", PLAIN), NOW)
    assert advice.state == "ready"
    assert advice.attributes["actuation"] is False and advice.attributes["mode"] == "advice"
    (item,) = advice.items
    assert item["kind"] == "cycle" and item["appliance"] == "dishwasher"
    start, end = datetime.fromisoformat(item["start_at"]), datetime.fromisoformat(item["end_at"])
    assert start.utcoffset() is not None and end > start  # timezone-aware, real timestamps
    assert item["expires_at"] == item["start_at"]
    assert advice.attributes["valid_until"] == item["start_at"]
    assert item["saving_eur"] is None or item["saving_eur"] >= 0
    assert advice.attributes["request_id"]


def test_two_appliances_two_items_and_the_latest_start_sets_the_validity() -> None:
    body = body_for("run the dishwasher before 8pm and the washing machine by noon", PLAIN)
    advice = build_advice(body, NOW)
    starts = sorted(i["start_at"] for i in advice.items)
    assert len(advice.items) == 2 and advice.attributes["valid_until"] == starts[-1]
    assert not advice.expired(NOW)
    assert advice.expired(datetime.fromisoformat(starts[-1]) + timedelta(seconds=1))


def test_tank_and_battery_become_hour_items() -> None:
    body = body_for("run the dishwasher", BATTERY_HOUSE, prices=spread_prices())
    advice = build_advice(body, NOW)
    kinds = {(i["kind"], i.get("action")) for i in advice.items}
    assert ("battery", "charge") in kinds and ("hot_water_tank", "heat") in kinds
    for item in advice.items:
        if "hours" in item:
            assert item["hours"] == sorted(item["hours"])
            assert item["expires_at"] == item["hours"][-1]


def test_a_fallback_plan_is_labelled_fallback() -> None:
    advice = build_advice(body_for("run the dishwasher", PLAIN, age_h=100), NOW)
    assert advice.state == "fallback" and advice.attributes["decision"] == "fallback"
    assert advice.items  # still the safe default start, but not presented as optimised


@pytest.mark.parametrize(
    "body",
    [
        {"status": "needs_input", "message": "Which appliance?"},
        {"status": "error", "message": "I could not read that."},
        {"status": "planned", "decision": "optimised"},  # no facts at all
        {},
    ],
)
def test_anything_unplanned_gives_no_plan_and_no_items(body: dict[str, Any]) -> None:
    advice = build_advice(body, NOW)
    assert advice.state == "no_plan" and advice.items == []
    assert advice.expired(NOW)  # nothing to act on


def test_a_plan_without_items_is_no_plan() -> None:
    body = {"status": "planned", "decision": "optimised", "facts": {"cycles": []}}
    assert build_advice(body, NOW).state == "no_plan"


def test_naive_times_are_refused() -> None:
    with pytest.raises(ValueError):
        build_advice({"status": "planned"}, datetime(2025, 1, 3, 15, 0))
    cycle = {
        "appliance": "x", "start_at": "2025-01-03T20:00:00", "end_at": "2025-01-03T22:00:00",
        "energy_kwh": 1.0, "cost_eur": 0.1, "cost_if_started_at_window_start_eur": 0.2,
    }  # fmt: skip
    with pytest.raises(ValueError):
        build_advice(
            {"status": "planned", "decision": "optimised", "facts": {"cycles": [cycle]}}, NOW
        )


def test_the_number_of_items_is_capped() -> None:
    cycle = {
        "appliance": "x", "start_at": "2025-01-03T20:00:00+01:00",
        "end_at": "2025-01-03T22:00:00+01:00", "energy_kwh": 1.0, "cost_eur": None,
        "cost_if_started_at_window_start_eur": None,
    }  # fmt: skip
    body = {"status": "planned", "decision": "optimised", "facts": {"cycles": [cycle] * 100}}
    advice = build_advice(body, NOW)
    assert len(advice.items) == ha.MAX_ITEMS and advice.items[0]["saving_eur"] is None


# --- construction rules -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ha.local:8123", "ha.local:8123", "http://", "http://user:pw@ha.local:8123",
        "http://ha.local:8123?x=1", "http://ha.local:8123#frag", "",
    ],
)  # fmt: skip
def test_bad_home_assistant_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        HomeAssistantPublisher(url, TOKEN)


def test_good_urls_are_accepted_and_tidied() -> None:
    assert HomeAssistantPublisher("http://ha.local:8123/", TOKEN).base_url == "http://ha.local:8123"
    assert HomeAssistantPublisher("https://ha.example/x/", TOKEN).base_url == "https://ha.example/x"


@pytest.mark.parametrize(
    "entity",
    [
        "light.kitchen",
        "sensor.other",
        "sensor.wattmagent_",
        "sensor.wattmagent_A",
        "sensor.wattmagent_a/b",
    ],
)
def test_only_our_own_entities_may_be_written(entity: str) -> None:
    with pytest.raises(ValueError):
        HomeAssistantPublisher("http://ha.local:8123", TOKEN, entity_id=entity)


@pytest.mark.parametrize(
    "event", ["homeassistant_stop", "wattmagent_", "wattmagent_a b", "call_service"]
)
def test_only_our_own_events_may_be_fired(event: str) -> None:
    with pytest.raises(ValueError):
        HomeAssistantPublisher("http://ha.local:8123", TOKEN, event_type=event)


def test_a_short_token_is_refused_and_the_token_is_never_shown() -> None:
    with pytest.raises(ValueError):
        HomeAssistantPublisher("http://ha.local:8123", "short")
    publisher = HomeAssistantPublisher("http://ha.local:8123", TOKEN)
    assert TOKEN not in repr(publisher) and TOKEN not in str(publisher)


# --- delivery ---------------------------------------------------------------------------------


def advice_for_test() -> Advice:
    return build_advice(body_for("run the dishwasher", PLAIN), NOW)


def test_a_dry_run_sends_nothing() -> None:
    with fake_server() as server:
        calls = HomeAssistantPublisher(server.url, TOKEN).publish(advice_for_test())
        assert server.requests == []
    assert [c.path for c in calls] == [
        "/api/states/sensor.wattmagent_advice",
        "/api/events/wattmagent_advice",
    ]


def test_publishing_sends_exactly_the_two_advice_calls() -> None:
    advice = advice_for_test()
    with fake_server() as server:
        HomeAssistantPublisher(server.url, TOKEN).publish(advice, dry_run=False)
        paths = [r["path"] for r in server.requests]
        first = server.requests[0]
    assert paths == ["/api/states/sensor.wattmagent_advice", "/api/events/wattmagent_advice"]
    assert first["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert (
        first["body"]["state"] == "ready" and first["body"]["attributes"]["items"] == advice.items
    )
    assert not any("/api/services" in p for p in paths)  # advice mode never calls a service


def test_the_token_is_only_in_the_header_never_in_what_is_sent() -> None:
    advice = advice_for_test()
    publisher = HomeAssistantPublisher("http://ha.local:8123", TOKEN)
    assert TOKEN not in json.dumps([c.body for c in publisher.plan_calls(advice)])
    with fake_server() as server:
        HomeAssistantPublisher(server.url, TOKEN).publish(advice, dry_run=False)
        for request in server.requests:
            assert TOKEN not in json.dumps(request["body"])
            assert request["headers"]["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("status", [400, 401, 404, 500])
def test_an_error_answer_fails_without_leaking_the_token(status: int) -> None:
    with fake_server() as server:
        server.status = status
        with pytest.raises(PublishError) as caught:
            HomeAssistantPublisher(server.url, TOKEN).publish(advice_for_test(), dry_run=False)
    assert str(status) in str(caught.value) and TOKEN not in str(caught.value)


def test_a_redirect_is_not_followed() -> None:
    with fake_server() as server, fake_server() as elsewhere:
        server.redirect_to = elsewhere.url + "/steal"
        with pytest.raises(PublishError) as caught:
            HomeAssistantPublisher(server.url, TOKEN).publish(advice_for_test(), dry_run=False)
        assert elsewhere.requests == []  # the token never went to the other place
        assert len(server.requests) == 1 and TOKEN not in str(caught.value)


def test_an_unreachable_server_is_a_clean_error() -> None:
    with fake_server() as server:
        url = server.url
    with pytest.raises(PublishError) as caught:
        HomeAssistantPublisher(url, TOKEN).publish(advice_for_test(), dry_run=False)
    assert "could not reach" in str(caught.value) and TOKEN not in str(caught.value)


def test_a_slow_server_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ha, "TIMEOUT_S", 0.3)
    with fake_server() as server:
        server.delay = 1.5
        with pytest.raises(PublishError):
            HomeAssistantPublisher(server.url, TOKEN).publish(advice_for_test(), dry_run=False)


@pytest.mark.parametrize(
    "call",
    [
        Call("POST", "/api/services/light/turn_on", {}),
        Call("POST", "/api/states/light.kitchen", {}),
        Call("POST", "/api/events/homeassistant_stop", {}),
        Call("GET", "/api/states/sensor.wattmagent_advice", {}),
        Call("POST", "/api/states/sensor.wattmagent_advice/../../services/x", {}),
    ],
)
def test_nothing_outside_advice_mode_can_be_sent(call: Call) -> None:
    with fake_server() as server:
        with pytest.raises(PublishError, match="outside advice mode"):
            HomeAssistantPublisher(server.url, TOKEN)._send(call)
        assert server.requests == []


def test_the_module_never_mentions_the_services_endpoint() -> None:
    text = Path(ha.__file__).read_text(encoding="utf-8")
    assert "/api/services" not in text and "services/" not in text


# --- command line -----------------------------------------------------------------------------


def saved_answer(tmp_path: Path) -> Path:
    path = tmp_path / "answer.json"
    path.write_text(json.dumps(body_for("run the dishwasher before 8pm", PLAIN)), encoding="utf-8")
    return path


def test_the_command_line_is_a_dry_run_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ha.main(["--from-file", str(saved_answer(tmp_path))], env={"WATTMAGENT_HA_TOKEN": TOKEN})
    out = capsys.readouterr().out
    assert code == 0 and "DRY RUN (nothing was sent)" in out and "state = ready" in out
    assert "/api/states/sensor.wattmagent_advice" in out and TOKEN not in out


def test_publish_needs_a_token_and_a_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ha.main(["--from-file", str(saved_answer(tmp_path)), "--publish"], env={})
    assert code == 2 and "WATTMAGENT_HA_TOKEN" in capsys.readouterr().err


def test_publish_through_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with fake_server() as server:
        code = ha.main(
            ["--from-file", str(saved_answer(tmp_path)), "--publish", "--ha-url", server.url],
            env={"WATTMAGENT_HA_TOKEN": TOKEN},
        )
        assert code == 0 and len(server.requests) == 2
    out = capsys.readouterr().out
    assert "PUBLISHED" in out and TOKEN not in out


def test_a_failed_publish_exits_with_one_and_hides_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with fake_server() as server:
        server.status = 401
        code = ha.main(
            ["--from-file", str(saved_answer(tmp_path)), "--publish", "--ha-url", server.url],
            env={"WATTMAGENT_HA_TOKEN": TOKEN},
        )
    captured = capsys.readouterr()
    assert code == 1 and "401" in captured.err and TOKEN not in captured.out + captured.err


def test_the_command_line_can_ask_the_wattmagent_api(capsys: pytest.CaptureFixture[str]) -> None:
    with fake_server() as api:
        api.reply = body_for("run the dishwasher", PLAIN)
        code = ha.main(
            ["--text", "run the dishwasher", "--api-url", api.url],
            env={"WATTMAGENT_ADVICE_API_KEY": "k" * 20},
        )
        assert code == 0
        assert api.requests[0]["path"] == "/v1/plans"
        assert api.requests[0]["headers"]["x-api-key"] == "k" * 20
        assert api.requests[0]["body"] == {"text": "run the dishwasher"}
    assert "state = ready" in capsys.readouterr().out


def test_asking_the_api_needs_a_key_and_reports_api_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert ha.main(["--text", "x"], env={}) == 2
    with fake_server() as api:
        api.status = 503
        code = ha.main(
            ["--text", "x", "--api-url", api.url], env={"WATTMAGENT_ADVICE_API_KEY": "k" * 20}
        )
    assert code == 1 and "503" in capsys.readouterr().err


def test_the_command_line_rejects_a_bad_saved_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ha.main(["--from-file", str(bad)], env={}) == 1
    assert ha.main(["--from-file", str(tmp_path / "missing.json")], env={}) == 1
    assert "error:" in capsys.readouterr().err
