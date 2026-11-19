"""The alert rules may only use metrics the API really exports, and compose must wire them up."""

from __future__ import annotations

import re
from pathlib import Path

from wattmagent.api.metrics import ApiMetrics

ROOT = Path(__file__).resolve().parents[2]
MON = ROOT / "deploy" / "monitoring"


def exported_names() -> set[str]:
    """Metric names from the live /metrics text, plus the _bucket/_sum/_count of histograms."""
    text = ApiMetrics("test").render()
    names = set(re.findall(r"^# TYPE (\S+) (?:counter|gauge|histogram)", text, flags=re.M))
    for histogram in re.findall(r"^# TYPE (\S+) histogram", text, flags=re.M):
        names |= {f"{histogram}_bucket", f"{histogram}_sum", f"{histogram}_count"}
    return names


def test_every_metric_in_the_alert_rules_is_exported() -> None:
    used = set(re.findall(r"\bwattmagent_[a-z_]+", (MON / "alerts.yml").read_text("utf-8")))
    assert used, "no metrics found in alerts.yml"
    assert used <= exported_names(), used - exported_names()


def test_the_alert_tests_only_use_exported_metrics() -> None:
    used = set(re.findall(r"\bwattmagent_[a-z_]+", (MON / "alerts_test.yml").read_text("utf-8")))
    assert used <= exported_names(), used - exported_names()


def test_every_alert_has_a_severity_and_a_summary_and_a_wait() -> None:
    text = (MON / "alerts.yml").read_text("utf-8")
    alerts = re.split(r"^\s*- alert: ", text, flags=re.M)[1:]
    assert len(alerts) >= 6
    for block in alerts:
        name = block.splitlines()[0]
        assert re.search(r"severity: (warning|critical)", block), name
        assert "summary:" in block, name
        assert re.search(r"^\s+for: \d+m", block, flags=re.M), name  # no flapping on one sample


def test_every_alert_has_a_unit_test() -> None:
    rules = set(re.findall(r"- alert: (\w+)", (MON / "alerts.yml").read_text("utf-8")))
    tested = set(re.findall(r"alertname: (\w+)", (MON / "alerts_test.yml").read_text("utf-8")))
    # ApiDown, SlowPlans and AuthFailuresSpike are not unit-tested yet: listed, not hidden
    assert {"PricesStale", "FallbackRateHigh", "ServerErrors"} <= tested <= rules


def test_prometheus_scrapes_the_api_service_and_loads_the_rules() -> None:
    prom = (MON / "prometheus.yml").read_text("utf-8")
    compose = (ROOT / "deploy" / "compose.yaml").read_text("utf-8")
    assert "alerts.yml" in prom and "/metrics" in prom
    assert "api:8000" in prom and re.search(r"^  api:", compose, flags=re.M)
    assert "profiles: [monitoring]" in compose  # off unless asked for
    assert '"127.0.0.1:9090:9090"' in compose  # the UI is not exposed to the internet
    assert "prometheus:v2.55.1" in compose and "prometheus:v2.55.1" in (
        ROOT / ".github" / "workflows" / "ci.yaml"
    ).read_text("utf-8")
