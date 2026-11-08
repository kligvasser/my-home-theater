"""End-to-end scan against a temp filesystem, incl. idempotency (plan §5.2)."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select

from homeTheater.config.settings import Scanner
from homeTheater.db.models import JobRun, OwnedFile, RunStatus, Title, TitleKind
from homeTheater.scanner import FileEntry, LocalFileSystem, SMBFileSystem, scan_library


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

    def flaky_upsert(
        session: object, path: str, *args: object, **kwargs: object
    ) -> tuple[bool, bool]:
        if "Matrix" in path:
            raise RuntimeError("simulated flush failure")
        return real_upsert(session, path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(svc, "_upsert_owned_file", flaky_upsert)

    stats = scan_library(LocalFileSystem(base_dir=str(media)), _roots())

    assert stats.files_added == 1  # the episode made it in
    assert len(stats.errors) == 1 and "simulated flush failure" in stats.errors[0]

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 1
        ep = s.scalar(select(OwnedFile))
        assert ep is not None and ep.kind is TitleKind.series


def test_subs_folder_attaches_to_movie(config_file: Path, tmp_path: Path) -> None:
    """Movies/<Title (Year)>/Subs/<lang>.srt counts toward that movie's coverage."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile
    from homeTheater.scanner import scan_library
    from homeTheater.scanner.filesystem import LocalFileSystem

    init_db()
    base = tmp_path / "nas-movies"
    if True:
        movie_dir = Path(base) / "Movies" / "Heat (1995)"
        subs = movie_dir / "Subs"
        subs.mkdir(parents=True)
        (movie_dir / "Heat (1995) 1080p.mkv").write_bytes(b"x")
        (subs / "2_English.srt").write_text("sub")
        (subs / "Hebrew.srt").write_text("sub")

        scan_library(LocalFileSystem(base), {TitleKind.movie: "Movies"})

    with session_scope() as s:
        owned = s.query(OwnedFile).one()
        assert owned.subtitle_langs == ["en", "he"]


def test_season_subs_folder_matches_episodes_by_stem(config_file: Path, tmp_path: Path) -> None:
    """Season 01/Subs/<episode stem>.<lang>.srt attaches per episode; a
    standalone name is NOT attached when the folder has multiple episodes."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile
    from homeTheater.scanner import scan_library
    from homeTheater.scanner.filesystem import LocalFileSystem

    init_db()
    base = tmp_path / "nas-tv"
    if True:
        season = Path(base) / "TV Shows" / "The Wire" / "Season 01"
        subs = season / "Subs"
        subs.mkdir(parents=True)
        (season / "The Wire - S01E01 - The Target.mkv").write_bytes(b"x")
        (season / "The Wire - S01E02 - The Detail.mkv").write_bytes(b"x")
        (subs / "The Wire - S01E01 - The Target.he.srt").write_text("sub")
        (subs / "English.srt").write_text("ambiguous")  # 2 episodes: skip

        scan_library(LocalFileSystem(base), {TitleKind.series: "TV Shows"})

    with session_scope() as s:
        rows = {Path(o.path).name: o.subtitle_langs for o in s.query(OwnedFile).all()}
        assert rows["The Wire - S01E01 - The Target.mkv"] == ["he"]
        assert rows["The Wire - S01E02 - The Detail.mkv"] is None


def test_rescan_keeps_enriched_title_link(config_file: Path, tmp_path: Path) -> None:
    """After enrichment renames a title to its canonical TMDb form, a re-scan
    must NOT create a duplicate title or repoint the file."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import OwnedFile, Title
    from homeTheater.scanner import scan_library
    from homeTheater.scanner.filesystem import LocalFileSystem

    init_db()
    base = tmp_path / "nas"
    movie_dir = base / "Movies" / "Better.Call.Soul.2015"  # misspelled release name
    movie_dir.mkdir(parents=True)
    (movie_dir / "Better.Call.Soul.2015.1080p.mkv").write_bytes(b"x")

    scan_library(LocalFileSystem(str(base)), {TitleKind.movie: "Movies"})

    with session_scope() as s:
        title = s.query(Title).one()
        title_id = title.id
        title.title = "Better Call Saul"  # enrichment canonicalized the name
        title.tmdb_id = 60059

    stats = scan_library(LocalFileSystem(str(base)), {TitleKind.movie: "Movies"})
    assert stats.titles_created == 0  # no duplicate from the rename

    with session_scope() as s:
        assert s.query(Title).count() == 1
        owned = s.query(OwnedFile).one()
        assert owned.title_id == title_id  # link preserved


