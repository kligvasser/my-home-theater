"""Server-rendered dashboard pages (read-only)."""

from __future__ import annotations

from math import ceil

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..dashboard import get_stats, list_titles, recent_runs
from ..dashboard.queries import PAGE_SIZE
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


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": recent_runs(50), "active": "runs", "version": __version__},
    )
