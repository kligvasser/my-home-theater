"""Canonical per-title feature extraction for the preference model.

One function, one shape: :func:`extract_features` turns a :class:`Title` into a
flat, JSON-serializable dict. It is used in two places so training and inference
can never drift apart:

* snapshotted onto ``Candidate.features`` the moment a candidate is created
  (labels come later from approve/reject/import decisions, so the snapshot must
  preserve the feature values *as they were at decision time*);
* recomputed over owned titles when building a training set ("what I own" are
  the seed positives).

Keep features derivable from the catalog only — no network calls here.
"""

from __future__ import annotations

import math
from typing import Any

from .db.models import Title, TitleKind

FEATURES_VERSION = 1


def extract_features(title: Title) -> dict[str, Any]:
    """Flat feature dict for one title. Values are JSON-native; missing = None."""

    genres = sorted(g.name for g in title.genres) if title.genres else []
    return {
        "version": FEATURES_VERSION,
        "kind": title.kind.value if isinstance(title.kind, TitleKind) else str(title.kind),
        "year": title.year,
        "decade": (title.year // 10 * 10) if title.year else None,
        "runtime": title.runtime,
        "genres": genres,
        "imdb_rating": title.imdb_rating,
        "imdb_votes": title.imdb_votes,
        "imdb_votes_log10": (
            round(math.log10(title.imdb_votes), 3) if title.imdb_votes else None
        ),
        "tmdb_rating": title.tmdb_rating,
        "tmdb_votes": title.tmdb_votes,
        "popularity": title.popularity,
        "original_language": title.original_language,
        "origin_countries": title.origin_countries or [],
        "certification": title.certification,
        "keywords": title.keywords or [],
        "cast_top": title.cast_top or [],
        "directors": title.directors or [],
        "in_collection": title.collection_tmdb_id is not None,
        "collection_name": title.collection_name,
        "seasons_count": title.seasons_count,
        "episodes_count": title.episodes_count,
        "series_status": title.series_status,
        "overview_len": len(title.overview) if title.overview else 0,
    }
