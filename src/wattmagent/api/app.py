"""HTTP API: plain-language charging request in, verified plan out.

Order of checks on ``POST /v1/plans``: API key, rate limit, body validation, idempotency,
then the agent. Every response carries ``X-Request-ID`` (an inbound id is kept if it is
safe, otherwise one is generated). Planning itself is in ``service.py``.

This module deliberately has no ``from __future__ import annotations``: FastAPI resolves
the annotations of the handlers defined inside ``create_app`` at definition time.
"""

import hashlib
import logging
import math
import re
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from wattmagent import __version__
from wattmagent.api.idempotency import IdempotencyStore, Kind, StoredResponse
from wattmagent.api.metrics import ApiMetrics
from wattmagent.api.plans import PlanStore
from wattmagent.api.ratelimit import RateLimiter
from wattmagent.api.security import KeyRing
from wattmagent.api.service import (
    ContextProvider,
    CsvContextProvider,
    LiveContextProvider,
    PlanService,
    ServiceUnavailable,
)
from wattmagent.core.config import Settings
from wattmagent.core.errors import ReplanError
from wattmagent.core.logsetup import configure_logging
from wattmagent.graph.request import MAX_REQUEST_CHARS
from wattmagent.safety.audit import InMemoryAuditLog, JsonlAuditLog

log = logging.getLogger("wattmagent.api")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_REQUEST_CHARS)


class ReplanRequest(BaseModel):
    """What the client knows now: the car's real charge, and optionally the battery's."""

    model_config = ConfigDict(extra="forbid")
    current_percent: float = Field(ge=0, le=100)
    battery_percent: float | None = Field(default=None, ge=0, le=100)


def service_from_settings(settings: Settings) -> PlanService:
    tz = ZoneInfo(settings.timezone)
    provider: ContextProvider
    if settings.price_source == "smard":
        from wattmagent.data.smard_live import HttpxFetch

        provider = LiveContextProvider(HttpxFetch(), tz, refresh_s=settings.price_refresh_s)
    elif settings.price_source == "csv":
        if settings.price_csv is None:
            raise RuntimeError("WATTMAGENT_PRICE_CSV is not set: no price data to plan on")
        provider = CsvContextProvider(settings.price_csv, tz)
    else:
        raise RuntimeError(f"unknown price source: {settings.price_source!r}")
    audit = JsonlAuditLog(settings.audit_log) if settings.audit_log else InMemoryAuditLog()
    llm = None
    if settings.language_backend == "llm":
        from wattmagent.llm.factory import build_client

        llm = build_client(settings)
    elif settings.language_backend != "rules":
        raise RuntimeError(f"unknown language backend: {settings.language_backend!r}")
    household = None
    if settings.household_file is not None:
        from wattmagent.appliances.catalogue import ConfigError
        from wattmagent.appliances.household import load_household

        try:
            household = load_household(settings.household_file)
        except (ConfigError, OSError) as exc:
            raise RuntimeError(f"WATTMAGENT_HOUSEHOLD_FILE cannot be used: {exc}") from exc
    return PlanService(
        provider, audit=audit, llm=llm, store=PlanStore(settings.plan_ttl_s), household=household
    )


