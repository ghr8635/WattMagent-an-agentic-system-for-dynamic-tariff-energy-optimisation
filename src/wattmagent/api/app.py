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
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from wattmagent import __version__
from wattmagent.api.idempotency import IdempotencyStore, Kind, StoredResponse
from wattmagent.api.ratelimit import RateLimiter
from wattmagent.api.security import KeyRing
from wattmagent.api.service import CsvContextProvider, PlanService, ServiceUnavailable
from wattmagent.core.config import Settings
from wattmagent.graph.request import MAX_REQUEST_CHARS
from wattmagent.safety.audit import InMemoryAuditLog, JsonlAuditLog

log = logging.getLogger("wattmagent.api")

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=MAX_REQUEST_CHARS)


def service_from_settings(settings: Settings) -> PlanService:
    if settings.price_csv is None:
        raise RuntimeError("WATTMAGENT_PRICE_CSV is not set: the API has no price data to plan on")
    provider = CsvContextProvider(settings.price_csv, ZoneInfo(settings.timezone))
    audit = JsonlAuditLog(settings.audit_log) if settings.audit_log else InMemoryAuditLog()
    llm = None
    if settings.language_backend == "llm":
        from wattmagent.llm.factory import build_client

        llm = build_client(settings)
    elif settings.language_backend != "rules":
        raise RuntimeError(f"unknown language backend: {settings.language_backend!r}")
    return PlanService(provider, audit=audit, llm=llm)


def create_app(
    settings: Settings | None = None,
    *,
    service: PlanService | None = None,
    limiter: RateLimiter | None = None,
    store: IdempotencyStore | None = None,
) -> FastAPI:
    """Build the app. Refuses to start without API keys. Tests inject ``service``."""
    cfg = settings or Settings()
    keys = KeyRing(cfg.api_key_set)  # raises if none or too short
    svc = service or service_from_settings(cfg)
    rate = limiter or RateLimiter(cfg.rate_limit_per_min)
    idem = store or IdempotencyStore(cfg.idempotency_ttl_s)

    app = FastAPI(title="WattMagent", version=__version__)

    @app.middleware("http")
    async def request_id_and_log(request: Request, call_next: Any) -> Response:
        inbound = request.headers.get("x-request-id", "")
        rid = inbound if _REQUEST_ID.match(inbound) else uuid.uuid4().hex
        request.state.request_id = rid
        started = time.monotonic()
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        log.info(
            "request_id=%s method=%s path=%s status=%s ms=%.0f key=%s",
            rid,
            request.method,
            request.url.path,
            response.status_code,
            (time.monotonic() - started) * 1000,
            getattr(request.state, "key_id", "-"),
        )
        return response

    def require_key(request: Request, x_api_key: Annotated[str | None, Header()] = None) -> str:
        who = keys.authenticate(x_api_key)
        if who is None:
            raise HTTPException(
                401, "missing or invalid API key", headers={"WWW-Authenticate": "ApiKey"}
            )
        request.state.key_id = who
        wait = rate.check(who)
        if wait > 0:
            raise HTTPException(
                429, "rate limit exceeded", headers={"Retry-After": str(math.ceil(wait))}
            )
        return who

    @app.exception_handler(ServiceUnavailable)
    async def unavailable(request: Request, exc: ServiceUnavailable) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "request_id": request.state.request_id}, status_code=503
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

    @app.post("/v1/plans")
    def create_plan(
        body: PlanRequest,
        request: Request,
        who: Annotated[str, Depends(require_key)],
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if idempotency_key is not None and not _IDEMPOTENCY_KEY.match(idempotency_key):
            raise HTTPException(
                422, "Idempotency-Key must be 8-128 characters of letters, digits, . _ -"
            )
        if idempotency_key is not None:
            fingerprint = hashlib.sha256(body.text.encode("utf-8")).hexdigest()
            begun = idem.begin(who, idempotency_key, fingerprint)
            if begun.kind is Kind.REPLAY and begun.stored is not None:
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
            status, payload = svc.plan(body.text, request.state.request_id)
        except BaseException:
            if idempotency_key is not None:
                idem.abort(who, idempotency_key)
            raise
        if idempotency_key is not None:
            idem.finish(who, idempotency_key, StoredResponse(status, payload))
        return JSONResponse(payload, status_code=status)

    return app
