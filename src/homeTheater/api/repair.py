"""Catalog repair: review split titles, fold them back, fix matches by hand.

Previews are reads. Applies run under :func:`repair_guard` (the CLI's locks and
backup) and take back the preview's token, so they do what was reviewed or nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..config import AppConfig, effective_config
from ..db.models import TitleKind
from ..db.session import session_scope
from ..errors import JobBusyError, NotConfiguredError, redact_exc
from ..logging_setup import get_logger
from ..metadata.dto import TmdbTitle
from ..metadata.tmdb import TMDbClient
from ..scanner import repair
from .auth import require_token
from .templates import templates

log = get_logger(__name__)

router = APIRouter(tags=["repair"])

_UNMATCHED_SHOWN = 200


@router.get("/repair", response_class=HTMLResponse, include_in_schema=False)
def repair_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "repair.html", {"active": "repair", "version": __version__}
    )


def _preview() -> dict[str, Any]:
    payload = repair.preview_plan(effective_config().nas.tv_root)
    with session_scope() as s:
        payload["unmatched"], payload["unmatched_total"] = repair.unmatched_titles(
            s, _UNMATCHED_SHOWN
        )
    return payload


@router.get("/api/repair/preview")
def api_preview() -> dict[str, Any]:
    return _preview()


def _guarded[T](work: Callable[[], T]) -> T:
    """Run a repair call, turning what it refuses into HTTP answers."""

    try:
        return work()
    except repair.TitleNotFoundError as exc:
        raise HTTPException(404, redact_exc(exc)) from exc
    except (JobBusyError, repair.ReviewOutdatedError) as exc:
        raise HTTPException(409, redact_exc(exc)) from exc
    except NotConfiguredError as exc:  # no database file to back up
        raise HTTPException(503, redact_exc(exc)) from exc


class ReviewedRepair(BaseModel):
    folder: str
    token: str


class ApplyRepairs(BaseModel):
    repairs: list[ReviewedRepair]


@router.post("/api/repair/apply", dependencies=[Depends(require_token)])
def api_apply(reviewed: ApplyRepairs) -> dict[str, Any]:
    """Apply the selected repairs. Anything changed since the preview is left alone
    and listed in ``outdated``."""

    cfg = effective_config()
    chosen = {r.folder: r.token for r in reviewed.repairs}
    if not chosen:
        raise HTTPException(422, "Nothing selected.")
    return asdict(_guarded(lambda: repair.apply_reviewed(cfg, chosen)))


class MergeRequest(BaseModel):
    dup_id: int = Field(ge=1)
    keep_id: int = Field(ge=1)


class MergeApply(MergeRequest):
    token: str


@router.post("/api/repair/merge/preview", dependencies=[Depends(require_token)])
def api_merge_preview(req: MergeRequest) -> dict[str, Any]:
    return _guarded(lambda: repair.merge_preview(req.dup_id, req.keep_id))


@router.post("/api/repair/merge", dependencies=[Depends(require_token)])
def api_merge(req: MergeApply) -> dict[str, Any]:
    cfg = effective_config()
    return _guarded(lambda: repair.apply_merge(cfg, req.dup_id, req.keep_id, req.token))


class RematchRequest(BaseModel):
    title_id: int = Field(ge=1)
    tmdb_id: int = Field(ge=1)


class RematchApply(RematchRequest):
    token: str


async def _details(cfg: AppConfig, tmdb_id: int, kind: TitleKind) -> TmdbTitle:
    """TMDb's record of ``tmdb_id`` in the title's own kind: the same number can be
    a different work as a movie and as a series."""

    if cfg.secrets.tmdb_api_key is None:
        raise HTTPException(503, "TMDB_API_KEY is not configured")
    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            tmdb = TMDbClient(
                cfg.secrets.tmdb_api_key.get_secret_value(),
                http,
                language=cfg.metadata.language,
                cache_days=cfg.metadata.cache_days,
            )
            return await tmdb.details(tmdb_id, kind)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, f"TMDb has no {kind.value} with id {tmdb_id}") from exc
        raise HTTPException(502, "TMDb request failed") from exc
    except httpx.HTTPError as exc:
        log.warning("repair.tmdb_failed", error=redact_exc(exc))
        raise HTTPException(502, "TMDb request failed") from exc


async def _title_details(req: RematchRequest) -> tuple[AppConfig, TmdbTitle]:
    cfg = await asyncio.to_thread(effective_config)
    kind = await asyncio.to_thread(_guarded, lambda: repair.title_kind(req.title_id))
    return cfg, await _details(cfg, req.tmdb_id, kind)


@router.post("/api/repair/rematch/preview", dependencies=[Depends(require_token)])
async def api_rematch_preview(req: RematchRequest) -> dict[str, Any]:
    _, details = await _title_details(req)
    return await asyncio.to_thread(_guarded, lambda: repair.rematch_preview(req.title_id, details))


@router.post("/api/repair/rematch", dependencies=[Depends(require_token)])
async def api_rematch(req: RematchApply) -> dict[str, Any]:
    cfg, details = await _title_details(req)
    return await asyncio.to_thread(
        _guarded, lambda: repair.apply_rematch(cfg, req.title_id, details, req.token)
    )
