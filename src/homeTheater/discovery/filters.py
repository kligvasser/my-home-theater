"""Threshold filtering + scoring for discovery candidates (plan §5.4).

Pure functions so the 'should I get this?' logic is unit-tested without I/O.
Thresholds arrive already resolved per title kind (movies and series have very
different vote economics — see config.Thresholds).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config.settings import ResolvedThresholds


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    passed: bool
    reason: str


def evaluate(
    *,
    imdb_rating: float | None,
    imdb_votes: int | None,
    tmdb_rating: float | None,
    tmdb_votes: int | None,
    genres: list[str],
    thresholds: ResolvedThresholds,
    excluded_genres: list[str],
) -> FilterOutcome:
    """Apply 'high rank with enough views' + genre exclusions.

    Returns a human-readable reason either way (why it passed, or why not).
    When IMDb data is missing (no OMDb key, or a title OMDb doesn't know yet),
    ``tmdb_fallback`` judges on TMDb rating/votes instead of rejecting outright.
    """

    excluded = {g.lower() for g in excluded_genres}
    hit = [g for g in genres if g.lower() in excluded]
    if hit:
        return FilterOutcome(False, f"excluded genre: {', '.join(hit)}")

    if imdb_rating is None or imdb_votes is None:
        if not thresholds.tmdb_fallback:
            return FilterOutcome(False, "no IMDb data")
        if tmdb_rating is None or tmdb_votes is None:
            return FilterOutcome(False, "no IMDb or TMDb rating data")
        if tmdb_rating < thresholds.min_imdb_rating:
            return FilterOutcome(
                False, f"TMDb {tmdb_rating:.1f} < {thresholds.min_imdb_rating} (no IMDb data)"
            )
        if tmdb_votes < thresholds.min_tmdb_votes:
            return FilterOutcome(
                False,
                f"TMDb votes {tmdb_votes:,} < {thresholds.min_tmdb_votes:,} (no IMDb data)",
            )
        return FilterOutcome(
            True, f"TMDb {tmdb_rating:.1f} with {tmdb_votes:,} votes (no IMDb data)"
        )

    if imdb_rating < thresholds.min_imdb_rating:
        return FilterOutcome(False, f"IMDb {imdb_rating:.1f} < {thresholds.min_imdb_rating}")

    if imdb_votes < thresholds.min_imdb_votes:
        return FilterOutcome(
            False, f"IMDb votes {imdb_votes:,} < {thresholds.min_imdb_votes:,}"
        )

    # Missing TMDb votes count as 0 — same policy as the IMDb checks above.
    if (tmdb_votes or 0) < thresholds.min_tmdb_votes:
        got = f"{tmdb_votes:,}" if tmdb_votes is not None else "no votes"
        return FilterOutcome(False, f"TMDb votes {got} < {thresholds.min_tmdb_votes:,}")

    return FilterOutcome(True, f"IMDb {imdb_rating:.1f} with {imdb_votes:,} votes")


def score(
    imdb_rating: float | None,
    imdb_votes: int | None,
    popularity: float | None,
    *,
    tmdb_rating: float | None = None,
    tmdb_votes: int | None = None,
) -> float:
    """Rank = rating weighted by how many people voted, nudged by popularity.

    ``rating × log10(votes)`` rewards broadly-validated high ratings over a 9.5
    with 200 votes; popularity is a small tiebreaker. Falls back to TMDb
    rating/votes when IMDb data is missing.
    """

    r = imdb_rating if imdb_rating is not None else (tmdb_rating or 0.0)
    v = imdb_votes if imdb_votes is not None else (tmdb_votes or 0)
    pop = popularity or 0.0
    return round(r * math.log10(v + 10) + pop / 1000.0, 3)
