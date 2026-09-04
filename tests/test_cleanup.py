"""Cleanup: NAS-extras discovery/deletion and stuck-torrent classification."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

TRANSMISSION = "http://transmission.test/transmission/rpc"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = tmp_path / "lib"
    cfg = tmp_path / "cleanup.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'c.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        f"  library_base_dir: {lib}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)
    _reset()
    return lib


def test_find_and_delete_nas_extras(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = _write_config(tmp_path, monkeypatch)
    season = lib / "TV Shows" / "Ted Lasso" / "Season 01"
    season.mkdir(parents=True)
    ep = season / "Ted Lasso (2020) - S01E01 - Pilot.mkv"
    ep.write_bytes(b"e" * 100)
    extra1 = season / "Season 1 - The Cast Ask Each Other Anything - IMDB.mkv"
    extra1.write_bytes(b"x" * 50)
    extra2 = season / "Jose Mourinho Sketch - Apple TV.mkv"
    extra2.write_bytes(b"y" * 30)

    from homeTheater.cleanup import delete_nas_extras, find_nas_extras
    from homeTheater.config import get_config

    extras, notes = find_nas_extras(get_config())
    paths = {e.path for e in extras}
    assert paths == {str(extra1), str(extra2)}  # the episode is kept
    assert notes == []

    deleted, errors = delete_nas_extras(list(paths))
    assert deleted == 2 and errors == []
    assert ep.exists() and not extra1.exists() and not extra2.exists()


def test_find_nas_extras_notes_when_no_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'c.db'}'}}\n"
        "acquisition: {backend: torrent}\n"
        "torrent: {enabled_sources: [piratebay]}\n"  # no library_base_dir
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    _reset()

    from homeTheater.cleanup import find_nas_extras
    from homeTheater.config import get_config

    extras, notes = find_nas_extras(get_config())
    assert extras == [] and notes and "not set" in notes[0]


def _transmission_list(torrents: list[dict]) -> None:
    session = {"done": False}

    def respond(request: httpx.Request) -> httpx.Response:
        if not session["done"]:
            session["done"] = True
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "s"})
        return httpx.Response(200, json={"result": "success", "arguments": {"torrents": torrents}})

    respx.post(TRANSMISSION).mock(side_effect=respond)


def _t(h: str, name: str, done: bool) -> dict:
    return {
        "hashString": h,
        "name": name,
        "percentDone": 1.0 if done else 0.4,
        "status": 6 if done else 4,
        "downloadDir": "/d",
        "error": 0,
        "errorString": "",
    }


@respx.mock
async def test_find_stuck_torrents_classifies_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only completed torrents with no pending download are 'stuck'; a pending
    import and an incomplete download are left alone."""
    _write_config(tmp_path, monkeypatch)
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        CandidateStatus,
        Download,
        Title,
        TitleKind,
    )

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1, title="X", kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(
            title_id=t.id, source=CandidateSource.discovery, status=CandidateStatus.imported
        )
        s.add(c)
        s.flush()
        # imported (safe to remove), pending-import (keep), incomplete (keep)
        s.add(Download(candidate_id=c.id, external_id="a" * 40, state="imported", release="A"))
        s.add(Download(candidate_id=c.id, external_id="b" * 40, state="completed", release="B"))

    _transmission_list(
        [
            _t("a" * 40, "Imported Movie", True),  # imported -> stuck (safe)
            _t("b" * 40, "Pending Import", True),  # completed/pending -> keep
            _t("c" * 40, "Orphan Complete", True),  # no download row -> stuck (orphan)
            _t("d" * 40, "Still Downloading", False),  # incomplete -> keep
        ]
    )

    from homeTheater.cleanup import find_stuck_torrents
    from homeTheater.config import get_config

    stuck, notes = await find_stuck_torrents(get_config())
    by_hash = {t.infohash: t.reason for t in stuck}
    assert set(by_hash) == {"a" * 40, "c" * 40}
    assert by_hash["a" * 40] == "already imported"
    assert by_hash["c" * 40].startswith("orphaned")
    assert notes == []


@respx.mock
async def test_find_stuck_torrents_notes_when_client_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, monkeypatch)
    from homeTheater.db import init_db

    init_db()
    respx.post(TRANSMISSION).mock(side_effect=httpx.ConnectError("refused"))

    from homeTheater.cleanup import find_stuck_torrents
    from homeTheater.config import get_config

    stuck, notes = await find_stuck_torrents(get_config())
    assert stuck == [] and notes and "unreachable" in notes[0]
