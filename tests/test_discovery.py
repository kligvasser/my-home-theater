"""Full discovery flow + manual add, with TMDb/OMDb mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind

TMDB = "https://api.themoviedb.org/3"
OMDB = "https://www.omdbapi.com/"


def _details(tmdb_id: int, title: str, imdb: str, votes: int, genres: list[str]) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "release_date": "2008-07-16",
        "runtime": 152,
        "vote_average": 8.5,
        "vote_count": votes,
        "popularity": 90.0,
        "genres": [{"id": 1, "name": g} for g in genres],
        "external_ids": {"imdb_id": imdb},
        "imdb_id": imdb,
    }


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


def _mock_common() -> None:
    # TV trending is enabled by default -> return nothing so only movies flow.
    respx.get(f"{TMDB}/trending/tv/week").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    # Owned (603) + a passing title (155) + a failing title (999).
    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": 603, "title": "The Matrix", "release_date": "1999-03-30"},
                    {"id": 155, "title": "The Dark Knight", "release_date": "2008-07-16"},
                    {"id": 999, "title": "Meh Movie", "release_date": "2010-01-01"},
                ]
            },
        )
    )
    respx.get(f"{TMDB}/movie/155").mock(
        return_value=httpx.Response(
            200, json=_details(155, "The Dark Knight", "tt0468569", 30000, ["Action"])
        )
    )
    respx.get(f"{TMDB}/movie/999").mock(
        return_value=httpx.Response(
            200, json=_details(999, "Meh Movie", "tt9999999", 800, ["Drama"])
        )
    )
    respx.get(OMDB).mock(
        side_effect=lambda request: httpx.Response(
            200,
            json=(
                {"Response": "True", "imdbRating": "9.0", "imdbVotes": "2,600,000"}
                if request.url.params.get("i") == "tt0468569"
                else {"Response": "True", "imdbRating": "6.0", "imdbVotes": "5,000"}
            ),
        )
    )


@respx.mock
async def test_discovery_creates_ranked_candidates(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.discovery import run_discovery

    init_db()
    # Seed The Matrix as OWNED with its TMDb id so discovery skips it.
    with session_scope() as s:
        owned = Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie)
        owned.owned_files = [OwnedFile(path="/m.mkv", kind=TitleKind.movie)]
        s.add(owned)

    _mock_common()
    stats = await run_discovery(get_config())

    assert stats.owned_skipped == 1  # The Matrix
    assert stats.considered == 2  # Dark Knight + Meh
    assert stats.created == 1  # only Dark Knight passes
    assert stats.filtered == 1  # Meh fails rating/votes

    from sqlalchemy import select

    from homeTheater.db.models import Candidate

    with session_scope() as s:
        cands = s.scalars(select(Candidate)).all()
        assert len(cands) == 1
        c = cands[0]
        assert "9.0" in c.reason and "2,600,000" in c.reason
        assert "trending" in c.reason
        assert c.score and c.score > 0


@respx.mock
async def test_discovery_dedups_and_skips_live(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.discovery import run_discovery

    init_db()
    _mock_common()
    # First run creates the Dark Knight candidate.
    await run_discovery(get_config())
    # Second run must skip it as a live candidate, creating nothing new.
    stats = await run_discovery(get_config())
    assert stats.live_skipped == 1
    assert stats.created == 0


@respx.mock
async def test_add_manual(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource
    from homeTheater.discovery.actions import add_manual

    init_db()
    respx.get(f"{TMDB}/movie/27205").mock(
        return_value=httpx.Response(
            200, json=_details(27205, "Inception", "tt1375666", 40000, ["Action"])
        )
    )
    respx.get(OMDB).mock(
        return_value=httpx.Response(
            200, json={"Response": "True", "imdbRating": "8.8", "imdbVotes": "2,400,000"}
        )
    )

    cid = await add_manual(get_config(), 27205, TitleKind.movie)
    with session_scope() as s:
        c = s.get(Candidate, cid)
        assert c is not None and c.source == CandidateSource.manual
