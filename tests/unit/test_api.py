"""HTTP behaviour of the API. Skipped where FastAPI or httpx is not installed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from tests.unit.test_api_core import KEY, NOW, TEXT, fresh_service  # noqa: E402
from wattmagent.api.app import create_app  # noqa: E402
from wattmagent.api.ratelimit import RateLimiter  # noqa: E402
from wattmagent.api.service import CsvContextProvider, PlanService  # noqa: E402
from wattmagent.core.config import Settings  # noqa: E402
from wattmagent.safety.audit import InMemoryAuditLog  # noqa: E402

AUTH = {"X-API-Key": KEY}


def settings() -> Settings:
    return Settings(api_keys=KEY, _env_file=None)


def client(service: PlanService, limiter: RateLimiter | None = None, **kw: Any) -> TestClient:
    return TestClient(create_app(settings(), service=service, limiter=limiter), **kw)


def test_health_and_ready(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    assert c.get("/healthz").json()["status"] == "ok"
    assert c.get("/readyz").json() == {"status": "ready"}


def test_not_ready_when_prices_are_missing(tmp_path: Path) -> None:
    provider = CsvContextProvider(tmp_path / "nope.csv", ZoneInfo("Europe/Berlin"))
    c = client(PlanService(provider, clock=lambda: NOW))
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json()["status"] == "not_ready"


def test_plans_require_a_valid_key(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    assert c.post("/v1/plans", json={"text": TEXT}).status_code == 401
    assert (
        c.post("/v1/plans", json={"text": TEXT}, headers={"X-API-Key": "wrong"}).status_code == 401
    )


def test_plan_succeeds_and_carries_a_request_id(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    r = c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "planned" and body["decision"] == "optimised"
    assert r.headers["X-Request-ID"] == body["request_id"]


def test_inbound_request_id_is_kept_only_if_safe(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    ok = c.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert ok.headers["X-Request-ID"] == "trace-123"
    bad = c.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert bad.headers["X-Request-ID"] != "bad id with spaces"


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "x" * 501}, {"text": "a", "x": 1}])
def test_invalid_bodies_are_rejected(tmp_path: Path, payload: dict[str, Any]) -> None:
    c = client(fresh_service(tmp_path))
    assert c.post("/v1/plans", json=payload, headers=AUTH).status_code == 422


def test_unclear_request_returns_questions(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    r = c.post("/v1/plans", json={"text": "charge my car"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "needs_input"


def test_rate_limit_returns_429_with_retry_after(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path), limiter=RateLimiter(2))
    for _ in range(2):
        assert c.post("/v1/plans", json={"text": TEXT}, headers=AUTH).status_code == 200
    r = c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1


def test_idempotency_replays_instead_of_planning_twice(tmp_path: Path) -> None:
    audit = InMemoryAuditLog()
    c = client(fresh_service(tmp_path, audit))
    headers = {**AUTH, "Idempotency-Key": "booking-0001"}
    first = c.post("/v1/plans", json={"text": TEXT}, headers=headers)
    second = c.post("/v1/plans", json={"text": TEXT}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json() == first.json()
    assert len(audit.entries) == 1  # the agent ran once


def test_idempotency_key_reused_for_another_request_is_a_conflict(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    headers = {**AUTH, "Idempotency-Key": "booking-0002"}
    assert c.post("/v1/plans", json={"text": TEXT}, headers=headers).status_code == 200
    r = c.post("/v1/plans", json={"text": "80% by 7am"}, headers=headers)
    assert r.status_code == 409


def test_malformed_idempotency_key_is_rejected(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    r = c.post("/v1/plans", json={"text": TEXT}, headers={**AUTH, "Idempotency-Key": "short"})
    assert r.status_code == 422


def test_unreadable_prices_give_503(tmp_path: Path) -> None:
    provider = CsvContextProvider(tmp_path / "nope.csv", ZoneInfo("Europe/Berlin"))
    c = client(PlanService(provider, clock=lambda: NOW))
    r = c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)
    assert r.status_code == 503 and "request_id" in r.json()


def test_unexpected_error_gives_500_without_details(tmp_path: Path) -> None:
    class Boom(PlanService):
        def plan(self, text: str, request_id: str, owner: str = "") -> tuple[int, dict[str, Any]]:
            raise RuntimeError("secret internals")

    base = fresh_service(tmp_path)
    c = client(Boom(base.provider, clock=lambda: NOW), raise_server_exceptions=False)
    r = c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)
    assert r.status_code == 500
    assert "secret" not in r.text and r.json()["detail"] == "internal error"


def test_app_refuses_to_start_without_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_app(Settings(api_keys="", _env_file=None), service=fresh_service(tmp_path))


# ------------------------------------------------------------------------- metrics


def scrape(c: TestClient) -> str:
    r = c.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    return r.text


def test_metrics_count_requests_by_route_template_and_outcome(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)
    c.get("/does-not-exist/with-secret-looking-part")
    text = scrape(c)
    assert 'wattmagent_http_requests_total{method="POST",route="/v1/plans",status="200"} 1' in text
    assert 'route="unmatched",status="404"' in text
    assert "secret-looking" not in text  # request paths never become labels
    assert 'wattmagent_plan_results_total{status="planned",decision="optimised"} 1' in text
    assert "wattmagent_http_request_duration_seconds_count" in text


def test_metrics_count_auth_failures_replays_and_rate_limits(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path), limiter=RateLimiter(3))
    c.post("/v1/plans", json={"text": TEXT})  # no key: 401, not counted by the limiter
    headers = {**AUTH, "Idempotency-Key": "metrics-0001"}
    for _ in range(2):
        c.post("/v1/plans", json={"text": TEXT}, headers=headers)  # one plan, one replay
    c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)  # third allowed call
    c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)  # over the limit: 429
    text = scrape(c)
    assert "wattmagent_auth_failures_total 1" in text
    assert "wattmagent_idempotent_replays_total 1" in text
    assert "wattmagent_rate_limited_total 1" in text


def test_price_age_gauge_appears_once_prices_were_used(tmp_path: Path) -> None:
    sample = re.compile(r"^wattmagent_price_age_hours ", re.MULTILINE)  # a value, not HELP/TYPE
    c = client(fresh_service(tmp_path))
    assert sample.search(scrape(c)) is None
    c.get("/readyz")
    assert sample.search(scrape(c)) is not None


def test_server_errors_are_counted_as_500(tmp_path: Path) -> None:
    class Boom(PlanService):
        def plan(self, text: str, request_id: str, owner: str = "") -> tuple[int, dict[str, Any]]:
            raise RuntimeError("boom")

    base = fresh_service(tmp_path)
    c = client(Boom(base.provider, clock=lambda: NOW), raise_server_exceptions=False)
    assert c.post("/v1/plans", json={"text": TEXT}, headers=AUTH).status_code == 500
    assert 'route="/v1/plans",status="500"} 1' in scrape(c)


# ------------------------------------------------------------------------- replanning


def plan_id_of(c: TestClient) -> str:
    r = c.post("/v1/plans", json={"text": TEXT}, headers=AUTH)
    assert r.status_code == 200
    pid: str = r.json()["plan_id"]
    return pid


def test_replan_returns_a_new_version_and_what_it_replaced(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    pid = plan_id_of(c)
    r = c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 60}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["plan_id"] == pid and body["version"] == 2 and body["previous"]["version"] == 1
    assert r.headers["X-Request-ID"] == body["request_id"]


def test_replan_of_an_unknown_plan_is_404(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    for pid in ("0" * 32, "not-a-plan-id"):
        r = c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 50}, headers=AUTH)
        assert r.status_code == 404


def test_another_key_cannot_replan_my_plan(tmp_path: Path) -> None:
    other = "o" * 20
    app = create_app(
        Settings(api_keys=f"{KEY},{other}", _env_file=None), service=fresh_service(tmp_path)
    )
    c = TestClient(app)
    pid = plan_id_of(c)
    r = c.post(
        f"/v1/plans/{pid}/replan", json={"current_percent": 50}, headers={"X-API-Key": other}
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"current_percent": -1},
        {"current_percent": 101},
        {"current_percent": 50, "x": 1},
        {"current_percent": 50, "battery_percent": 120},
    ],
)
def test_replan_rejects_bad_bodies(tmp_path: Path, payload: dict[str, Any]) -> None:
    c = client(fresh_service(tmp_path))
    pid = plan_id_of(c)
    assert c.post(f"/v1/plans/{pid}/replan", json=payload, headers=AUTH).status_code == 422


def test_replan_needs_a_key(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    pid = plan_id_of(c)
    assert c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 50}).status_code == 401


def test_replan_is_idempotent(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    pid = plan_id_of(c)
    headers = {**AUTH, "Idempotency-Key": "replan-0001"}
    first = c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 60}, headers=headers)
    again = c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 60}, headers=headers)
    assert again.headers["Idempotent-Replay"] == "true" and again.json() == first.json()
    assert first.json()["version"] == 2  # replayed, not replanned to version 3
    other = c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 70}, headers=headers)
    assert other.status_code == 409


def test_replan_outcomes_are_counted(tmp_path: Path) -> None:
    c = client(fresh_service(tmp_path))
    pid = plan_id_of(c)
    c.post(f"/v1/plans/{pid}/replan", json={"current_percent": 60}, headers=AUTH)
    c.post(f"/v1/plans/{'0' * 32}/replan", json={"current_percent": 60}, headers=AUTH)
    text = scrape(c)
    assert 'wattmagent_replans_total{outcome="planned"} 1' in text
    assert 'wattmagent_replans_total{outcome="not_found"} 1' in text
    assert 'route="/v1/plans/{plan_id}/replan"' in text  # the template, never the id
