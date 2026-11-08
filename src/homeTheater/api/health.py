"""Health/readiness endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from .. import __version__
from ..config import get_config
from ..db import get_engine
from ..errors import redact_exc
from ..logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, Any]:
    """Liveness, plus whether the scheduled jobs are keeping up. Always 200: the
    process is up, and a stale backup is no reason for a monitor to restart it. A
    ``degraded`` status lists the jobs; job names are all this unauthenticated
    endpoint gives away, the detail is on the Status page."""

    from ..scheduler import job_freshness

    body: dict[str, Any] = {"status": "ok", "version": __version__}
    try:
        stale = [j.name for j in job_freshness(get_config()) if j.stale]
    except Exception as exc:  # job history is a bonus; never fail liveness over it
        log.warning("health.freshness_failed", error=redact_exc(exc))
        return body
    if stale:
        body.update(status="degraded", stale_jobs=stale)
    return body


@router.get("/ready", response_model=None)
def ready() -> dict[str, str] | JSONResponse:
    """Readiness: the process can reach its database. The reason for a failure goes
    to the log only: this endpoint is exempt from auth."""

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        log.error("ready.database_failed", error=redact_exc(exc))
        return JSONResponse({"status": "degraded", "database": "error"}, status_code=503)
    return {"status": "ok", "database": "ok"}