def test_series_extras_are_skipped(config_file: Path, tmp_path: Path) -> None:
    """A featurette in a season folder (no SxxExx) is skipped, not cataloged as a
    junk title; real episodes in the same folder still import."""
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    ep_dir = media / "TV Shows" / "Ted Lasso" / "Season 02"
    ep_dir.mkdir(parents=True)
    (ep_dir / "Ted Lasso (2020) - S02E01 - Goodbye Earl.mkv").write_bytes(b"e" * 5)
    (ep_dir / "Season 2 - The Cast Ask Each Other Anything - IMDB.mkv").write_bytes(b"x" * 5)

    stats = scan_library(LocalFileSystem(base_dir=str(media)), {TitleKind.series: "TV Shows"})
    assert stats.files_added == 1 and stats.files_skipped == 1

    with session_scope() as s:
        titles = [t.title for t in s.scalars(select(Title))]
        assert titles == ["Ted Lasso"]  # no junk "The Cast Ask..." title
        owned = s.scalars(select(OwnedFile)).all()
        assert len(owned) == 1 and owned[0].episode == 1


def test_skipped_extra_keeps_its_row(config_file: Path, tmp_path: Path) -> None:
    """A file with no episode number stays uncataloged when new, but an existing
    row survives: the file was walked, so it isn't gone (L6). Hebrew-named real
    episodes take this same path."""
    _reset_singletons()
    from homeTheater.db import init_db, session_scope

    init_db()
    media = tmp_path / "media"
    ep_dir = media / "TV Shows" / "Ted Lasso" / "Season 02"
    ep_dir.mkdir(parents=True)
    extra = ep_dir / "Season 2 - Meet the Cast.mkv"
    extra.write_bytes(b"x" * 5)

    with session_scope() as s:
        t = Title(title="Meet the Cast", kind=TitleKind.series)
        s.add(t)
        s.flush()
        s.add(OwnedFile(path=str(extra), title_id=t.id, kind=TitleKind.series))

    stats = scan_library(LocalFileSystem(base_dir=str(media)), {TitleKind.series: "TV Shows"})
    assert stats.files_pruned == 0
    assert stats.files_skipped == 1

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 1


def _seed_movies(base: str, count: int) -> list[str]:
    """Pre-existing catalog rows under ``base``/Movies; returns their paths."""

    from homeTheater.db import init_db, session_scope

    init_db()
    paths = [f"{base}/Movies/Movie {i:03} (2001).mkv" for i in range(count)]
    with session_scope() as s:
        title = Title(title="Seeded", kind=TitleKind.movie)
        s.add(title)
        s.flush()
        s.add_all(OwnedFile(path=p, title_id=title.id, kind=TitleKind.movie) for p in paths)
    return paths


def _entries(base: str, paths: list[str]) -> list[FileEntry]:
    return [
        FileEntry(path=p, name=p.rsplit("/", 1)[1], parent=f"{base}/Movies", size=1) for p in paths
    ]


def _row_paths() -> set[str]:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return set(s.scalars(select(OwnedFile.path)).all())


def _last_run() -> tuple[RunStatus, dict[str, Any]]:
    from homeTheater.db import session_scope

    with session_scope() as s:
        run = s.scalars(select(JobRun).order_by(JobRun.id.desc())).first()
        assert run is not None and run.stats is not None
        return run.status, run.stats


# --- L1: real walkers, driven through their real error paths ---------------


class FakeSmbShare:
    """A fake ``smbclient._os.scandir`` so the real ``smbclient.walk`` runs.

    ``fail_open`` raises when a directory is opened; ``fail_listing`` raises after
    one entry, part-way through the listing, as a dropped session does. The real
    scandir is a generator, so even an open failure arrives from ``next()``.
    """

    def __init__(self, tree: dict[str, list[tuple[str, bool]]]) -> None:
        self.tree = tree
        self.fail_open: str | None = None
        self.fail_listing: str | None = None

    def scandir(self, path: str, **_: object) -> Iterator[SimpleNamespace]:
        if path == self.fail_open:
            raise OSError(13, "Permission denied", path)
        for i, (name, is_dir) in enumerate(self.tree[path]):
            if path == self.fail_listing and i == 1:
                raise OSError(5, "Connection dropped", path)
            yield SimpleNamespace(
                name=name,
                path=f"{path}\\{name}",
                is_dir=lambda d=is_dir: d,
                is_symlink=lambda: False,
            )


