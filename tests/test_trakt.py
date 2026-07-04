"""Trakt device auth + watchlist source (all HTTP mocked via respx)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind

TRAKT = "https://api.trakt.tv"
TMDB = "https://api.themoviedb.org/3"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@respx.mock
async def test_device_flow_saves_token(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db
    from homeTheater.trakt import TraktClient, load_token

    init_db()
    respx.post(f"{TRAKT}/oauth/device/code").mock(
        return_value=httpx.Response(
            200,
            json={
                "device_code": "dev123",
                "user_code": "ABCD1234",
                "verification_url": "https://trakt.tv/activate",
                "expires_in": 600,
                "interval": 0,
            },
        )
    )
    token_route = respx.post(f"{TRAKT}/oauth/device/token")
    token_route.side_effect = [
        httpx.Response(400),  # pending
        httpx.Response(
            200,
            json={"access_token": "acc", "refresh_token": "ref", "expires_in": 7776000},
        ),
    ]

    async with httpx.AsyncClient() as http:
        client = TraktClient("cid", "secret", http)
        device = await client.device_code()
        assert device["user_code"] == "ABCD1234"
        await client.poll_device_token(device)

    saved = load_token()
    assert saved is not None and saved["access_token"] == "acc"
    assert saved["refresh_token"] == "ref"


@respx.mock
async def test_watchlist_fetch_maps_items(config_file: Path) -> None:
    _reset()
    from homeTheater.db import init_db
    from homeTheater.trakt import TraktClient, save_token

    init_db()
    save_token({"access_token": "acc", "refresh_token": "ref", "expires_in": 7776000})

    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "movie": {
                        "title": "Dune",
                        "year": 2021,
                        "ids": {"tmdb": 438631, "imdb": "tt1160419"},
                    }
                }
            ],
        )
    )
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(
        return_value=httpx.Response(
            200,
            json=[{"show": {"title": "Dark", "year": 2017, "ids": {"tmdb": 70523}}}],
        )
    )

    async with httpx.AsyncClient() as http:
        items = await TraktClient("cid", "secret", http).watchlist()

    assert [(i.kind, i.tmdb_id) for i in items] == [
        (TitleKind.movie, 438631),
        (TitleKind.series, 70523),
    ]


@respx.mock
async def test_watchlist_source_bypasses_thresholds(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watchlisted title below the vote threshold still becomes a candidate,
    recorded with source=watchlist."""

    monkeypatch.setenv("TMDB_API_KEY", "k")
    monkeypatch.setenv("TRAKT_CLIENT_ID", "cid")
    monkeypatch.setenv("TRAKT_CLIENT_SECRET", "sec")
    _reset()
    from sqlalchemy import select

    from homeTheater.config import get_config
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource
    from homeTheater.discovery import run_discovery
    from homeTheater.trakt import save_token

    init_db()
    save_token({"access_token": "acc", "refresh_token": "ref", "expires_in": 7776000})

    respx.get(f"{TMDB}/trending/movie/week").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(f"{TMDB}/trending/tv/week").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(
        return_value=httpx.Response(
            200,
            json=[{"movie": {"title": "Tiny Indie", "year": 2024, "ids": {"tmdb": 777}}}],
        )
    )
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(
        return_value=httpx.Response(200, json=[])
    )
    # Low votes + no IMDb data: would fail every threshold if filtered.
    respx.get(f"{TMDB}/movie/777").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 777,
                "title": "Tiny Indie",
                "release_date": "2024-05-01",
                "runtime": 90,
                "vote_average": 6.1,
                "vote_count": 42,
                "genres": [{"id": 18, "name": "Drama"}],
                "external_ids": {"imdb_id": "tt0000777"},
            },
        )
    )

    stats = await run_discovery(get_config())
    assert stats.created == 1 and stats.filtered == 0

    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        assert cand.source == CandidateSource.watchlist
        assert "watchlist" in (cand.reason or "")
