"""JSON API over the catalog + run history (+ token-gated catalog cleanup)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select

from ..dashboard import TITLE_SORTS, get_stats, list_titles, recent_runs
from ..db.models import Candidate, Download, OwnedFile, Subtitle, Title
from ..db.session import session_scope
from ..logging_setup import get_logger
from .auth import require_token

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["catalog"])


@router.get("/stats")
def api_stats() -> dict[str, Any]:
    from ..config import effective_config

    stats = get_stats(sub_langs=effective_config().subtitles.languages)
    data = asdict(stats)
    data["coverage"]["pct"] = stats.coverage.pct  # property isn't captured by asdict
    for entry, cov in zip(data["coverages"], stats.coverages, strict=True):
        entry["pct"] = cov.pct
    return data


@router.get("/titles")
def api_titles(
    q: str | None = None,
    kind: str | None = None,
    page: int = Query(1, ge=1),
    sort: str = Query("added", pattern=f"^({'|'.join(TITLE_SORTS)})$"),
) -> dict[str, Any]:
    rows, total = list_titles(q=q, kind=kind, page=page, sort=sort)
    return {"total": total, "page": page, "sort": sort, "items": [asdict(r) for r in rows]}


@router.delete("/titles/{title_id}", dependencies=[Depends(require_token)])
def api_delete_title(title_id: int) -> dict[str, Any]:
    """Remove a title and its catalog children (cleanup for bad parses etc.).

    Catalog-only: nothing on the NAS is touched. If the file still exists on the
    NAS, the next scan will re-add it. Deleting also drops the title's candidate
    history (including rejected = training labels) — the UI warns about that.
    """

    with session_scope() as s:
        title = s.get(Title, title_id)
        if title is None:
            raise HTTPException(status_code=404, detail="title not found")
        name = title.title
        candidate_ids = list(
            s.scalars(select(Candidate.id).where(Candidate.title_id == title_id)).all()
        )
        if candidate_ids:
            s.execute(delete(Download).where(Download.candidate_id.in_(candidate_ids)))
            s.execute(delete(Candidate).where(Candidate.id.in_(candidate_ids)))
        s.execute(delete(Subtitle).where(Subtitle.title_id == title_id))
        s.execute(delete(OwnedFile).where(OwnedFile.title_id == title_id))
        title.genres = []  # clear the title_genre association rows
        s.delete(title)
    log.info("catalog.title_deleted", title_id=title_id, title=name)
    return {"deleted": title_id, "title": name}


@router.get("/runs")
def api_runs(limit: int = Query(25, ge=1, le=200)) -> list[dict[str, Any]]:
    return [asdict(r) for r in recent_runs(limit)]