SMB_ROOT = "\\\\nas\\share\\Movies"


@pytest.fixture
def smb_share(monkeypatch: pytest.MonkeyPatch) -> FakeSmbShare:
    smbclient = pytest.importorskip("smbclient")
    share = FakeSmbShare(
        {
            SMB_ROOT: [("A (2001)", True), ("B (2002)", True)],
            f"{SMB_ROOT}\\A (2001)": [("A (2001).mkv", False), ("A (2001).en.srt", False)],
            f"{SMB_ROOT}\\B (2002)": [("B (2002).mkv", False)],
        }
    )
    monkeypatch.setattr("smbclient._os.scandir", share.scandir)
    monkeypatch.setattr("smbclient._os.lstat", lambda p, **_: SimpleNamespace(st_mode=0o040755))
    monkeypatch.setattr(smbclient, "stat", lambda p, **_: SimpleNamespace(st_size=7))
    return share


def _smb_fs() -> SMBFileSystem:
    fs = SMBFileSystem(host="nas", share="share")
    fs._ensure_session = lambda server: None  # type: ignore[method-assign]  # faked below
    return fs


def test_smb_walk_lists_a_healthy_share(smb_share: FakeSmbShare) -> None:
    names = {e.name for e in _smb_fs().walk("Movies")}
    assert names == {"A (2001).mkv", "A (2001).en.srt", "B (2002).mkv"}


@pytest.mark.parametrize(
    ("attr", "path"),
    [
        ("fail_open", SMB_ROOT),
        ("fail_open", f"{SMB_ROOT}\\B (2002)"),
        ("fail_listing", SMB_ROOT),
        ("fail_listing", f"{SMB_ROOT}\\A (2001)"),
    ],
    ids=["root", "subdir", "root-mid-listing", "subdir-mid-listing"],
)
def test_smb_read_errors_fail_the_walk_and_keep_rows(
    config_file: Path, smb_share: FakeSmbShare, attr: str, path: str
) -> None:
    _reset_singletons()
    setattr(smb_share, attr, path)
    from homeTheater.db import init_db, session_scope

    init_db()
    rows = [f"{SMB_ROOT}\\B (2002)\\B (2002).mkv"] + [
        f"{SMB_ROOT}\\Gone\\{i}.mkv" for i in range(3)
    ]
    with session_scope() as s:
        title = Title(title="B", year=2002, kind=TitleKind.movie)
        s.add(title)
        s.flush()
        s.add_all(OwnedFile(path=p, title_id=title.id, kind=TitleKind.movie) for p in rows)

    with pytest.raises(OSError):
        list(_smb_fs().walk("Movies"))
    with pytest.raises(RuntimeError, match="Scan failed"):
        scan_library(_smb_fs(), {TitleKind.movie: "Movies"})

    assert _row_paths() == set(rows)
    assert _last_run()[0] is RunStatus.failed


@contextlib.contextmanager
def _unreadable(directory: Path) -> Iterator[None]:
    directory.chmod(0o000)
    try:
        if os.access(directory, os.R_OK):
            pytest.skip("running as a user who ignores permissions")
        yield
    finally:
        directory.chmod(0o755)


