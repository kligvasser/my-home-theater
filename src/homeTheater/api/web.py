"""Server-rendered dashboard pages (read-only)."""

from __future__ import annotations

import asyncio
from math import ceil

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from .. import __version__
from ..config import get_config, load_overrides
from ..dashboard import (
    CANDIDATE_SORTS,
    TITLE_SORTS,
    candidate_counts,
    get_stats,
    list_candidates,
    list_missing_subtitles,
    list_titles,
    recent_runs,
    recent_titles,
)
from ..dashboard.queries import PAGE_SIZE
from ..db.models import CandidateStatus
from .templates import templates

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    from ..config import effective_config

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": get_stats(sub_langs=effective_config().subtitles.languages),
            "recent": recent_titles(12),
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
    sort: str = "added",
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    if sort not in TITLE_SORTS:
        sort = "added"
    rows, total = list_titles(q=q, kind=kind, page=page, sort=sort)
    pages = max(1, ceil(total / PAGE_SIZE))
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "rows": rows,
            "total": total,
            "q": q,
            "kind": kind,
            "sort": sort,
            "page": page,
            "pages": pages,
            "active": "library",
            "version": __version__,
        },
    )


@router.get("/candidates", response_class=HTMLResponse)
def candidates(
    request: Request,
    status: str = "new",
    kind: str | None = None,
    sort: str = "score",
) -> HTMLResponse:
    # Unknown ?status= values would otherwise blow up at enum binding; fall back.
    try:
        shown = CandidateStatus(status)
    except ValueError:
        shown = CandidateStatus.new
    if sort not in CANDIDATE_SORTS:
        sort = "score"
    if kind not in ("movie", "series"):
        kind = None
    return templates.TemplateResponse(
        request,
        "candidates.html",
        {
            "candidates": list_candidates(status=shown, kind=kind, sort=sort),
            "counts": candidate_counts(),
            "status": str(shown),
            "kind": kind or "",
            "sort": sort,
            "active": "candidates",
            "version": __version__,
        },
    )


@router.get("/subtitles", response_class=HTMLResponse)
def subtitles(request: Request, lang: str | None = None) -> HTMLResponse:
    from ..config import effective_config

    langs = effective_config().subtitles.languages or ["he"]
    shown = lang if lang in langs else langs[0]
    stats = get_stats(sub_langs=langs)
    return templates.TemplateResponse(
        request,
        "subtitles.html",
        {
            "coverages": stats.coverages,
            "shown": shown,
            "missing": list_missing_subtitles(lang=shown, limit=200),
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


@router.get("/gaps", response_class=HTMLResponse)
async def gaps(
    request: Request,
    kind: str | None = None,
    min_rating: float = Query(0, ge=0, le=10),
    decade: int | None = Query(None, ge=1900, le=2030),
    q: str | None = None,
) -> HTMLResponse:
    """'Why not owned?' — top-rated titles above your thresholds that you don't
    have (and haven't rejected). TMDb-list data only: no per-title API cost."""

    import httpx

    from ..config import effective_config
    from ..db.models import TitleKind
    from ..discovery.filters import evaluate
    from ..discovery.service import _owned_live_rejected
    from ..metadata.tmdb import TMDbClient

    cfg = effective_config()
    kinds = (
        [TitleKind(kind)]
        if kind in (TitleKind.movie, TitleKind.series)
        else [TitleKind.movie, TitleKind.series]
    )
    rows: list[dict[str, object]] = []
    error: str | None = None
    if cfg.secrets.tmdb_api_key is None:
        error = "TMDB_API_KEY is not configured."
    else:
        owned, live, rejected = await asyncio.to_thread(_owned_live_rejected)
        taken = owned | live | rejected
        needle = (q or "").strip().lower()
        async with httpx.AsyncClient(timeout=15.0) as http:
            tmdb = TMDbClient(
                cfg.secrets.tmdb_api_key.get_secret_value(),
                http,
                language=cfg.metadata.language,
                cache_days=cfg.metadata.cache_days,
            )
            for k in kinds:
                for t in await tmdb.top_rated(k, limit=60):
                    if (k, t.tmdb_id) in taken:
                        continue
                    outcome = evaluate(
                        imdb_rating=None,  # list payloads are TMDb-only
                        imdb_votes=None,
                        tmdb_rating=t.tmdb_rating,
                        tmdb_votes=t.tmdb_votes,
                        genres=[],
                        thresholds=cfg.thresholds.for_kind(k.value),
                        excluded_genres=cfg.discovery.excluded_genres,
                    )
                    if not outcome.passed:
                        continue
                    if min_rating and (t.tmdb_rating or 0) < min_rating:
                        continue
                    if decade and not (t.year and decade <= t.year < decade + 10):
                        continue
                    if needle and needle not in t.title.lower():
                        continue
                    rows.append(
                        {
                            "tmdb_id": t.tmdb_id,
                            "title": t.title,
                            "year": t.year,
                            "kind": k.value,
                            "rating": t.tmdb_rating,
                            "votes": t.tmdb_votes,
                            "poster_url": t.poster_url,
                            "overview": t.overview,
                        }
                    )
        rows.sort(key=lambda r: float(r["rating"] or 0.0), reverse=True)  # type: ignore[arg-type]

    decades = list(range(2020, 1949, -10))
    return templates.TemplateResponse(
        request,
        "gaps.html",
        {
            "rows": rows,
            "error": error,
            "kind": kind if kind in ("movie", "series") else "",
            "min_rating": min_rating,
            "decade": decade,
            "decades": decades,
            "q": q or "",
            "active": "gaps",
            "version": __version__,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    cfg = get_config()
    file_values = {
        "thresholds": cfg.thresholds.model_dump(mode="json"),
        "discovery": cfg.discovery.model_dump(mode="json"),
        "taste": cfg.taste.model_dump(mode="json"),
        "subtitles": cfg.subtitles.model_dump(mode="json"),
        "organizer": cfg.organizer.model_dump(mode="json"),
        "features": {"auto_approve": cfg.features.auto_approve},
    }
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "file": file_values,
            "ov": load_overrides(),
            "read_only": {"dry_run": cfg.features.dry_run},
            "active": "settings",
            "version": __version__,
        },
    )


@router.get("/insights", response_class=HTMLResponse)
def insights(request: Request) -> HTMLResponse:
    """Taste clusters per kind. Sync-def: sklearn runs in the threadpool."""

    from ..preferences import model_info
    from ..taste import build_index

    model = model_info(get_config())
    cfg = get_config().taste
    sections = []
    for kind_label, kind in (("Movies", "movie"), ("Series", "series")):
        from ..db.models import TitleKind

        index = build_index(TitleKind(kind), min_library=cfg.min_library)
        sections.append(
            {
                "label": kind_label,
                "available": index is not None,
                "titles": index.size if index else 0,
                "min_library": cfg.min_library,
                "clusters": index.clusters(cfg.max_clusters) if index else [],
            }
        )
    return templates.TemplateResponse(
        request,
        "insights.html",
        {"sections": sections, "model": model, "active": "insights", "version": __version__},
    )


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"runs": recent_runs(50), "active": "runs", "version": __version__},
    )
