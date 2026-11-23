"""Measured consumption from Home Assistant history: the hour arithmetic and a fake server."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_api_core import NOW, fresh_service
from wattmagent.api.service import UiError
from wattmagent.integrations import ha_history
from wattmagent.integrations.ha_history import (
    HistoryError,
    HomeAssistantHistory,
    hourly_kwh,
    parse_history,
    value_at,
)

TOKEN = "t0ken-" + "x" * 30
T0 = datetime(2026, 3, 10, 10, 0, tzinfo=UTC)
STARTS = [T0 + timedelta(hours=i) for i in range(4)]


def row(minutes: int, state: str) -> dict[str, str]:
    return {"state": state, "last_changed": (T0 + timedelta(minutes=minutes)).isoformat()}


# --- arithmetic -------------------------------------------------------------------------------


def test_hourly_energy_is_the_difference_of_the_meter_at_the_hour_edges() -> None:
    readings = parse_history(
        [[row(0, "100.0"), row(30, "100.4"), row(70, "101.0"), row(190, "102.5")]]
    )
    got = hourly_kwh(readings, STARTS, T0 + timedelta(hours=4))
    # 10-11: 100.0 -> 100.4 (the 11:10 reading is later); 11-12: 100.4 -> 101.0; 12-13: 1.0 -> same
    assert got == [0.4, 0.6, 0.0, pytest.approx(1.5)]


def test_an_hour_that_is_not_over_is_unknown() -> None:
    readings = parse_history([[row(0, "5"), row(100, "6")]])
    got = hourly_kwh(readings, STARTS[:2], T0 + timedelta(minutes=90))
    assert got == [0.0, None]  # 11:00-12:00 is not finished at 11:30


def test_no_reading_before_the_hour_means_unknown_not_zero() -> None:
    readings = parse_history([[row(90, "7"), row(150, "8")]])
    got = hourly_kwh(readings, STARTS, T0 + timedelta(hours=5))
    # nothing was known at 10:00 or 11:00; from 12:00 the meter read 7 and rose to 8 at 12:30
    assert got == [None, None, 1.0, 0.0]


def test_a_meter_that_goes_backwards_is_a_reset_not_negative_use() -> None:
    readings = parse_history([[row(0, "500"), row(30, "3"), row(90, "4")]])
    got = hourly_kwh(readings, STARTS[:2], T0 + timedelta(hours=3))
    assert got[0] is None  # 500 -> 3 within the hour
    assert got[1] == pytest.approx(1.0)


def test_watt_hours_are_converted() -> None:
    readings = parse_history([[row(0, "1000"), row(50, "1500")]])
    assert hourly_kwh(readings, STARTS[:1], T0 + timedelta(hours=2), unit_factor=0.001) == [0.5]


def test_unusable_states_are_skipped() -> None:
    payload = [
        [
            row(0, "10"),
            row(10, "unavailable"),
            row(20, "unknown"),
            row(30, "nan"),
            row(40, "11"),
            {"state": "12", "last_changed": "not a time"},
            {"state": "13", "last_changed": "2026-03-10T10:50:00"},
            "junk",
        ]
    ]
    got = parse_history(payload)
    assert [v for _, v in got] == [10.0, 11.0]  # naive times, nan, text and junk are all dropped


def test_readings_are_sorted_and_the_lookup_is_a_step() -> None:
    got = parse_history([[row(60, "2"), row(0, "1")]])
    assert [v for _, v in got] == [1.0, 2.0]
    assert value_at(got, T0 - timedelta(minutes=1)) is None
    assert value_at(got, T0) == 1.0 and value_at(got, T0 + timedelta(minutes=59)) == 1.0
    assert value_at(got, T0 + timedelta(hours=1)) == 2.0


@pytest.mark.parametrize("payload", [None, [], {}, "x"])
def test_an_empty_or_odd_reply_has_no_readings(payload: Any) -> None:
    assert parse_history(payload) == []


def test_a_reply_that_is_not_a_list_of_lists_is_an_error() -> None:
    with pytest.raises(HistoryError):
        parse_history([{"state": "1"}])


# --- the client over real HTTP ----------------------------------------------------------------


class Fake:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = 200
        self.reply: Any = []
        self.raw: bytes | None = None
        self.redirect_to: str | None = None
        self.url = ""


@contextmanager
def fake_server() -> Iterator[Fake]:
    state = Fake()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            state.requests.append(
                {
                    "method": "GET",
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
            )
            if state.redirect_to:
                self.send_response(302)
                self.send_header("Location", state.redirect_to)
                self.end_headers()
                return
            out = state.raw if state.raw is not None else json.dumps(state.reply).encode()
            self.send_response(state.status)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self) -> None:  # noqa: N802
            state.requests.append({"method": "POST", "path": self.path, "headers": {}})
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def client(url: str, **kw: Any) -> HomeAssistantHistory:
    return HomeAssistantHistory(url, TOKEN, kw.pop("entity", "sensor.grid_import"), **kw)


def test_one_get_for_one_entity_with_the_token_in_the_header() -> None:
    with fake_server() as fake:
        fake.reply = [[row(0, "100"), row(30, "100.5"), row(100, "101")]]
        got = client(fake.url).hourly_kwh(STARTS[:2], T0 + timedelta(hours=3))
    assert got == [0.5, 0.5]
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req["method"] == "GET" and req["path"].startswith(
        "/api/history/period/2026-03-10T10%3A00"
    )
    assert (
        "filter_entity_id=sensor.grid_import" in req["path"] and "minimal_response" in req["path"]
    )
    assert req["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_the_client_only_ever_reads() -> None:
    with fake_server() as fake:
        fake.reply = [[row(0, "1")]]
        client(fake.url).hourly_kwh(STARTS[:1], T0 + timedelta(hours=2))
    assert {r["method"] for r in fake.requests} == {"GET"}
    source = Path(ha_history.__file__).read_text(encoding="utf-8")
    assert "/api/services" not in source and "/api/states" not in source and "POST" not in source


@pytest.mark.parametrize("status", [401, 404, 500])
def test_an_error_answer_fails_without_leaking_the_token(status: int) -> None:
    with fake_server() as fake:
        fake.status = status
        with pytest.raises(HistoryError) as caught:
            client(fake.url).hourly_kwh(STARTS[:1], T0 + timedelta(hours=2))
    assert str(status) in str(caught.value) and TOKEN not in str(caught.value)


def test_a_redirect_is_not_followed() -> None:
    with fake_server() as target, fake_server() as fake:
        fake.redirect_to = target.url + "/steal"
        with pytest.raises(HistoryError):
            client(fake.url).hourly_kwh(STARTS[:1], T0 + timedelta(hours=2))
    assert target.requests == []  # the token never reached the other place


def test_invalid_json_and_an_unreachable_server_are_clean_errors() -> None:
    with fake_server() as fake:
        fake.raw = b"<html>not json"
        with pytest.raises(HistoryError, match="not valid JSON"):
            client(fake.url).hourly_kwh(STARTS[:1], T0 + timedelta(hours=2))
        url = fake.url
    with pytest.raises(HistoryError, match="could not reach"):
        client(url).hourly_kwh(STARTS[:1], T0 + timedelta(hours=2))


@pytest.mark.parametrize(
    "entity", ["light.kitchen", "sensor.", "sensor.A", "sensor.x/../../states", "sensor.a b", ""]
)
def test_only_plain_sensor_entities_are_accepted(entity: str) -> None:
    with pytest.raises(ValueError):
        client("http://ha.local:8123", entity=entity)


def test_bad_settings_are_refused_and_the_token_is_never_shown() -> None:
    with pytest.raises(ValueError):
        HomeAssistantHistory("http://ha.local:8123", "short", "sensor.x")
    with pytest.raises(ValueError):
        HomeAssistantHistory("ftp://ha.local", TOKEN, "sensor.x")
    with pytest.raises(ValueError):
        client("http://ha.local:8123", unit="MWh")
    assert TOKEN not in repr(client("http://ha.local:8123"))


def test_at_most_26_hours_are_read_at_a_time() -> None:
    with pytest.raises(ValueError):
        client("http://ha.local:8123").hourly_kwh([T0 + timedelta(hours=i) for i in range(27)], T0)
    with pytest.raises(ValueError):
        client("http://ha.local:8123").hourly_kwh([], T0)


# --- the service ------------------------------------------------------------------------------


class FakeMeter:
    description = "fake meter"

    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def hourly_kwh(self, starts: list[datetime], now: datetime) -> list[float | None]:
        self.calls += 1
        if self.fail:
            raise HistoryError("could not reach Home Assistant: URLError")
        return [0.25 if s + timedelta(hours=1) <= now else None for s in starts]


def test_the_service_gives_the_local_day_with_unknown_future_hours(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    svc.consumption = FakeMeter()
    got = svc.measured_day()
    assert got["date"] == "2026-03-10" and len(got["kwh"]) == 24 and got["source"] == "fake meter"
    # NOW is 18:20 Berlin: hours 0..17 are over
    assert got["kwh"][:18] == [0.25] * 18 and got["kwh"][18:] == [None] * 6
    assert got["hours_known"] == 18 and got["total_kwh"] == pytest.approx(4.5)


def test_the_answer_is_cached_for_a_minute(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    meter = FakeMeter()
    svc.consumption = meter
    svc.measured_day()
    svc.measured_day()
    assert meter.calls == 1
    svc.measured_day("tomorrow")
    assert meter.calls == 2  # another day is another question
    svc.clock = lambda: NOW + timedelta(minutes=2)
    svc.measured_day("tomorrow")
    assert meter.calls == 3


def test_not_configured_unreadable_and_bad_day_are_refused(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    with pytest.raises(UiError) as caught:
        svc.measured_day()
    assert caught.value.status_code == 404
    svc.consumption = FakeMeter(fail=True)
    with pytest.raises(UiError) as caught:
        svc.measured_day()
    assert caught.value.status_code == 502 and "could not be read" in str(caught.value)
    with pytest.raises(UiError) as caught:
        svc.measured_day("yesterday")
    assert caught.value.status_code == 422


def test_tomorrow_has_no_finished_hours(tmp_path: Path) -> None:
    svc = fresh_service(tmp_path)
    svc.consumption = FakeMeter()
    got = svc.measured_day("tomorrow")
    assert got["date"] == "2026-03-11" and got["hours_known"] == 0 and got["total_kwh"] == 0


def test_a_watt_hour_sensor_is_converted_to_kwh_through_the_client() -> None:
    with fake_server() as fake:
        fake.reply = [[row(0, "1000"), row(30, "1500")]]
        got = client(fake.url, unit="Wh").hourly_kwh(STARTS[:1], T0 + timedelta(hours=2))
    assert got == [0.5]
