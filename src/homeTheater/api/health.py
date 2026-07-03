"""Health/readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from .. import __version__
from ..db import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up."""

    return {"status": "ok", "version": __version__}


@router.get("/ready")
def ready() -> dict[str, str]:
    """Readiness: the process can reach its database."""

    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - surfaced as unhealthy
        return {"status": "degraded", "database": f"error: {exc}"}
    return {"status": "ok", "database": "ok"}
