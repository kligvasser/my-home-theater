"""Kodi .nfo and artwork export: what is written, what is left alone, what is skipped."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
import respx

POSTER = "https://image.tmdb.org/t/p/w780/p.jpg"
FANART = "https://image.tmdb.org/t/p/w1280/b.jpg"
JPEG = b"\xff\xd8\xff" + b"0" * 64


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str = ""):  # type: ignore[no-untyped-def]
    lib = tmp_path / "lib"
    cfg = tmp_path / "nfo.yaml"
    cfg.write_text(
        "nas: {share: S, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'n.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: []\n"
        f"  library_base_dir: {lib}\n" + extra
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    return lib, get_config()


def _add_movie(lib: Path, *, on_disk: bool = True, tmdb_id: int | None = 949) -> Path:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import Genre, MetadataCache, OwnedFile, Title, TitleKind

    video = lib / "Movies" / "Heat (1995)" / "Heat (1995).mkv"
    if on_disk:
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"v" * 100)
    with session_scope() as s:
        title = Title(
            kind=TitleKind.movie,
            title="Heat & <Co>",
            year=1995,
            tmdb_id=tmdb_id,
            imdb_id="tt0113277",
            runtime=170,
            overview="A crew of thieves.",
            release_date="1995-12-15",
            certification="R",
            imdb_rating=8.3,
            imdb_votes=700000,
            tmdb_rating=7.9,
            tmdb_votes=7000,
            directors=["Michael Mann"],
            cast_top=["Al Pacino"],
            origin_countries=["US"],
            poster_url="https://image.tmdb.org/t/p/w500/p.jpg",
        )
        title.genres = [Genre(name="Crime"), Genre(name="Action")]
        s.add(title)
        s.flush()
        s.add(
            OwnedFile(
                path="\\\\nas\\S\\Movies\\Heat (1995)\\Heat (1995).mkv",
                title_id=title.id,
                kind=TitleKind.movie,
            )
        )
        if tmdb_id is not None:
            s.add(
                MetadataCache(
                    provider="tmdb",
                    cache_key=f"details:v2:movie:{tmdb_id}:en-US",
                    fetched_at=utcnow(),
                    payload={
                        "poster_path": "/p.jpg",
                        "backdrop_path": "/b.jpg",
                        "tagline": "A Los Angeles crime saga",
                        "credits": {
                            "cast": [
                                {"name": "Al Pacino", "character": "Vincent Hanna"},
                                {"name": "Robert De Niro", "character": "Neil McCauley"},
                            ]
                        },
                    },
                )
            )
    return video


def _mock_images() -> tuple[respx.Route, respx.Route]:
    headers = {"content-type": "image/jpeg"}
    return (
        respx.get(POSTER).mock(return_value=httpx.Response(200, content=JPEG, headers=headers)),
        respx.get(FANART).mock(return_value=httpx.Response(200, content=JPEG, headers=headers)),
    )


@respx.mock
def test_writes_nfo_and_artwork_then_does_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import kodi_nfo

    lib, config = _setup(tmp_path, monkeypatch)
    video = _add_movie(lib)
    poster, fanart = _mock_images()

    stats = kodi_nfo.export_library(config)

    assert (stats.movies, stats.nfo_written, stats.artwork_written) == (1, 1, 2)
    assert stats.errors == []
    folder = video.parent
    assert (folder / "Heat (1995)-poster.jpg").read_bytes() == JPEG
    assert (folder / "Heat (1995)-fanart.jpg").read_bytes() == JPEG
    text = (folder / "Heat (1995).nfo").read_text(encoding="utf-8")
    assert kodi_nfo.MARKER in text
    movie = ET.fromstring(text)  # well-formed, with the title escaped
    assert movie.findtext("title") == "Heat & <Co>"
    assert movie.findtext("year") == "1995"
    assert movie.findtext("tagline") == "A Los Angeles crime saga"
    ids = {u.get("type"): u.text for u in movie.findall("uniqueid")}
    assert ids == {"tmdb": "949", "imdb": "tt0113277"}
    assert [g.text for g in movie.findall("genre")] == ["Action", "Crime"]
    assert [(a.findtext("name"), a.findtext("role")) for a in movie.findall("actor")] == [
        ("Al Pacino", "Vincent Hanna"),
        ("Robert De Niro", "Neil McCauley"),
    ]
    assert movie.find("ratings/rating[@name='imdb']").get("default") == "true"
    assert not [p for p in folder.iterdir() if p.name.endswith(".part")]

    again = kodi_nfo.export_library(config)

    assert (again.nfo_written, again.nfo_unchanged, again.artwork_written) == (0, 1, 0)
    assert poster.call_count == 1 and fanart.call_count == 1


@respx.mock
def test_someone_elses_kodi_nfo_is_kept_and_a_release_note_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import kodi_nfo

    lib, config = _setup(tmp_path, monkeypatch, "nfo: {artwork: false}\n")
    video = _add_movie(lib)
    nfo = video.with_suffix(".nfo")
    nfo.write_text("<movie><title>Mine</title></movie>")

    stats = kodi_nfo.export_library(config)

    assert (stats.nfo_foreign, stats.nfo_written, stats.artwork_written) == (1, 0, 0)
    assert "Mine" in nfo.read_text()

    nfo.write_bytes(b"\xdb\xdb RELEASE BY GROUP \xdb\xdb")
    stats = kodi_nfo.export_library(config)

    assert stats.nfo_written == 1
    assert kodi_nfo.MARKER in nfo.read_text(encoding="utf-8")


@respx.mock
def test_a_failed_image_is_reported_and_the_nfo_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import kodi_nfo

    lib, config = _setup(tmp_path, monkeypatch)
    video = _add_movie(lib)
    respx.get(POSTER).mock(return_value=httpx.Response(404))
    respx.get(FANART).mock(
        return_value=httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
    )

    stats = kodi_nfo.export_library(config)

    assert stats.nfo_written == 1 and stats.artwork_written == 0
    assert len(stats.errors) == 2
    assert video.with_suffix(".nfo").exists()
    assert not (video.parent / "Heat (1995)-poster.jpg").exists()


def test_a_movie_missing_from_the_nas_is_counted_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import kodi_nfo

    lib, config = _setup(tmp_path, monkeypatch)
    (lib / "Movies").mkdir(parents=True)
    video = _add_movie(lib, on_disk=False)

    stats = kodi_nfo.export_library(config)

    assert (stats.movies, stats.missing) == (0, 1)
    assert not video.parent.exists()


def test_image_paths_from_the_cache_are_not_trusted() -> None:
    from homeTheater.kodi_nfo import _image_path

    assert _image_path("/abc.jpg") == "/abc.jpg"
    for bad in ("abc.jpg", "/../x.jpg", "/a/b.jpg", "/x.svg", None, 5):
        assert _image_path(bad) is None


async def test_scheduled_job_is_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    lib, config = _setup(tmp_path, monkeypatch)
    video = _add_movie(lib)

    await jobs.run_nfo_job(config)

    assert not video.with_suffix(".nfo").exists()
    assert "nfo.enabled" in jobs.skip_reasons["nfo"]


def test_the_junk_sweep_keeps_what_the_export_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    from homeTheater import junk, kodi_nfo

    lib, config = _setup(tmp_path, monkeypatch, "nfo: {artwork: false}\n")
    video = _add_movie(lib)
    (lib / "TV Shows").mkdir()
    kodi_nfo.export_library(config)
    for name in ("Heat (1995)-poster.jpg", "Heat (1995)-fanart.jpg"):
        (video.parent / name).write_bytes(JPEG)
    old = time.time() - 86400
    for path in video.parent.iterdir():
        os.utime(path, (old, old))

    assert junk.find_junk(config).files == []


def _add_show(lib: Path) -> Path:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.db.models import MetadataCache, OwnedFile, Title, TitleKind

    season = lib / "TV Shows" / "Silo" / "Season 01"
    season.mkdir(parents=True)
    names = ("Silo S01E01.mkv", "Silo S01E02E03.mkv")
    for name in names:
        (season / name).write_bytes(b"v" * 10)
    with session_scope() as s:
        title = Title(
            kind=TitleKind.series,
            title="Silo",
            year=2023,
            tmdb_id=125988,
            tvdb_id=403245,
            overview="Ten thousand people underground.",
            series_status="Returning Series",
            cast_top=["Rebecca Ferguson"],
        )
        s.add(title)
        s.flush()
        for name, first, last in zip(names, (1, 2), (None, 3), strict=True):
            s.add(
                OwnedFile(
                    path=f"\\\\nas\\S\\TV Shows\\Silo\\Season 01\\{name}",
                    title_id=title.id,
                    kind=TitleKind.series,
                    season=1,
                    episode=first,
                    episode_end=last,
                )
            )
        s.add(
            MetadataCache(
                provider="tmdb",
                cache_key="details:v2:series:125988:en-US",
                fetched_at=utcnow(),
                payload={"poster_path": "/p.jpg", "backdrop_path": "/b.jpg"},
            )
        )
    return season


@respx.mock
def test_show_gets_tvshow_nfo_artwork_and_an_nfo_per_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import kodi_nfo

    lib, config = _setup(tmp_path, monkeypatch)
    season = _add_show(lib)
    _mock_images()

    stats = kodi_nfo.export_library(config)

    assert (stats.shows, stats.episodes, stats.nfo_written, stats.artwork_written) == (1, 2, 3, 2)
    assert stats.errors == []
    show = season.parent
    assert (show / "poster.jpg").exists() and (show / "fanart.jpg").exists()
    tvshow = ET.fromstring((show / "tvshow.nfo").read_text(encoding="utf-8"))
    assert tvshow.tag == "tvshow" and tvshow.findtext("title") == "Silo"
    ids = {u.get("type"): u.text for u in tvshow.findall("uniqueid")}
    assert ids == {"tmdb": "125988", "tvdb": "403245"}

    single = ET.fromstring((season / "Silo S01E01.nfo").read_text(encoding="utf-8"))
    assert (single.findtext("season"), single.findtext("episode")) == ("1", "1")
    assert single.findtext("title") == "Episode 1"
    assert single.findtext("showtitle") == "Silo"
    # A double episode is two blocks in one file, which is not a single XML document.
    double = (season / "Silo S01E02E03.nfo").read_text(encoding="utf-8")
    assert double.count("<episodedetails>") == 2
    assert "<episode>2</episode>" in double and "<episode>3</episode>" in double

    again = kodi_nfo.export_library(config)
    assert (again.nfo_written, again.nfo_unchanged, again.artwork_written) == (0, 3, 0)


def test_junk_takes_our_nfo_once_its_episode_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    from homeTheater import junk, kodi_nfo

    assert junk._OUR_NFO_MARKER == kodi_nfo.MARKER
    lib, config = _setup(tmp_path, monkeypatch, "nfo: {artwork: false}\n")
    (lib / "Movies").mkdir(parents=True)
    season = _add_show(lib)
    kodi_nfo.export_library(config)
    (season / "Silo S01E01.mkv").unlink()
    old = time.time() - 86400
    for folder in (season, season.parent):
        for path in folder.iterdir():
            if path.is_file():
                os.utime(path, (old, old))

    plan = junk.find_junk(config)

    assert [(os.path.basename(f.path), f.reason) for f in plan.files] == [
        ("Silo S01E01.nfo", junk.REASON_STALE_NFO)
    ]
