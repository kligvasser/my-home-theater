"""Discovery threshold filtering + scoring (pure)."""

from __future__ import annotations

from homeTheater.config import Thresholds
from homeTheater.config.settings import ResolvedThresholds
from homeTheater.discovery import evaluate, score


def _thresholds(**over: object) -> ResolvedThresholds:
    defaults: dict[str, object] = {
        "min_imdb_rating": 7.0,
        "min_imdb_votes": 25_000,
        "min_tmdb_votes": 500,
        "tmdb_fallback": True,
    }
    defaults.update(over)
    return ResolvedThresholds(**defaults)  # type: ignore[arg-type]


def test_passes_when_above_thresholds() -> None:
    out = evaluate(
        imdb_rating=8.2,
        imdb_votes=100_000,
        tmdb_rating=8.0,
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
        tmdb_rating=6.5,
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
        tmdb_rating=9.0,
        tmdb_votes=5000,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed and "votes" in out.reason


def test_missing_imdb_falls_back_to_tmdb() -> None:
    out = evaluate(
        imdb_rating=None,
        imdb_votes=None,
        tmdb_rating=7.9,
        tmdb_votes=1200,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert out.passed and "TMDb" in out.reason


def test_missing_imdb_fallback_still_applies_bars() -> None:
    out = evaluate(
        imdb_rating=None,
        imdb_votes=None,
        tmdb_rating=6.0,
        tmdb_votes=1200,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed and "TMDb" in out.reason


def test_missing_everything_fails() -> None:
    out = evaluate(
        imdb_rating=None,
        imdb_votes=None,
        tmdb_rating=None,
        tmdb_votes=None,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed


def test_fallback_disabled_rejects_missing_imdb() -> None:
    out = evaluate(
        imdb_rating=None,
        imdb_votes=None,
        tmdb_rating=9.0,
        tmdb_votes=90_000,
        genres=[],
        thresholds=_thresholds(tmdb_fallback=False),
        excluded_genres=[],
    )
    assert not out.passed and "no IMDb data" in out.reason


def test_missing_tmdb_votes_no_longer_bypasses_bar() -> None:
    out = evaluate(
        imdb_rating=9.0,
        imdb_votes=500_000,
        tmdb_rating=None,
        tmdb_votes=None,
        genres=[],
        thresholds=_thresholds(),
        excluded_genres=[],
    )
    assert not out.passed and "TMDb votes" in out.reason


def test_excluded_genre_blocks() -> None:
    out = evaluate(
        imdb_rating=9.0,
        imdb_votes=500_000,
        tmdb_rating=8.5,
        tmdb_votes=9000,
        genres=["Documentary"],
        thresholds=_thresholds(),
        excluded_genres=["documentary"],
    )
    assert not out.passed and "excluded genre" in out.reason


def test_per_kind_thresholds_resolve() -> None:
    t = Thresholds(min_imdb_votes=25_000)
    assert t.for_kind("movie").min_imdb_votes == 25_000
    # series default override lowers the vote bar
    assert t.for_kind("series").min_imdb_votes == 5_000
    # explicit overrides win
    t2 = Thresholds(movie={"min_imdb_rating": 6.5}, series={"min_imdb_votes": 111})
    assert t2.for_kind("movie").min_imdb_rating == 6.5
    assert t2.for_kind("series").min_imdb_votes == 111
    # globals fill the gaps
    assert t2.for_kind("movie").min_imdb_votes == 25_000


def test_score_rewards_more_votes() -> None:
    # Same rating, more votes -> higher score.
    assert score(8.0, 500_000, 10) > score(8.0, 1000, 10)


def test_score_falls_back_to_tmdb() -> None:
    assert score(None, None, 0, tmdb_rating=8.0, tmdb_votes=1000) > 0
