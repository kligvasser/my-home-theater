"""End-to-end scan against a temp filesystem, incl. idempotency (plan §5.2)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select

from homeTheater.db.models import OwnedFile, Title, TitleKind
from homeTheater.scanner import FileEntry, LocalFileSystem, scan_library


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


class ListFS:
    """In-memory FileSystem for entries a real filesystem can't hold (e.g.
    surrogate-escaped names, which APFS refuses to create)."""

    def __init__(self, base: str, files: dict[str, list[FileEntry]]) -> None:
        self.base = base
        self.files = files

    def resolve(self, root: str) -> str:
        return f"{self.base}/{root}"

    def walk(self, root: str) -> Iterator[FileEntry]:
        yield from self.files.get(root, [])


class FailingRootFS:
    """Delegates to a real filesystem but simulates an SMB outage for one root."""

    def __init__(self, inner: LocalFileSystem, bad_root: str) -> None:
        self.inner = inner
        self.bad_root = bad_root

    def resolve(self, root: str) -> str:
        return self.inner.resolve(root)

    def walk(self, root: str) -> Iterator[FileEntry]:
        if root == self.bad_root:
            raise OSError("SMB share unavailable")
        return self.inner.walk(root)


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
    assert stats.files_pruned == 0

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
    assert stats2.files_pruned == 0

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Title)) == 2
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 2


def test_deleted_file_is_pruned_on_rescan(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    _make_tree(media)
    fs = LocalFileSystem(base_dir=str(media))
    scan_library(fs, _roots())

    movie = media / "Movies" / "The Matrix (1999)" / "The Matrix (1999) 1080p BluRay x264.mkv"
    movie.unlink()

    stats2 = scan_library(fs, _roots())
    assert stats2.files_pruned == 1

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 1
        # Titles are kept: they hold metadata and candidate history.
        assert s.scalar(select(func.count()).select_from(Title)) == 2


def test_failed_root_is_not_pruned(config_file: Path, tmp_path: Path) -> None:
    """An SMB outage on one root must not wipe its rows; other roots proceed."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    _make_tree(media)
    fs = LocalFileSystem(base_dir=str(media))
    scan_library(fs, _roots())

    (media / "Movies" / "The Matrix (1999)" / "The Matrix (1999) 1080p BluRay x264.mkv").unlink()

    flaky = FailingRootFS(fs, bad_root="TV Shows")
    with pytest.raises(RuntimeError, match="SMB share unavailable"):
        scan_library(flaky, _roots())

    with session_scope() as s:
        # Movies root walked fine: its deleted file was pruned.
        assert (
            s.scalar(
                select(func.count()).select_from(OwnedFile).where(OwnedFile.kind == TitleKind.movie)
            )
            == 0
        )
        # TV root failed: its rows are intact.
        assert (
            s.scalar(
                select(func.count())
                .select_from(OwnedFile)
                .where(OwnedFile.kind == TitleKind.series)
            )
            == 1
        )


def test_bare_episode_names_resolve_via_parent_dirs(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    ep_dir = media / "TV Shows" / "The Wire" / "Season 02"
    ep_dir.mkdir(parents=True)
    (ep_dir / "S02E05.mkv").write_bytes(b"z" * 5)

    stats = scan_library(LocalFileSystem(base_dir=str(media)), {TitleKind.series: "TV Shows"})
    assert stats.files_added == 1
    assert stats.files_skipped == 0

    with session_scope() as s:
        title = s.scalar(select(Title))
        assert title is not None
        assert title.title == "The Wire"
        owned = s.scalar(select(OwnedFile))
        assert owned is not None
        assert owned.season == 2 and owned.episode == 5


def test_hidden_and_junk_entries_are_skipped(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    _make_tree(media)

    movie_dir = media / "Movies" / "The Matrix (1999)"
    # macOS AppleDouble resource fork: a "media" extension but garbage content.
    (movie_dir / "._The Matrix (1999) 1080p BluRay x264.mkv").write_bytes(b"\x00" * 4)
    # Synology thumbnail tree and recycle bin: never real, owned media.
    ea_dir = media / "Movies" / "@eaDir" / "The Matrix (1999)"
    ea_dir.mkdir(parents=True)
    (ea_dir / "SYNOPHOTO_THUMB.mkv").write_bytes(b"\x00" * 4)
    recycle = media / "Movies" / "#recycle"
    recycle.mkdir()
    (recycle / "Old Movie (2000) 720p.mkv").write_bytes(b"\x00" * 4)

    stats = scan_library(LocalFileSystem(base_dir=str(media)), _roots())

    assert stats.files_scanned == 3  # junk never even enters the walk
    assert stats.media_files == 2

    with session_scope() as s:
        titles = {t.title for t in s.scalars(select(Title))}
        assert titles == {"The Matrix", "Breaking Bad"}
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 2


def test_case_variant_files_share_one_title(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    movies = media / "Movies"
    movies.mkdir(parents=True)
    (movies / "The.Matrix.1999.1080p.BluRay.x264.mkv").write_bytes(b"a" * 8)
    (movies / "the matrix (1999).mkv").write_bytes(b"b" * 8)

    stats = scan_library(LocalFileSystem(base_dir=str(media)), {TitleKind.movie: "Movies"})

    assert stats.titles_created == 1
    assert stats.files_added == 2

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Title)) == 1
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 2


def test_scan_survives_undecodable_path(config_file: Path, tmp_path: Path) -> None:
    """A surrogate-escaped (non-UTF-8) filename is skipped, not fatal."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    base = str(tmp_path / "nas")
    good = FileEntry(
        path=f"{base}/Movies/Good Movie (2020) 1080p.mkv",
        name="Good Movie (2020) 1080p.mkv",
        parent=f"{base}/Movies",
        size=10,
    )
    bad = FileEntry(
        path=f"{base}/Movies/Bad\udcffMovie (2021).mkv",
        name="Bad\udcffMovie (2021).mkv",
        parent=f"{base}/Movies",
        size=10,
    )
    fs = ListFS(base, {"Movies": [bad, good]})

    stats = scan_library(fs, {TitleKind.movie: "Movies"})

    assert stats.files_skipped == 1
    assert stats.files_added == 1
    assert len(stats.errors) == 1 and "non-UTF-8" in stats.errors[0]

    with session_scope() as s:
        title = s.scalar(select(Title))
        assert title is not None and title.title == "Good Movie"


def test_scan_survives_db_error_for_one_file(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing per-file transaction is rolled back and recorded; the rest of
    the scan still commits (regression: one poisoned row used to sink the run)."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    _make_tree(media)

    import homeTheater.scanner.service as svc

    real_upsert = svc._upsert_owned_file

    def flaky_upsert(session: object, title: object, path: str, *args: object) -> bool:
        if "Matrix" in path:
            raise RuntimeError("simulated flush failure")
        return real_upsert(session, title, path, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "_upsert_owned_file", flaky_upsert)

    stats = scan_library(LocalFileSystem(base_dir=str(media)), _roots())

    assert stats.files_added == 1  # the episode made it in
    assert len(stats.errors) == 1 and "simulated flush failure" in stats.errors[0]

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 1
        ep = s.scalar(select(OwnedFile))
        assert ep is not None and ep.kind is TitleKind.series