def create_app(
    settings: Settings | None = None,
    *,
    service: PlanService | None = None,
    limiter: RateLimiter | None = None,
    store: IdempotencyStore | None = None,
    metrics: ApiMetrics | None = None,
) -> FastAPI:
    """Build the app. Refuses to start without API keys. Tests inject ``service``."""
    cfg = settings or Settings()
    configure_logging(cfg.log_format, cfg.log_level)
    keys = KeyRing(cfg.api_key_set)  # raises if none or too short
    svc = service or service_from_settings(cfg)
    rate = limiter or RateLimiter(cfg.rate_limit_per_min)
    idem = store or IdempotencyStore(cfg.idempotency_ttl_s)
    meter = metrics or ApiMetrics(__version__)
    meter.price_age_fn = svc.price_age_hours

    app = FastAPI(title="WattMagent", version=__version__)

    @app.middleware("http")
    async def request_id_and_log(request: Request, call_next: Any) -> Response:
        inbound = request.headers.get("x-request-id", "")
        rid = inbound if _REQUEST_ID.match(inbound) else uuid.uuid4().hex
        request.state.request_id = rid
        started = time.monotonic()
        status = 500  # what a client sees if the app raises past every handler
        try:
            response: Response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            elapsed = time.monotonic() - started
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"
            meter.observe_request(request.method, route, status, elapsed)
            log.info(
                "request",
                extra={
                    "request_id": rid,
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round(elapsed * 1000),
                    "key_id": getattr(request.state, "key_id", "-"),
                },
            )

    def require_key(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
        who = keys.authenticate(x_api_key)
        if who is None:
            meter.auth_failures.inc()
            raise HTTPException(
                401, "missing or invalid API key", headers={"WWW-Authenticate": "ApiKey"}
            )
        request.state.key_id = who
        wait = rate.check(who)
        if wait > 0:
            meter.rate_limited.inc()
            raise HTTPException(
                429, "rate limit exceeded", headers={"Retry-After": str(math.ceil(wait))}
            )
        return who

    @app.exception_handler(ServiceUnavailable)
    async def unavailable(request: Request, exc: ServiceUnavailable) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "request_id": request.state.request_id}, status_code=503
        )

    @app.exception_handler(ReplanError)
    async def replan_refused(request: Request, exc: ReplanError) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "request_id": request.state.request_id},
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "-")
        log.exception("unhandled error request_id=%s", rid)
        return JSONResponse(
            {"detail": "internal error", "request_id": rid},
            status_code=500,
            headers={"X-Request-ID": rid},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. No dependencies are checked."""
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        """Readiness: prices are present, fresh and cover the planning window."""
        reasons = svc.ready()
        if reasons:
            return JSONResponse({"status": "not_ready", "reasons": reasons}, status_code=503)
        return JSONResponse({"status": "ready"})

    def idempotent(
        who: str,
        key: str | None,
        fingerprint: str,
        run: Callable[[], tuple[int, dict[str, Any]]],
    ) -> JSONResponse:
        """Run ``run`` once per (caller, Idempotency-Key); a repeat gets the stored answer."""
        if key is not None and not _IDEMPOTENCY_KEY.match(key):
            raise HTTPException(
                422, "Idempotency-Key must be 8-128 characters of letters, digits, . _ -"
            )
        if key is not None:
            begun = idem.begin(who, key, fingerprint)
            if begun.kind is Kind.REPLAY and begun.stored is not None:
                meter.replays.inc()
                return JSONResponse(
                    begun.stored.body,
                    status_code=begun.stored.status_code,
                    headers={"Idempotent-Replay": "true"},
                )
            if begun.kind is Kind.CONFLICT:
                raise HTTPException(
                    409, "Idempotency-Key was already used with a different request"
                )
            if begun.kind is Kind.IN_FLIGHT:
                raise HTTPException(409, "a request with this Idempotency-Key is still running")
        try:
            status, payload = run()
        except BaseException:
            if key is not None:
                idem.abort(who, key)  # nothing was stored: the client may retry
            raise
        if key is not None:
            idem.finish(who, key, StoredResponse(status, payload))
        return JSONResponse(payload, status_code=status)

    @app.post("/v1/plans")
    def create_plan(
        body: PlanRequest,
        request: Request,
        who: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        def run() -> tuple[int, dict[str, Any]]:
            status, payload = svc.plan(body.text, request.state.request_id, owner=who)
            meter.plans.inc(str(payload["status"]), str(payload.get("decision") or "none"))
            return status, payload

        fingerprint = hashlib.sha256(body.text.encode("utf-8")).hexdigest()
        return idempotent(who, idempotency_key, fingerprint, run)

    @app.post("/v1/plans/{plan_id}/replan")
    def replan_plan(
        plan_id: str,
        body: ReplanRequest,
        request: Request,
        who: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not _PLAN_ID.match(plan_id):
            meter.replans.inc("not_found")
            raise HTTPException(404, "unknown or expired plan")

        def run() -> tuple[int, dict[str, Any]]:
            try:
                status, payload = svc.replan(
                    plan_id,
                    who,
                    body.current_percent,
                    body.battery_percent,
                    request.state.request_id,
                )
            except ReplanError as exc:
                meter.replans.inc("not_found" if exc.status_code == 404 else "rejected")
                raise
            meter.replans.inc("planned" if payload["status"] == "planned" else "needs_attention")
            return status, payload

        fingerprint = hashlib.sha256(
            f"{plan_id}|{body.current_percent}|{body.battery_percent}".encode()
        ).hexdigest()
        return idempotent(who, idempotency_key, fingerprint, run)

    @app.get("/metrics")
    def metrics_endpoint() -> Response:
        """Prometheus scrape target. Keep it off the public proxy (see deploy/Caddyfile)."""
        return Response(meter.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    return app
