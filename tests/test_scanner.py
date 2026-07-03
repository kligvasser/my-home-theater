"""End-to-end scan against a temp filesystem, incl. idempotency (plan §5.2)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from homeTheater.db.models import OwnedFile, Title, TitleKind
from homeTheater.scanner import LocalFileSystem, scan_library


def _reset_singletons() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _make_tree(base: Path) -> None:
    movie_dir = base / "Movies" / "The Matrix (1999)"
    movie_dir.mkdir(parents=True)
    (movie_dir / "The Matrix (1999) 1080p BluRay x264.mkv").write_bytes(b"x" * 10)
    (movie_dir / "The Matrix (1999) 1080p BluRay x264.he.srt").write_text("sub")

    ep_dir = base / "TV Shows" / "Breaking Bad" / "Season 03"
    ep_dir.mkdir(parents=True)
    (ep_dir / "Breaking.Bad.S03E07.720p.HDTV.x264.mkv").write_bytes(b"y" * 20)


def _roots() -> dict[TitleKind, str]:
    return {TitleKind.movie: "Movies", TitleKind.series: "TV Shows"}


def test_scan_builds_catalog(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    _make_tree(media)

    fs = LocalFileSystem(base_dir=str(media))
    stats = scan_library(fs, _roots())

    assert stats.media_files == 2
    assert stats.titles_created == 2
    assert stats.files_added == 2
    assert stats.subtitles_found == 1

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Title)) == 2
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 2

        matrix = s.scalar(select(OwnedFile).where(OwnedFile.kind == TitleKind.movie))
        assert matrix is not None
        assert matrix.resolution == "1080p"
        assert matrix.subtitle_langs == ["he"]

        ep = s.scalar(select(OwnedFile).where(OwnedFile.kind == TitleKind.series))
        assert ep is not None
        assert ep.season == 3 and ep.episode == 7


def test_rescan_is_idempotent(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    _make_tree(media)
    fs = LocalFileSystem(base_dir=str(media))

    scan_library(fs, _roots())
    stats2 = scan_library(fs, _roots())

    # Second pass creates nothing new; it updates the two existing files.
    assert stats2.titles_created == 0
    assert stats2.files_added == 0
    assert stats2.files_updated == 2

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Title)) == 2
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 2
