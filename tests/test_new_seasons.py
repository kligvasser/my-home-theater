"""New-season suggestions for series you already own (library new seasons source)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind

TMDB = "https://api.themoviedb.org/3"
OMDB = "https://www.omdbapi.com/"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMDB_API_KEY", "k")
    monkeypatch.setenv("OMDB_API_KEY", "k")
    _reset()


def _tv_details(seasons: list[dict]) -> dict:
    return {
        "id": 1396,
        "name": "Breaking Bad",
        "first_air_date": "2008-01-20",
        "episode_run_time": [47],
        "vote_average": 8.9,
        "vote_count": 12000,
        "popularity": 200.0,
        "genres": [{"id": 18, "name": "Drama"}],
        "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
        "number_of_seasons": len([s for s in seasons if s["season_number"] > 0]),
        "number_of_episodes": 30,
        "status": "Returning Series",
        "seasons": seasons,
    }


def _mock_empty_trending() -> None:
    for media in ("movie", "tv"):
        respx.get(f"{TMDB}/trending/{media}/week").mock(
            return_value=httpx.Response(200, json={"results": []})
        )


def _seed_owned_series(seasons: list[int]) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import OwnedFile, Title

    with session_scope() as s:
        t = Title(tmdb_id=1396, title="Breaking Bad", year=2008, kind=TitleKind.series)
        t.owned_files = [
            OwnedFile(path=f"/tv/bb/s{n:02d}e01.mkv", kind=TitleKind.series, season=n, episode=1)
            for n in seasons
        ]
        s.add(t)


@respx.mock
async def test_new_aired_season_suggested(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owning S1–S2 of a show with an aired S3 yields one season-scoped candidate;
    specials (S0) and unaired seasons are ignored, and a re-run doesn't duplicate."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate
    from homeTheater.discovery import run_discovery

    init_db()
    _seed_owned_series([1, 2])
    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 0, "episode_count": 3, "air_date": "2009-02-17"},
                    {"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"},
                    {"season_number": 2, "episode_count": 13, "air_date": "2009-03-08"},
                    {"season_number": 3, "episode_count": 13, "air_date": "2010-03-21"},
                    {"season_number": 4, "episode_count": 13, "air_date": "2030-01-01"},
                ]
            ),
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.5", "imdbVotes": "2,000,000"}
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 1

    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        assert cand.season == 3
        assert "new season S03" in cand.reason
        assert "13 episodes" in cand.reason
        assert "you own up to S02" in cand.reason
        assert cand.features["season"] == 3

    # Re-run: the live S3 candidate keeps the season from being re-emitted at all
    # (it must not eat the per-source limit), so nothing is even considered.
    stats = await run_discovery(get_config())
    assert stats.created == 0 and stats.considered == 0


@respx.mock
async def test_rejected_season_blocks_only_that_season(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting S3 must not bury S4: per-(title, season) invariants."""

    _env(monkeypatch)
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, CandidateStatus, Title
    from homeTheater.discovery import run_discovery

    init_db()
    _seed_owned_series([1, 2])
    with session_scope() as s:
        title_id = s.scalars(select(Title.id)).one()
        s.add(
            Candidate(
                title_id=title_id,
                season=3,
                source=CandidateSource.discovery,
                status=CandidateStatus.rejected,
            )
        )

    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"},
                    {"season_number": 2, "episode_count": 13, "air_date": "2009-03-08"},
                    {"season_number": 3, "episode_count": 13, "air_date": "2010-03-21"},
                    {"season_number": 4, "episode_count": 13, "air_date": "2011-07-17"},
                ]
            ),
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "9.5", "imdbVotes": "2,000,000"}
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 1  # S4 only; rejected S3 stays rejected
    with session_scope() as s:
        seasons = {c.season: c.status for c in s.scalars(select(Candidate)).all()}
        assert seasons[3] == CandidateStatus.rejected
        assert seasons[4] == CandidateStatus.new


@respx.mock
async def test_series_without_new_seasons_makes_no_details_call_noise(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully-owned show produces no candidates (and no crash on cache hits)."""

    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    _seed_owned_series([1, 2, 3])
    _mock_empty_trending()
    respx.get(f"{TMDB}/tv/1396").mock(
        return_value=httpx.Response(
            200,
            json=_tv_details(
                [
                    {"season_number": 1, "episode_count": 7, "air_date": "2008-01-20"},
                    {"season_number": 2, "episode_count": 13, "air_date": "2009-03-08"},
                    {"season_number": 3, "episode_count": 13, "air_date": "2010-03-21"},
                ]
            ),
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 0


def test_build_query_targets_season() -> None:
    from homeTheater.acquisition.torrent.select import build_query

    assert build_query("Breaking Bad", 2008, TitleKind.series, season=3) == "Breaking Bad S03"
    assert build_query("Breaking Bad", 2008, TitleKind.series) == "Breaking Bad"
    assert build_query("Heat", 1995, TitleKind.movie, season=None) == "Heat 1995"
