"""Candidate queue: read (open) + review actions (auth-gated)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..acquisition import queue_candidate
from ..config import effective_config, get_config
from ..dashboard import candidate_counts, list_candidates
from ..db.models import CandidateStatus, TitleKind
from ..discovery.actions import add_manual, approve, reject
from ..errors import InvalidTransitionError, NotConfiguredError, redact_exc
from ..logging_setup import get_logger
from ..metadata.tmdb import TMDbClient
from .auth import require_token

log = get_logger(__name__)

router = APIRouter(prefix="/api/candidates", tags=["candidates"])


class ManualAdd(BaseModel):
    tmdb_id: int
    kind: TitleKind = TitleKind.movie


@router.get("")
def api_list(
    status: CandidateStatus | None = CandidateStatus.new,
    kind: TitleKind | None = None,
    sort: str = Query("score", pattern="^(score|taste|year|rating|added)$"),
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    return {
        "counts": candidate_counts(),
        "items": [
            asdict(c) for c in list_candidates(status=status, kind=kind, sort=sort, limit=limit)
        ],
    }


@router.post("/{candidate_id}/approve", dependencies=[Depends(require_token)])
def api_approve(candidate_id: int) -> dict[str, str]:
    try:
        found = approve(candidate_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="candidate not found")
    return {"status": "approved"}


@router.post("/{candidate_id}/reject", dependencies=[Depends(require_token)])
def api_reject(candidate_id: int) -> dict[str, str]:
    try:
        found = reject(candidate_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not found:
        raise HTTPException(status_code=404, detail="candidate not found")
    return {"status": "rejected"}


@router.post("/manual", dependencies=[Depends(require_token)])
async def api_manual(body: ManualAdd) -> dict[str, int | str]:
    try:
        candidate_id = await add_manual(get_config(), body.tmdb_id, body.kind)
    except NotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": candidate_id, "status": "new"}


@router.get("/search", dependencies=[Depends(require_token)])
async def api_search(
    q: str = Query(min_length=2, max_length=200),
    kind: TitleKind = TitleKind.movie,
) -> dict[str, Any]:
    """TMDb search proxy for the dashboard's search-and-add box.

    Token-gated: it spends TMDb quota and exists to feed the (gated) manual-add
    action anyway.
    """

    cfg = get_config()
    if cfg.secrets.tmdb_api_key is None:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=15.0) as http:
        tmdb = TMDbClient(
            cfg.secrets.tmdb_api_key.get_secret_value(),
            http,
            language=cfg.metadata.language,
            cache_days=cfg.metadata.cache_days,
        )
        try:
            results = await tmdb.search_results(q, kind)
        except httpx.HTTPError as exc:
            log.warning("search.failed", q=q, error=redact_exc(exc))
            raise HTTPException(status_code=502, detail="TMDb search failed") from exc
    return {
        "items": [
            {
                "tmdb_id": r.tmdb_id,
                "title": r.title,
                "year": r.year,
                "kind": kind.value,
                "tmdb_rating": r.tmdb_rating,
                "poster_url": r.poster_url,
                "overview": r.overview,
            }
            for r in results
        ]
    }


class DiscoverRun(BaseModel):
    # Optional one-shot boost: pull deeper trending/top-rated pages this run.
    max_per_source: int | None = Field(None, ge=1, le=100)


@router.post("/discover", dependencies=[Depends(require_token)])
async def api_discover(body: DiscoverRun, background: BackgroundTasks) -> dict[str, Any]:
    """Kick a discovery run now ("search more"). Runs in the background; watch
    /runs for the result. Uses runtime-overridden settings."""

    from ..discovery import run_discovery

    cfg = effective_config()
    if cfg.secrets.tmdb_api_key is None:
        raise HTTPException(status_code=503, detail="TMDB_API_KEY is not configured")
    if body.max_per_source:
        boosted = cfg.discovery.model_copy(update={"max_per_source": body.max_per_source})
        cfg = cfg.model_copy(update={"discovery": boosted})

    async def _run() -> None:
        try:
            await run_discovery(cfg)
        except Exception as exc:  # already logged + recorded in job_run
            log.warning("discover.background_failed", error=redact_exc(exc))

    background.add_task(_run)
    return {"started": True, "max_per_source": cfg.discovery.max_per_source}


@router.post("/{candidate_id}/queue", dependencies=[Depends(require_token)])
async def api_queue(candidate_id: int) -> dict[str, Any]:
    """Hand a candidate to Radarr/Sonarr (or report the dry-run intent)."""

    try:
        outcome = await queue_candidate(get_config(), candidate_id)
    except NotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {
        "queued": outcome.queued,
        "dry_run": outcome.dry_run,
        "external_id": outcome.external_id,
        "message": outcome.message,
    }