def test_local_walk_raises_on_missing_root(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list(LocalFileSystem(tmp_path).walk("Movies"))


def test_local_walk_raises_on_unreadable_root(tmp_path: Path) -> None:
    root = tmp_path / "Movies"
    root.mkdir()
    with _unreadable(root), pytest.raises(PermissionError):
        list(LocalFileSystem(tmp_path).walk("Movies"))


def test_local_walk_raises_on_unreadable_subdir(tmp_path: Path) -> None:
    locked = tmp_path / "Movies" / "Locked (2001)"
    locked.mkdir(parents=True)
    with _unreadable(locked), pytest.raises(PermissionError):
        list(LocalFileSystem(tmp_path).walk("Movies"))


def test_local_walk_raises_on_error_mid_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Movies"
    root.mkdir()
    (root / "a.mkv").write_bytes(b"x")
    (root / "b.mkv").write_bytes(b"x")
    real_scandir = os.scandir

    class DropsAfterOne:
        def __init__(self, path: Any) -> None:
            self.inner = real_scandir(path)
            self.served = 0

        def __enter__(self) -> DropsAfterOne:
            return self

        def __exit__(self, *exc: object) -> None:
            self.inner.close()

        def __iter__(self) -> DropsAfterOne:
            return self

        def __next__(self) -> os.DirEntry[str]:
            if self.served == 1:
                raise OSError(5, "Input/output error")
            self.served += 1
            return next(self.inner)

    monkeypatch.setattr(os, "scandir", DropsAfterOne)
    with pytest.raises(OSError, match="Input/output"):
        list(LocalFileSystem(tmp_path).walk("Movies"))


def test_unreadable_subdir_fails_scan_without_pruning(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db

    init_db()
    media = tmp_path / "media"
    _make_tree(media)
    fs = LocalFileSystem(base_dir=str(media))
    scan_library(fs, _roots())
    before = _row_paths()

    locked = media / "TV Shows" / "Breaking Bad"
    with _unreadable(locked), pytest.raises(RuntimeError, match="Scan failed"):
        scan_library(fs, _roots())

    assert _row_paths() == before


def test_missing_root_fails_scan_without_pruning(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db

    init_db()
    media = tmp_path / "media"
    _make_tree(media)
    fs = LocalFileSystem(base_dir=str(media))
    scan_library(fs, _roots())
    before = _row_paths()

    shutil.rmtree(media / "TV Shows")
    with pytest.raises(RuntimeError, match="Scan failed"):
        scan_library(fs, {TitleKind.series: "TV Shows"}, force_prune=True)

    assert _row_paths() == before


def test_empty_root_with_catalog_rows_is_a_failure(config_file: Path, tmp_path: Path) -> None:
    """A stale mount point reads as an empty directory, and any row count refuses
    it. A real emptied root stays clearable with force."""

    _reset_singletons()
    from homeTheater.db import init_db

    init_db()
    media = tmp_path / "media"
    _make_tree(media)
    fs = LocalFileSystem(base_dir=str(media))
    scan_library(fs, _roots())

    shutil.rmtree(media / "TV Shows")
    (media / "TV Shows").mkdir()

    with pytest.raises(RuntimeError, match="reads as empty"):
        scan_library(fs, {TitleKind.series: "TV Shows"})
    assert len(_row_paths()) == 2

    stats = scan_library(fs, {TitleKind.series: "TV Shows"}, force_prune=True)
    assert stats.files_pruned == 1


def test_empty_root_with_no_rows_is_fine(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db

    init_db()
    (tmp_path / "Movies").mkdir()
    stats = scan_library(LocalFileSystem(tmp_path), {TitleKind.movie: "Movies"})
    assert stats.files_scanned == 0


# --- L2: prune limits ------------------------------------------------------


def test_mass_prune_is_refused_and_rows_kept(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 20)
    new = f"{base}/Movies/Brand New (2024).mkv"
    fs = ListFS(base, {"Movies": _entries(base, [*paths[:14], new])})

    with pytest.raises(RuntimeError, match="refusing to prune 6 of 20"):
        scan_library(fs, {TitleKind.movie: "Movies"})

    assert _row_paths() == {*paths, new}  # nothing lost; the new file still landed
    status, stats = _last_run()
    assert status is RunStatus.failed
    assert stats["files_pruned"] == 0
    assert any("--force-prune" in e for e in stats["errors"])


def test_force_prune_overrides_the_limit(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 20)
    fs = ListFS(base, {"Movies": _entries(base, paths[:14])})

    stats = scan_library(fs, {TitleKind.movie: "Movies"}, force_prune=True)

    assert stats.files_pruned == 6
    assert _row_paths() == set(paths[:14])


def test_small_library_can_lose_one_file(config_file: Path, tmp_path: Path) -> None:
    """1 of 10 is 10%: the floor is what keeps this from needing --force-prune."""

    _reset_singletons()
    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 10)
    fs = ListFS(base, {"Movies": _entries(base, paths[1:])})

    stats = scan_library(fs, {TitleKind.movie: "Movies"})

    assert stats.files_pruned == 1


def test_a_refused_root_does_not_block_the_other_root(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 20)
    episode = f"{base}/TV/Show/S01E01.mkv"
    from homeTheater.db import session_scope

    with session_scope() as s:
        title = s.scalars(select(Title)).one()
        s.add(OwnedFile(path=episode, title_id=title.id, kind=TitleKind.series))
    fs = ListFS(base, {"Movies": _entries(base, paths[:10]), "TV": [_junk_entry(base)]})

    with pytest.raises(RuntimeError, match="refusing to prune"):
        scan_library(fs, {TitleKind.movie: "Movies", TitleKind.series: "TV"})

    # TV walked fine and pruned; the refused Movies root kept every row.
    assert _row_paths() == set(paths)


def _junk_entry(base: str) -> FileEntry:
    return FileEntry(path=f"{base}/TV/notes.txt", name="notes.txt", parent=f"{base}/TV", size=1)


@pytest.mark.parametrize(
    ("stale", "total", "refused"),
    [
        (5, 10, False),  # at the floor: percent doesn't apply yet
        (6, 10, True),
        (10, 200, False),  # exactly 5%
        (11, 200, True),
        (100, 10_000, False),  # at the row cap
        (101, 10_000, True),  # only 1%, but over the row cap
    ],
)
def test_prune_limit_boundaries(stale: int, total: int, refused: bool) -> None:
    from homeTheater.scanner.service import PruneRefusedError, _check_prune_allowed

    if refused:
        with pytest.raises(PruneRefusedError):
            _check_prune_allowed(stale=stale, total=total, walked=1, limits=Scanner())
    else:
        _check_prune_allowed(stale=stale, total=total, walked=1, limits=Scanner())


def test_run_scan_applies_configured_limits(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file.write_text(
        config_file.read_text() + "scanner:\n  max_prune_rows: 1\n  prune_floor_rows: 0\n"
    )
    _reset_singletons()
    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 10)
    fs = ListFS(base, {"Movies": _entries(base, paths[2:])})
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: fs)
    monkeypatch.setattr("homeTheater.scanner.config_roots", lambda cfg: {TitleKind.movie: "Movies"})

    from homeTheater.config import get_config
    from homeTheater.scanner import run_scan

    with pytest.raises(RuntimeError, match="refusing to prune 2 of 10"):
        run_scan(get_config())
    assert run_scan(get_config(), force_prune=True).files_pruned == 2


def _run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    from homeTheater import main

    monkeypatch.setattr(sys, "argv", ["home-theater", *argv])
    main.main()


@pytest.mark.parametrize(
    ("argv", "expected"), [(["scan"], False), (["scan", "--force-prune"], True)]
)
def test_scan_cli_force_prune_flag(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: bool
) -> None:
    _reset_singletons()
    calls: list[bool] = []

    def fake_run_scan(config: object, *, force_prune: bool = False) -> Any:
        calls.append(force_prune)
        return SimpleNamespace(as_dict=dict)

    monkeypatch.setattr("homeTheater.scanner.run_scan", fake_run_scan)
    _run_cli(monkeypatch, *argv)
    assert calls == [expected]


def test_scan_cli_reports_a_refused_prune_without_a_traceback(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _reset_singletons()

    def refused(config: object, *, force_prune: bool = False) -> Any:
        raise RuntimeError("Scan failed: ['Movies: refusing to prune 6 of 20']")

    monkeypatch.setattr("homeTheater.scanner.run_scan", refused)
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, "scan")
    assert exc.value.code == 1
    assert "refusing to prune" in capsys.readouterr().out


# --- L5: scans vs concurrent imports and each other ------------------------


class ImportDuringWalkFS(ListFS):
    """Registers a file the moment the walk has passed its folder."""

    def __init__(
        self, base: str, files: dict[str, list[FileEntry]], on_walk: Callable[[], None]
    ) -> None:
        super().__init__(base, files)
        self.on_walk = on_walk

    def walk(self, root: str) -> Iterator[FileEntry]:
        yield from super().walk(root)
        self.on_walk()


def test_row_created_during_the_walk_is_not_pruned(config_file: Path, tmp_path: Path) -> None:
    """The file finished importing after the walk passed its folder: unseen, but
    its row is newer than the walk. The older unseen row still goes."""

    _reset_singletons()
    from homeTheater.db import session_scope

    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 3)
    imported = f"{base}/Movies/Just Imported (2024).mkv"

    def register_import() -> None:
        with session_scope() as s:
            title = s.scalars(select(Title)).one()
            s.add(OwnedFile(path=imported, title_id=title.id, kind=TitleKind.movie))

    fs = ImportDuringWalkFS(base, {"Movies": _entries(base, paths[:2])}, register_import)

    stats = scan_library(fs, {TitleKind.movie: "Movies"})

    assert stats.files_pruned == 1
    assert _row_paths() == {*paths[:2], imported}


def test_run_scan_refuses_while_another_scan_holds_the_lock(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_singletons()
    from homeTheater.config import get_config
    from homeTheater.errors import JobBusyError
    from homeTheater.locks import job_lock
    from homeTheater.scanner import run_scan

    def no_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("scan ran without the lock")

    monkeypatch.setattr("homeTheater.scanner.scan_library", no_scan)
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: object())
    cfg = get_config()
    with job_lock(cfg, "scan"), pytest.raises(JobBusyError):
        run_scan(cfg)


def test_scan_cli_exits_2_while_a_scan_runs(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reset_singletons()
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock

    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: object())
    with job_lock(get_config(), "scan"), pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, "scan")
    assert exc.value.code == 2


