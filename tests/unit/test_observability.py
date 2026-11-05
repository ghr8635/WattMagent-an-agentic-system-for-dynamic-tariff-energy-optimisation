"""Metrics rendering and log formatting. No web framework needed."""

from __future__ import annotations

import json
import logging
import re

import pytest

from wattmagent.api.metrics import (
    MAX_SERIES,
    ApiMetrics,
    Counter,
    GaugeFunc,
    Histogram,
    Registry,
)
from wattmagent.core.logsetup import JsonFormatter, TextFormatter, configure_logging

SAMPLE = re.compile(
    r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*",?)+\})? \S+$'
)


def assert_valid_exposition(text: str) -> None:
    """Every line is a HELP/TYPE comment or a well-formed sample; ends with a newline."""
    assert text.endswith("\n")
    for line in text.splitlines():
        assert line.startswith(("# HELP ", "# TYPE ")) or SAMPLE.match(line), line


# --------------------------------------------------------------------------- metrics


def test_counter_counts_per_label_set_and_renders() -> None:
    c = Counter("demo_total", "A demo.", ("route", "status"))
    c.inc("/a", "200")
    c.inc("/a", "200")
    c.inc("/b", "500", amount=3)
    assert c.value("/a", "200") == 2 and c.value("/b", "500") == 3 and c.value("/x", "1") == 0
    assert c.render() == [
        "# HELP demo_total A demo.",
        "# TYPE demo_total counter",
        'demo_total{route="/a",status="200"} 2',
        'demo_total{route="/b",status="500"} 3',
    ]


def test_unlabelled_counter_starts_at_zero() -> None:
    assert Counter("zero_total", "z").render()[-1] == "zero_total 0"


def test_counter_rejects_wrong_use() -> None:
    c = Counter("c_total", "c", ("a",))
    with pytest.raises(ValueError):
        c.inc()
    with pytest.raises(ValueError):
        c.inc("x", amount=-1)
    with pytest.raises(ValueError):
        Counter("bad name", "x")


def test_label_values_are_escaped() -> None:
    c = Counter("esc_total", "e", ("v",))
    c.inc('a"b\\c\nd')
    assert c.render()[-1] == 'esc_total{v="a\\"b\\\\c\\nd"} 1'
    assert_valid_exposition("\n".join(c.render()) + "\n")


def test_series_cap_ignores_new_label_sets_but_keeps_counting_old_ones() -> None:
    c = Counter("cap_total", "c", ("v",))
    for i in range(MAX_SERIES + 50):
        c.inc(str(i))
    assert len(c.render()) == 2 + MAX_SERIES  # HELP, TYPE and the capped samples
    c.inc("0")
    assert c.value("0") == 2


def test_histogram_buckets_are_cumulative_with_sum_and_count() -> None:
    h = Histogram("lat_seconds", "l", ("route",), buckets=(0.1, 1.0))
    for v in (0.05, 0.5, 5.0):
        h.observe(v, "/p")
    assert h.render()[2:] == [
        'lat_seconds_bucket{route="/p",le="0.1"} 1',
        'lat_seconds_bucket{route="/p",le="1"} 2',
        'lat_seconds_bucket{route="/p",le="+Inf"} 3',
        'lat_seconds_sum{route="/p"} 5.55',
        'lat_seconds_count{route="/p"} 3',
    ]


def test_histogram_refuses_reserved_label() -> None:
    with pytest.raises(ValueError):
        Histogram("h_seconds", "h", ("le",))


def test_gauge_without_a_value_has_no_sample() -> None:
    assert len(GaugeFunc("g", "g", lambda: None).render()) == 2
    assert GaugeFunc("g", "g", lambda: 1.5).render()[-1] == "g 1.5"
    assert (
        GaugeFunc("b", "b", lambda: 1.0, labels={"version": "1"}).render()[-1] == 'b{version="1"} 1'
    )


def test_registry_rejects_duplicate_names() -> None:
    reg = Registry()
    reg.register(Counter("dup_total", "d"))
    with pytest.raises(ValueError):
        reg.register(Counter("dup_total", "d"))


def test_api_metrics_render_is_valid_and_maps_unknown_methods() -> None:
    m = ApiMetrics("9.9.9")
    m.observe_request("POST", "/v1/plans", 200, 0.02)
    m.observe_request("BREW", "unmatched", 404, 0.001)
    m.plans.inc("planned", "optimised")
    m.price_age_fn = lambda: 2.5
    text = m.render()
    assert_valid_exposition(text)
    assert 'wattmagent_http_requests_total{method="POST",route="/v1/plans",status="200"} 1' in text
    assert 'wattmagent_http_requests_total{method="OTHER",route="unmatched",status="404"} 1' in text
    assert 'wattmagent_plan_results_total{status="planned",decision="optimised"} 1' in text
    assert "wattmagent_price_age_hours 2.5" in text
    assert 'wattmagent_build_info{version="9.9.9"} 1' in text
    assert "wattmagent_rate_limited_total 0" in text


# ------------------------------------------------------------------------------ logs


def record(**extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("wattmagent.api", logging.INFO, __file__, 1, "request", None, None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


def test_json_log_has_the_known_fields_only() -> None:
    line = JsonFormatter().format(
        record(request_id="r1", status=200, duration_ms=12, unlisted="do not log me")
    )
    data = json.loads(line)
    assert data["message"] == "request" and data["level"] == "INFO"
    assert data["request_id"] == "r1" and data["status"] == 200 and data["duration_ms"] == 12
    assert "unlisted" not in data and "do not log me" not in line


def test_text_log_appends_the_fields() -> None:
    line = TextFormatter().format(record(request_id="r1", status=401))
    assert line.endswith("request request_id=r1 status=401")


def test_configure_logging_is_repeatable_and_validates() -> None:
    logger = logging.getLogger("wattmagent")
    before = [h for h in logger.handlers if h.get_name() == "wattmagent-stream"]
    configure_logging("json", "warning")
    configure_logging("text", "INFO")
    ours = [h for h in logger.handlers if h.get_name() == "wattmagent-stream"]
    assert len(ours) == 1 and isinstance(ours[0].formatter, TextFormatter)
    for handler in ours:
        logger.removeHandler(handler)
    for handler in before:  # leave global state as found
        logger.addHandler(handler)
    with pytest.raises(ValueError):
        configure_logging("xml")
    with pytest.raises(ValueError):
        configure_logging("json", "loud")
