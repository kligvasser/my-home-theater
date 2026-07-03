"""Subtitle coverage (read) + Bazarr search trigger (auth-gated)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_config
from ..dashboard import get_stats, list_missing_subtitles
from ..subtitles import sweep_missing
from .auth import require_token

router = APIRouter(prefix="/api/subtitles", tags=["subtitles"])


@router.get("/coverage")
def api_coverage() -> dict[str, Any]:
    cov = get_stats(sub_lang=get_config().subtitles.primary).coverage
    return {"lang": cov.lang, "covered": cov.covered, "total": cov.total, "pct": cov.pct}


@router.get("/missing")
def api_missing() -> dict[str, Any]:
    lang = get_config().subtitles.primary
    rows = list_missing_subtitles(lang=lang)
    return {"lang": lang, "count": len(rows), "items": [asdict(r) for r in rows]}


@router.post("/search", dependencies=[Depends(require_token)])
async def api_search() -> dict[str, Any]:
    """Ask Bazarr to search for all missing target-language subtitles."""

    try:
        stats = await sweep_missing(get_config())
    except ValueError as exc:  # Bazarr not configured
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return stats.as_dict()
