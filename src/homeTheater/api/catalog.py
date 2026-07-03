"""JSON API over the catalog + run history (read-only)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Query

from ..dashboard import get_stats, list_titles, recent_runs

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/stats")
def api_stats() -> dict[str, Any]:
    stats = get_stats()
    data = asdict(stats)
    data["coverage"]["pct"] = stats.coverage.pct  # property isn't captured by asdict
    return data


@router.get("/titles")
def api_titles(
    q: str | None = None,
    kind: str | None = None,
    page: int = Query(1, ge=1),
) -> dict[str, Any]:
    rows, total = list_titles(q=q, kind=kind, page=page)
    return {"total": total, "page": page, "items": [asdict(r) for r in rows]}


@router.get("/runs")
def api_runs(limit: int = Query(25, ge=1, le=200)) -> list[dict[str, Any]]:
    return [asdict(r) for r in recent_runs(limit)]
