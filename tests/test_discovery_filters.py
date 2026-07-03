"""Discovery threshold filtering + scoring (pure)."""

from __future__ import annotations

from homeTheater.config import Thresholds
from homeTheater.discovery import evaluate, score


def _thresholds() -> Thresholds:
    return Thresholds(min_imdb_rating=7.0, min_imdb_votes=25_000, min_tmdb_votes=500)


def test_passes_when_above_thresholds() -> None:
    out = evaluate(
        imdb_rating=8.2,
        imdb_votes=100_000,
        tmdb_votes=5000,
        genres=["Action"],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert out.passed
    assert "8.2" in out.reason and "100,000" in out.reason


def test_fails_low_rating() -> None:
    out = evaluate(
        imdb_rating=6.5,
        imdb_votes=100_000,
        tmdb_votes=5000,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed and "< 7.0" in out.reason


def test_fails_few_votes() -> None:
    out = evaluate(
        imdb_rating=9.0,
        imdb_votes=1000,
        tmdb_votes=5000,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed and "votes" in out.reason


def test_fails_missing_rating() -> None:
    out = evaluate(
        imdb_rating=None,
        imdb_votes=None,
        tmdb_votes=None,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed


def test_excluded_genre_blocks() -> None:
    out = evaluate(
        imdb_rating=9.0,
        imdb_votes=500_000,
        tmdb_votes=9000,
        genres=["Documentary"],
        thresholds=_thresholds(),
        excluded_genres=["documentary"],
    )
    assert not out.passed and "excluded genre" in out.reason


def test_score_rewards_more_votes() -> None:
    # Same rating, more votes -> higher score.
    assert score(8.0, 500_000, 10) > score(8.0, 1000, 10)
