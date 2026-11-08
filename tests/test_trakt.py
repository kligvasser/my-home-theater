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
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(return_value=httpx.Response(200, json=[]))
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


def _page(items: list[dict], pages: int | None = None) -> httpx.Response:
    headers = {} if pages is None else {"X-Pagination-Page-Count": str(pages)}
    return httpx.Response(200, json=items, headers=headers)


def _movie(tmdb_id: int) -> dict:
    return {"movie": {"title": f"m{tmdb_id}", "ids": {"tmdb": tmdb_id}}}


async def _no_sleep(_seconds: float) -> None:
    return None


async def _watchlist() -> list:
    from homeTheater.trakt import TraktClient

    async with httpx.AsyncClient() as http:
        return await TraktClient("cid", "secret", http).watchlist()


def _valid_token(expires_in: int = 7776000) -> None:
    from homeTheater.db import init_db
    from homeTheater.trakt import save_token

    init_db()
    save_token({"access_token": "acc", "refresh_token": "ref", "expires_in": expires_in})


@respx.mock
async def test_watchlist_follows_pagination(config_file: Path) -> None:
    _reset()
    _valid_token()

    def movies(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _page([_movie(page)], pages=3)

    route = respx.get(f"{TRAKT}/sync/watchlist/movies").mock(side_effect=movies)
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(return_value=_page([]))

    items = await _watchlist()

    assert [i.tmdb_id for i in items] == [1, 2, 3]
    assert route.call_count == 3


@respx.mock
async def test_watchlist_waits_out_a_rate_limit(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    _valid_token()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("homeTheater.trakt.asyncio.sleep", sleep)
    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), _page([_movie(1)])]
    )
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(return_value=_page([]))

    assert [i.tmdb_id for i in await _watchlist()] == [1]
    assert slept == [7.0]


@respx.mock
async def test_persistent_rate_limit_surfaces_as_an_error(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    _valid_token()
    monkeypatch.setattr("homeTheater.trakt.asyncio.sleep", _no_sleep)
    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(return_value=httpx.Response(429))
    with pytest.raises(httpx.HTTPStatusError):
        await _watchlist()


@respx.mock
async def test_expired_token_with_rejected_refresh_asks_to_reauthorize(config_file: Path) -> None:
    """It used to carry on with the dead token and 401 on every run."""

    _reset()
    from homeTheater.errors import NotConfiguredError

    _valid_token(expires_in=-10)
    respx.post(f"{TRAKT}/oauth/token").mock(return_value=httpx.Response(401))
    watchlist = respx.get(f"{TRAKT}/sync/watchlist/movies").mock(return_value=_page([]))

    with pytest.raises(NotConfiguredError, match="trakt-auth"):
        await _watchlist()
    assert watchlist.call_count == 0


@respx.mock
async def test_refresh_outage_with_expired_token_is_an_http_error(config_file: Path) -> None:
    _reset()
    _valid_token(expires_in=-10)
    respx.post(f"{TRAKT}/oauth/token").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(httpx.TransportError):
        await _watchlist()


@respx.mock
async def test_failed_refresh_is_survivable_while_the_token_still_works(config_file: Path) -> None:
    _reset()
    _valid_token(expires_in=60)  # inside the refresh slack, not yet expired
    respx.post(f"{TRAKT}/oauth/token").mock(return_value=httpx.Response(500))
    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(return_value=_page([_movie(1)]))
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(return_value=_page([]))
    assert [i.tmdb_id for i in await _watchlist()] == [1]


@respx.mock
async def test_refresh_keeps_the_old_refresh_token_if_none_comes_back(config_file: Path) -> None:
    _reset()
    from homeTheater.trakt import load_token

    _valid_token(expires_in=-10)
    respx.post(f"{TRAKT}/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
    )
    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(return_value=_page([]))
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(return_value=_page([]))
    await _watchlist()
    token = load_token()
    assert token is not None and token["access_token"] == "new" and token["refresh_token"] == "ref"


@respx.mock
async def test_revoked_token_refreshes_once_and_retries(config_file: Path) -> None:
    _reset()
    _valid_token()
    respx.post(f"{TRAKT}/oauth/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "new", "refresh_token": "r2", "expires_in": 3600}
        )
    )

    def movies(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer acc":
            return httpx.Response(401)
        return _page([_movie(1)])

    respx.get(f"{TRAKT}/sync/watchlist/movies").mock(side_effect=movies)
    respx.get(f"{TRAKT}/sync/watchlist/shows").mock(return_value=_page([]))
    assert [i.tmdb_id for i in await _watchlist()] == [1]


async def test_watchlist_skip_message_is_redacted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset()
    from structlog.testing import capture_logs

    from homeTheater.discovery.sources import TraktWatchlistSource
    from homeTheater.errors import NotConfiguredError
    from homeTheater.trakt import TraktClient

    async def boom(_self: object) -> list:
        raise NotConfiguredError("see https://x/?client_secret=SEKRET")

    monkeypatch.setattr(TraktClient, "watchlist", boom)
    with capture_logs() as logs:
        items = await TraktWatchlistSource("id", "secret").fetch(None, 10)  # type: ignore[arg-type]

    assert items == []
    assert logs and "SEKRET" not in str(logs)
