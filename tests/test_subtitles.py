"""Bazarr client + subtitle sweep, with Bazarr mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from homeTheater.db.models import TitleKind

BZ = "http://bazarr.local"

WANTED_MOVIES = {
    "data": [
        {
            "radarrId": 5,
            "title": "The Matrix",
            "year": 1999,
            "missing_subtitles": [{"code2": "he", "name": "Hebrew"}],
        },
        {
            "radarrId": 6,
            "title": "Only English Wanted",
            "missing_subtitles": [{"code2": "en", "name": "English"}],
        },
    ]
}
WANTED_EPISODES = {
    "data": [
        {
            "sonarrSeriesId": 3,
            "sonarrEpisodeId": 77,
            "seriesTitle": "Fauda",
            "episodeTitle": "S1E1",
            "missing_subtitles": [{"code2": "he"}],
        }
    ]
}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BAZARR_URL", BZ)
    monkeypatch.setenv("BAZARR_API_KEY", "bz")
    _reset()


@respx.mock
async def test_bazarr_client_parses_wanted(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.subtitles import BazarrClient

    respx.get(f"{BZ}/api/movies/wanted").mock(return_value=httpx.Response(200, json=WANTED_MOVIES))

    async with httpx.AsyncClient() as http:
        client = BazarrClient(BZ, "bz", http)
        wanted = await client.wanted_movies()

    assert len(wanted) == 2
    assert wanted[0].kind is TitleKind.movie
    assert wanted[0].radarr_id == 5
    assert wanted[0].missing_langs == ["he"]


@respx.mock
async def test_sweep_triggers_only_target_language(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch)
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.subtitles import sweep_missing

    init_db()
    respx.get(f"{BZ}/api/movies/wanted").mock(return_value=httpx.Response(200, json=WANTED_MOVIES))
    respx.get(f"{BZ}/api/episodes/wanted").mock(
        return_value=httpx.Response(200, json=WANTED_EPISODES)
    )
    movie_search = respx.patch(f"{BZ}/api/movies").mock(return_value=httpx.Response(204))
    ep_search = respx.patch(f"{BZ}/api/episodes").mock(return_value=httpx.Response(204))

    stats = await sweep_missing(get_config())

    # Only the Hebrew-wanted movie (radarr 5) is searched, not the English-only one.
    assert stats.wanted_movies == 1
    assert stats.searched_movies == 1
    assert stats.wanted_episodes == 1
    assert stats.searched_episodes == 1
    assert movie_search.call_count == 1
    assert ep_search.call_count == 1


async def test_sweep_requires_bazarr_config(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BAZARR_URL", raising=False)
    monkeypatch.delenv("BAZARR_API_KEY", raising=False)
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.subtitles import sweep_missing

    init_db()
    with pytest.raises(ValueError, match="BAZARR_URL"):
        await sweep_missing(get_config())
