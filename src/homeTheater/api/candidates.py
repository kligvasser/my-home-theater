"""Candidate queue: read (open) + review actions (auth-gated)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..acquisition import queue_candidate
from ..config import get_config
from ..dashboard import candidate_counts, list_candidates
from ..db.models import TitleKind
from ..discovery.actions import add_manual, approve, reject
from .auth import require_token

router = APIRouter(prefix="/api/candidates", tags=["candidates"])


class ManualAdd(BaseModel):
    tmdb_id: int
    kind: TitleKind = TitleKind.movie


@router.get("")
def api_list(status: str | None = "new", limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    return {
        "counts": candidate_counts(),
        "items": [asdict(c) for c in list_candidates(status=status, limit=limit)],
    }


@router.post("/{candidate_id}/approve", dependencies=[Depends(require_token)])
def api_approve(candidate_id: int) -> dict[str, str]:
    if not approve(candidate_id):
        raise HTTPException(status_code=404, detail="candidate not found")
    return {"status": "approved"}


@router.post("/{candidate_id}/reject", dependencies=[Depends(require_token)])
def api_reject(candidate_id: int) -> dict[str, str]:
    if not reject(candidate_id):
        raise HTTPException(status_code=404, detail="candidate not found")
    return {"status": "rejected"}


@router.post("/manual", dependencies=[Depends(require_token)])
async def api_manual(body: ManualAdd) -> dict[str, int | str]:
    try:
        candidate_id = await add_manual(get_config(), body.tmdb_id, body.kind)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": candidate_id, "status": "new"}


@router.post("/{candidate_id}/queue", dependencies=[Depends(require_token)])
async def api_queue(candidate_id: int) -> dict[str, Any]:
    """Hand a candidate to Radarr/Sonarr (or report the dry-run intent)."""

    try:
        outcome = await queue_candidate(get_config(), candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "queued": outcome.queued,
        "dry_run": outcome.dry_run,
        "external_id": outcome.external_id,
        "message": outcome.message,
    }
