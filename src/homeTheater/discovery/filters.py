"""Threshold filtering + scoring for discovery candidates (plan §5.4).

Pure functions so the 'should I get this?' logic is unit-tested without I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Thresholds


@dataclass(frozen=True, slots=True)
class FilterOutcome:
    passed: bool
    reason: str


def evaluate(
    *,
    imdb_rating: float | None,
    imdb_votes: int | None,
    tmdb_votes: int | None,
    genres: list[str],
    thresholds: Thresholds,
    excluded_genres: list[str],
) -> FilterOutcome:
    """Apply 'high rank with enough views' + genre exclusions.

    Returns a human-readable reason either way (why it passed, or why not).
    """

    excluded = {g.lower() for g in excluded_genres}
    hit = [g for g in genres if g.lower() in excluded]
    if hit:
        return FilterOutcome(False, f"excluded genre: {', '.join(hit)}")

    if imdb_rating is None or imdb_rating < thresholds.min_imdb_rating:
        got = "no rating" if imdb_rating is None else f"{imdb_rating:.1f}"
        return FilterOutcome(False, f"IMDb {got} < {thresholds.min_imdb_rating}")

    if imdb_votes is None or imdb_votes < thresholds.min_imdb_votes:
        got = "no votes" if imdb_votes is None else f"{imdb_votes:,}"
        return FilterOutcome(False, f"IMDb votes {got} < {thresholds.min_imdb_votes:,}")

    if tmdb_votes is not None and tmdb_votes < thresholds.min_tmdb_votes:
        return FilterOutcome(False, f"TMDb votes {tmdb_votes:,} < {thresholds.min_tmdb_votes:,}")

    return FilterOutcome(True, f"IMDb {imdb_rating:.1f} with {imdb_votes:,} votes")


def score(
    imdb_rating: float | None,
    imdb_votes: int | None,
    popularity: float | None,
) -> float:
    """Rank = rating weighted by how many people voted, nudged by popularity.

    ``rating × log10(votes)`` rewards broadly-validated high ratings over a 9.5
    with 200 votes; popularity is a small tiebreaker.
    """

    r = imdb_rating or 0.0
    v = imdb_votes or 0
    pop = popularity or 0.0
    return round(r * math.log10(v + 10) + pop / 1000.0, 3)