def test_scan_endpoint_reports_already_running(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset_singletons()
    from fastapi.testclient import TestClient

    from homeTheater.api import create_app
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.locks import job_lock

    init_db()
    with TestClient(create_app()) as client, job_lock(get_config(), "scan"):
        r = client.post("/api/pipeline/scan", headers={"X-Auth-Token": "tok"})
    assert r.status_code == 200
    assert r.json()["started"] is False and "already running" in r.json()["message"]


def test_scan_endpoint_takes_the_lock(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    _reset_singletons()
    from fastapi.testclient import TestClient

    from homeTheater.api import create_app
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.locks import is_job_running
    from homeTheater.scanner import ScanStats

    init_db()
    held: list[bool] = []

    def fake_scan(*args: object, **kwargs: object) -> ScanStats:
        held.append(is_job_running(get_config(), "scan"))
        return ScanStats()

    monkeypatch.setattr("homeTheater.scanner.scan_library", fake_scan)
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: object())
    with TestClient(create_app()) as client:
        r = client.post("/api/pipeline/scan", headers={"X-Auth-Token": "tok"})
    assert r.json()["started"] is True
    assert held == [True]


# --- L6: only "not walked" means gone ---------------------------------------


@pytest.mark.parametrize("failure", ["unparsable", "parser-raises", "db-error"])
def test_file_that_fails_processing_keeps_its_row(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _reset_singletons()
    import homeTheater.scanner.service as svc

    base = str(tmp_path / "nas")
    paths = _seed_movies(base, 3)

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated")

    if failure == "unparsable":
        monkeypatch.setattr(svc, "parse_media", lambda *a, **k: None)
    elif failure == "parser-raises":
        monkeypatch.setattr(svc, "parse_media", boom)
    else:
        monkeypatch.setattr(svc, "_upsert_owned_file", boom)

    stats = scan_library(
        ListFS(base, {"Movies": _entries(base, paths)}), {TitleKind.movie: "Movies"}
    )

    assert stats.files_pruned == 0
    assert _row_paths() == set(paths)


def _series_file(media: Path, rel: str) -> None:
    path = media / "TV Shows" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_one_show_folder_is_one_title_whatever_the_files_are_called(
    config_file: Path, tmp_path: Path
) -> None:
    """L8: The Killing's and Severance's files split on filename."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner import LocalFileSystem, scan_library

    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    _series_file(media, "The.Killing/Season 01/bkd-tk.s01e06.mkv")
    _series_file(media, "The.Killing/Season 02/the.killing.s02e01.mkv")
    _series_file(media, "The.Killing/Season 03/pfa-tk.s03e01.mkv")
    _series_file(media, "Severance/Season 01/Severance.S01E01.mkv")
    _series_file(media, "Severance/Season 02/Severance (2021) - S02E01.mkv")

    init_db()
    scan_library(LocalFileSystem(base_dir=str(media)), _roots())

    with session_scope() as s:
        assert sorted(s.scalars(select(Title.title))) == ["Severance", "The Killing"]
        assert s.scalar(select(func.count()).select_from(OwnedFile)) == 5


def test_new_episode_joins_a_title_tmdb_renamed(config_file: Path, tmp_path: Path) -> None:
    """Folder ``Better Call Soul`` is the enriched ``Better Call Saul``: its name
    alone can't find the row, the files already under the folder do."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner import LocalFileSystem, scan_library

    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    _series_file(media, "Better Call Soul/Season 01/bcs.s01e01.mkv")
    fs = LocalFileSystem(base_dir=str(media))

    init_db()
    scan_library(fs, _roots())
    with session_scope() as s:
        title = s.scalars(select(Title)).one()
        title.title, title.year, title.tmdb_id = "Better Call Saul", 2015, 60059

    _series_file(media, "Better Call Soul/Season 01/bcs.s01e02.mkv")
    scan_library(fs, _roots())

    with session_scope() as s:
        title = s.scalars(select(Title)).one()
        assert title.title == "Better Call Saul" and len(title.owned_files) == 2


def test_year_only_tells_apart_same_named_series_when_both_have_one(
    config_file: Path, tmp_path: Path
) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner import LocalFileSystem, scan_library

    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    _series_file(media, "Battlestar Galactica (1978)/Season 01/g.s01e01.mkv")
    _series_file(media, "Battlestar Galactica (2004)/Season 01/g.s01e01.mkv")

    init_db()
    with session_scope() as s:
        s.add(Title(title="Dark Matter", year=2024, kind=TitleKind.series))
    _series_file(media, "Dark Matter/Season 01/Dark.Matter.2024.S01E09.mkv")
    scan_library(LocalFileSystem(base_dir=str(media)), _roots())

    with session_scope() as s:
        rows = s.execute(select(Title.title, Title.year).order_by(Title.year)).all()
    assert [tuple(r) for r in rows] == [
        ("Battlestar Galactica", 1978),
        ("Battlestar Galactica", 2004),
        ("Dark Matter", 2024),  # the enriched row, not a year-less second one
    ]


def test_multi_episode_file_stores_episode_end(config_file: Path, tmp_path: Path) -> None:
    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.scanner import LocalFileSystem, scan_library

    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    _series_file(media, "Show/Season 01/Show.S01E01E02.mkv")
    init_db()
    scan_library(LocalFileSystem(base_dir=str(media)), _roots())
    with session_scope() as s:
        owned = s.scalars(select(OwnedFile)).one()
        assert (owned.episode, owned.episode_end) == (1, 2)


def test_subtitle_stored_during_a_walk_survives_the_upsert(
    config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S20: the walk's snapshot of sidecars must not erase a language the sweep
    stored after the walk passed that folder."""

    _reset_singletons()
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Subtitle
    from homeTheater.scanner import LocalFileSystem, scan_library

    media = tmp_path / "media"
    (media / "Movies").mkdir(parents=True)
    _series_file(media, "Show/Season 01/Show.S01E01.mkv")
    fs = LocalFileSystem(base_dir=str(media))
    init_db()
    scan_library(fs, _roots())

    real_walk = fs.walk

    def walk_then_subtitle(root: str):  # type: ignore[no-untyped-def]
        entries = list(real_walk(root))
        if root != "TV Shows":
            return entries
        with session_scope() as s:
            owned = s.scalars(select(OwnedFile)).one()
            owned.subtitle_langs = ["he"]
            s.add(
                Subtitle(
                    title_id=owned.title_id, owned_file_id=owned.id, lang="he", status="downloaded"
                )
            )
        return entries

    monkeypatch.setattr(fs, "walk", walk_then_subtitle)
    scan_library(fs, _roots())
    with session_scope() as s:
        assert s.scalars(select(OwnedFile)).one().subtitle_langs == ["he"]

    # An old row is no evidence: the file's sidecar really has gone.
    monkeypatch.setattr(fs, "walk", real_walk)
    scan_library(fs, _roots())
    with session_scope() as s:
        assert s.scalars(select(OwnedFile)).one().subtitle_langs is None


def test_missing_nas_settings_are_not_configured_errors(config_file: Path) -> None:
    """L20: scheduler jobs skip NotConfiguredError; a bare ValueError was a failure."""

    _reset_singletons()
    from homeTheater.config import get_config
    from homeTheater.errors import NotConfiguredError
    from homeTheater.scanner import build_filesystem

    with pytest.raises(NotConfiguredError):
        build_filesystem(get_config())
