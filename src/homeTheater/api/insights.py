"""Taste insights: library clusters + on-demand similarity for any TMDb title."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ..config import get_config
from ..db.models import TitleKind
from ..errors import NotConfiguredError
from ..metadata.tmdb import TMDbClient
from .auth import require_token

router = APIRouter(prefix="/api", tags=["insights"])


def _clusters_payload() -> dict[str, Any]:
    from ..taste import build_index

    cfg = get_config().taste
    out: dict[str, Any] = {}
    for kind in TitleKind:
        index = build_index(kind, min_library=cfg.min_library)
        if index is None:
            out[kind.value] = {"available": False, "reason": "library too small or unenriched"}
            continue
        out[kind.value] = {
            "available": True,
            "titles": index.size,
            "clusters": [asdict(c) for c in index.clusters(cfg.max_clusters)],
        }
    return out


@router.get("/insights")
async def api_insights() -> dict[str, Any]:
    """Per-kind taste clusters over the owned library."""

    # sklearn fit is sync CPU work; keep it off the event loop.
    return await asyncio.to_thread(_clusters_payload)


@router.post("/preferences/train", dependencies=[Depends(require_token)])
async def api_train() -> dict[str, Any]:
    """(Re)train the preference classifier from your approve/reject decisions."""

    from ..preferences import train

    stats = await asyncio.to_thread(train, get_config())
    return stats.as_dict()


@router.get("/similarity")
async def api_similarity(
    tmdb_id: int, kind: TitleKind = Query(TitleKind.movie)
) -> dict[str, Any]:
    """How close a TMDb title is to the owned library (0..1 + nearest titles)."""

    from ..features import FEATURES_VERSION  # noqa: F401  (feature shape is canonical)
    from ..taste import build_index, tokens_from_features

    cfg = get_config()
    if cfg.secrets.tmdb_api_key is None:
        raise HTTPException(503, "TMDB_API_KEY is not configured")

    index = await asyncio.to_thread(build_index, kind, cfg.taste.min_library)
    if index is None:
        raise HTTPException(409, f"not enough enriched owned {kind.value}s to compare against")

    try:
        async with httpx.AsyncClient(timeout=15.0) as http:
            tmdb = TMDbClient(
                cfg.secrets.tmdb_api_key.get_secret_value(),
                http,
                language=cfg.metadata.language,
                cache_days=cfg.metadata.cache_days,
            )
            details = await tmdb.details(tmdb_id, kind)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(404, f"TMDb has no {kind.value} with id {tmdb_id}") from exc
        raise HTTPException(502, "TMDb request failed") from exc
    except NotConfiguredError as exc:
        raise HTTPException(503, str(exc)) from exc

    feats = {
        "genres": details.genres,
        "keywords": details.keywords,
        "cast_top": details.cast_top,
        "directors": details.directors,
        "original_language": details.original_language,
        "decade": (details.year // 10 * 10) if details.year else None,
        "certification": details.certification,
        "in_collection": details.collection_tmdb_id is not None,
        "collection_name": details.collection_name,
    }
    if not tokens_from_features(feats):
        raise HTTPException(422, "TMDb returned no usable features for this title")
    sim = await asyncio.to_thread(index.similarity, feats, cfg.taste.neighbors)
    return {
        "tmdb_id": tmdb_id,
        "kind": kind.value,
        "title": details.title,
        "similarity": sim.score,
        "like": sim.like,
        "library_size": index.size,
    }
