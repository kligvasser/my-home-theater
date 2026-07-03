"""Server-rendered dashboard pages (read-only)."""

from __future__ import annotations

import asyncio
from math import ceil

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..config import get_config
from ..dashboard import (
    candidate_counts,
    get_stats,
    list_candidates,
    list_missing_subtitles,
    list_titles,
    recent_runs,
)
from ..dashboard.queries import PAGE_SIZE
from ..db.models import CandidateStatus
from .templates import templates

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": get_stats(),
            "runs": recent_runs(10),
            "active": "dashboard",
            "version": __version__,
        },
    )


@router.get("/library", response_class=HTMLResponse)
def library(
    request: Request,
    q: str | None = None,
    kind: str | None = None,
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    rows, total = list_titles(q=q, kind=kind, page=page)
    pages = max(1, ceil(total / PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "rows": rows,
            "total": total,
            "q": q,
            "kind": kind,
            "page": page,
            "pages": pages,
            "active": "library",
            "version": __version__,
        },
    )


@router.get("/candidates", response_class=HTMLResponse)
def candidates(request: Request, status: str = "new") -> HTMLResponse:
    # Unknown ?status= values would otherwise blow up at enum binding; fall back.
    try:
        shown = CandidateStatus(status)
    except ValueError:
        shown = CandidateStatus.new
    return templates.TemplateResponse(
        request,
        "candidates.html",
        {
            "candidates": list_candidates(status=shown),
            "counts": candidate_counts(),
            "status": str(shown),
            "active": "candidates",
            "version": __version__,
        },
    )


@router.get("/subtitles", response_class=HTMLResponse)
def subtitles(request: Request) -> HTMLResponse:
    lang = get_config().subtitles.primary
    stats = get_stats(sub_lang=lang)
    return templates.TemplateResponse(
        request,
        "subtitles.html",
        {
            "coverage": stats.coverage,
            "missing": list_missing_subtitles(lang=lang, limit=200),
            "active": "subtitles",
            "version": __version__,
        },
    )


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    from ..health import check_all

    cfg = get_config()
    # recent_runs is sync SQLAlchemy; keep it off the event loop.
    runs = await asyncio.to_thread(recent_runs, 50)
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "providers": await check_all(cfg),
            "failures": [r for r in runs if r.status == "failed"],
            "dry_run": cfg.features.dry_run,
            "auto_approve": cfg.features.auto_approve,
            "scheduler": cfg.schedule.enabled,
            "active": "status",
            "version": __version__,
        },
    )


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": recent_runs(50), "active": "runs", "version": __version__},
    )
