"""Library junk: what counts, what never does, and what removal leaves behind."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

PLACEHOLDER = (
    b"1\n00:00:00,001 --> 04:00:00,002\n"
    b"Become OpenSubtitles.org VIP member \nto get subtitles -> osdb.link/vip\n"
)
REAL_SRT = b"".join(
    f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},900\nline {i}\n\n".encode() for i in range(1, 50)
)
OLD = time.time() - 30 * 86400


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str = "") -> Path:
    lib = tmp_path / "lib"
    cfg = tmp_path / "junk.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'j.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: []\n"
        f"  library_base_dir: {lib}\n" + extra
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    _reset()
    (lib / "Movies").mkdir(parents=True)
    (lib / "TV Shows").mkdir()
    return lib


def _put(path: Path, data: bytes = b"x", *, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def _config():  # type: ignore[no-untyped-def]
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    init_db()
    return get_config()


def _reasons(plan, lib: Path) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {os.path.relpath(f.path, lib): f.reason for f in plan.files}


def test_finds_junk_and_keeps_what_the_library_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import junk

    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "Movies" / "Heat (1995)"
    keep = [
        _put(movie / "Heat (1995).mkv", b"v" * 400_000),
        _put(movie / "Subs" / "Heat (1995).en.srt", REAL_SRT),
        _put(movie / "Heat (1995).nfo", b"<movie><title>Heat</title></movie>"),
        _put(movie / "poster.jpg"),
        _put(movie / "Heat (1995)-fanart.jpg"),
        _put(movie / "Heat (1995).idx"),
        _put(movie / "Heat (1995).mkv.part", mtime=time.time() - 3600),  # still downloading
        _put(lib / "Movies" / "Sample Size (2020)" / "Sample Size (2020).mkv", b"v" * 1000),
    ]
    season = lib / "TV Shows" / "Silo" / "Season 01"
    keep.append(_put(season / "Silo S01E01.mkv", b"v" * 5000))
    expected = {
        _put(movie / "Subs" / "Heat (1995).he.srt", PLACEHOLDER): junk.REASON_PLACEHOLDER,
        _put(movie / "RARBG.txt"): junk.REASON_RELEASE_NOTE,
        _put(movie / "heat.nfo", b"\xdb\xdb release by GROUP \xdb\xdb"): junk.REASON_RELEASE_NOTE,
        _put(movie / "Screens" / "screen0001.png"): junk.REASON_IMAGE,
        _put(movie / "Heat (1995).sample.mkv", b"s" * 100): junk.REASON_SAMPLE,
        _put(movie / "Subs" / "Heat (1995).sample.en.srt", REAL_SRT): junk.REASON_SAMPLE_SUBTITLE,
        _put(movie / "ETRG.mp4", b"p" * 10): junk.REASON_SAMPLE,
        _put(movie / ".DS_Store"): junk.REASON_OS,
        _put(season / ".pad" / "12345"): junk.REASON_PADDING,
        _put(season / "Silo S01E02.mkv.part", b"p" * 99): junk.REASON_PARTIAL,
        _put(lib / "Movies" / "Gone (2001)" / "Subs" / "Gone.en.srt", REAL_SRT): junk.REASON_ORPHAN,
    }
    archive = _put(season / "subs" / "Silo.S01E01.he.zip", b"PK")
    odd = _put(season / "Silo S01E01.srt 2", REAL_SRT)
    (lib / "TV Shows" / "Silo" / "Featurettes").mkdir()

    plan = junk.find_junk(_config())

    assert _reasons(plan, lib) == {os.path.relpath(p, lib): r for p, r in expected.items()}
    assert plan.unrecognised == [str(odd)]
    assert plan.archives_kept == 1 and archive.exists()
    assert plan.empty_dirs == [str(lib / "TV Shows" / "Silo" / "Featurettes")]
    assert all(p.exists() for p in keep)


def test_recent_files_and_app_owned_partials_are_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import junk

    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "Movies" / "Heat (1995)"
    _put(movie / "Heat (1995).mkv", b"v" * 1000)
    _put(movie / "RARBG.txt", mtime=time.time())  # the import may still be running
    _put(movie / "Heat (1995).mkv.part", b"p")
    _put(movie / "Heat (1995).mkv.part.meta", b"{}")  # the importer's own, to be resumed
    new = lib / "Movies" / "New (2026)"
    _put(new / "Subs" / "New (2026).en.srt", REAL_SRT, mtime=time.time())  # video not here yet

    assert junk.find_junk(_config()).files == []


def test_archives_are_junk_only_when_asked_and_only_small_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import junk

    lib = _write_config(tmp_path, monkeypatch, "junk: {remove_archives: true, archive_max_mb: 1}\n")
    season = lib / "TV Shows" / "Silo" / "Season 01"
    _put(season / "Silo S01E01.mkv", b"v" * 100)
    small = _put(season / "subs" / "a.zip", b"PK")
    big = _put(season / "release.rar", b"R" * (2 * 1024 * 1024))

    plan = junk.find_junk(_config())

    assert _reasons(plan, lib) == {os.path.relpath(small, lib): junk.REASON_ARCHIVE}
    assert plan.unrecognised == [str(big)]


def test_remove_trashes_files_forgets_placeholders_and_prunes_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import junk
    from homeTheater.db import session_scope
    from homeTheater.db.models import OwnedFile, Subtitle, Title, TitleKind, TrashItem

    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "Movies" / "Heat (1995)"
    video = _put(movie / "Heat (1995).mkv", b"v" * 1000)
    fake = _put(movie / "Subs" / "Heat (1995).he.srt", PLACEHOLDER)
    shot = _put(movie / "Screens" / "screen0001.png")
    ds = _put(movie / ".DS_Store")
    config = _config()
    with session_scope() as s:
        title = Title(kind=TitleKind.movie, title="Heat", year=1995)
        s.add(title)
        s.flush()
        owned = OwnedFile(
            path=str(video), title_id=title.id, kind=TitleKind.movie, subtitle_langs=["en", "he"]
        )
        s.add(owned)
        s.flush()
        s.add(
            Subtitle(
                title_id=title.id,
                owned_file_id=owned.id,
                lang="he",
                provider="opensubtitles_org",
                path=str(fake),
                status="downloaded",
            )
        )
        owned_id = owned.id

    result = asyncio.run(junk.apply_junk(config))

    assert result["junk_removed"] == 3 and result["errors"] == []
    assert not fake.exists() and not shot.exists() and not ds.exists()
    assert video.exists()
    assert not (movie / "Screens").exists() and not (movie / "Subs").exists()
    assert result["dirs_removed"] == 2
    with session_scope() as s:
        assert s.get(OwnedFile, owned_id).subtitle_langs == ["en"]
        assert s.query(Subtitle).count() == 0
        trashed = {t.original_path: t.reason for t in s.query(TrashItem)}
    assert trashed == {
        "Movies/Heat (1995)/Subs/Heat (1995).he.srt": f"junk: {junk.REASON_PLACEHOLDER}",
        "Movies/Heat (1995)/Screens/screen0001.png": f"junk: {junk.REASON_IMAGE}",
    }  # OS metadata is deleted, not trashed
    assert junk.find_junk(config).files == []


def test_limit_refuses_a_plan_that_is_too_large(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import junk

    lib = _write_config(tmp_path, monkeypatch)
    movie = lib / "Movies" / "Heat (1995)"
    _put(movie / "Heat (1995).mkv", b"v" * 1000)
    notes = [_put(movie / f"note{i}.txt") for i in range(3)]

    result = asyncio.run(junk.apply_junk(_config(), limit=2))

    assert result["refused"] == 3 and result["junk_removed"] == 0
    assert all(p.exists() for p in notes)


async def test_scheduled_job_is_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    lib = _write_config(tmp_path, monkeypatch)
    note = _put(lib / "Movies" / "Heat (1995)" / "RARBG.txt")
    _put(lib / "Movies" / "Heat (1995)" / "Heat (1995).mkv", b"v" * 1000)

    await jobs.run_junk_job(_config())

    assert note.exists()
    assert "junk.enabled" in jobs.skip_reasons["junk"]


async def test_scheduled_job_clears_junk_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    lib = _write_config(tmp_path, monkeypatch, "junk: {enabled: true}\n")
    note = _put(lib / "Movies" / "Heat (1995)" / "RARBG.txt")
    video = _put(lib / "Movies" / "Heat (1995)" / "Heat (1995).mkv", b"v" * 1000)

    await jobs.run_junk_job(_config())

    assert not note.exists() and video.exists()
