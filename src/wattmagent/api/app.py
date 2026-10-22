"""Minimal API: health check only. Auth, planning endpoints and streaming come later."""

from __future__ import annotations

from fastapi import FastAPI

from wattmagent import __version__

app = FastAPI(title="WattMagent", version=__version__)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
