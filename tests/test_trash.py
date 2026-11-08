"""F1: the NAS trash. The adversarial cases matter most: nothing here may ever touch a
file outside the trash root, overwrite one, or lose one because a rename failed."""

from __future__ import annotations

import errno
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeTheater import trash as tr
from homeTheater.db.base import utcnow
from homeTheater.db.models import TrashItem
from homeTheater.db.session import session_scope
from homeTheater.errors import JobBusyError, NotConfiguredError
from homeTheater.trash import (
    TRASH_DIRNAME,
    TrashConflictError,
    TrashError,
    TrashPathError,
    TrashUnavailableError,
)


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    path = tmp_path / "trash.yaml"
    path.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 't.db'}'}}\n"
        "torrent:\n"
        f"  library_base_dir: {tmp_path / 'lib'}\n"
    )
    (tmp_path / "lib").mkdir()
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(path))
    _reset()
    init_db()
    yield get_config()
    _reset()


def _file(cfg: Any, rel: str = "Movies/X/x.mkv", data: bytes = b"movie") -> Path:
    path = Path(cfg.torrent.library_base_dir) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _root(cfg: Any) -> Path:
    return Path(cfg.torrent.library_base_dir) / TRASH_DIRNAME


def _item(item_id: int) -> TrashItem:
    with session_scope() as s:
        item = s.get(TrashItem, item_id)
        assert item is not None
        s.expunge(item)
        return item


def _age(item_id: int, days: float) -> None:
    with session_scope() as s:
        item = s.get(TrashItem, item_id)
        assert item is not None
        item.trashed_at = utcnow() - timedelta(days=days)


def _set(item_id: int, **fields: Any) -> None:
    with session_scope() as s:
        item = s.get(TrashItem, item_id)
        assert item is not None
        for key, value in fields.items():
            setattr(item, key, value)


def _rows() -> int:
    with session_scope() as s:
        return s.query(TrashItem).count()


# --- moving -------------------------------------------------------------------


def test_move_keeps_the_relative_path_under_a_dated_folder(cfg: Any) -> None:
    src = _file(cfg, "TV Shows/S/Season 01/extra.mkv")
    item = _item(tr.move_to_trash(cfg, str(src), reason="r"))

    today = utcnow().date().isoformat()
    assert not src.exists()
    assert (_root(cfg) / today / "TV Shows/S/Season 01/extra.mkv").read_bytes() == b"movie"
    assert item.state == tr.STATE_TRASHED
    assert item.original_path == "TV Shows/S/Season 01/extra.mkv"
    assert item.size_bytes == 5 and item.reason == "r"


def test_move_is_a_rename_never_a_copy(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _file(cfg)
    inode = src.stat().st_ino
    tr.move_to_trash(cfg, str(src), reason="r")
    assert next(p for p in _root(cfg).rglob("x.mkv")).stat().st_ino == inode


def test_same_name_twice_keeps_both(cfg: Any) -> None:
    first = tr.move_to_trash(cfg, str(_file(cfg, data=b"one")), reason="r")
    second = tr.move_to_trash(cfg, str(_file(cfg, data=b"two")), reason="r")
    a, b = _item(first).trash_path, _item(second).trash_path
    assert a != b
    assert (_root(cfg) / a).read_bytes() == b"one" and (_root(cfg) / b).read_bytes() == b"two"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "relative/file.mkv",
    ],
)
def test_paths_outside_the_library_are_refused(cfg: Any, path: str) -> None:
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, path, reason="r")
    assert _rows() == 0


def test_dotdot_in_a_path_is_refused_even_when_it_lands_inside(cfg: Any) -> None:
    src = _file(cfg)
    sneaky = str(src.parent / ".." / "X" / "x.mkv")
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, sneaky, reason="r")
    assert src.exists()


def test_dotdot_escape_from_the_library_is_refused(cfg: Any, tmp_path: Path) -> None:
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, f"{cfg.torrent.library_base_dir}/../outside.mkv", reason="r")
    assert outside.exists()


def test_library_lookalike_prefix_is_outside(cfg: Any, tmp_path: Path) -> None:
    sibling = tmp_path / "lib2"
    sibling.mkdir()
    (sibling / "a.mkv").write_bytes(b"x")
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, str(sibling / "a.mkv"), reason="r")


def test_a_symlinked_folder_out_of_the_library_is_refused(cfg: Any, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.mkv").write_bytes(b"x")
    (Path(cfg.torrent.library_base_dir) / "Movies").mkdir()
    (Path(cfg.torrent.library_base_dir) / "Movies" / "link").symlink_to(elsewhere)
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, f"{cfg.torrent.library_base_dir}/Movies/link/a.mkv", reason="r")
    assert (elsewhere / "a.mkv").exists()


def test_the_trash_cant_trash_itself(cfg: Any) -> None:
    src = _file(cfg, f"{TRASH_DIRNAME}/old/x.mkv")
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists()


def test_folders_are_refused(cfg: Any) -> None:
    folder = _file(cfg).parent
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, str(folder), reason="r")
    assert folder.is_dir()


def test_a_symlinked_trash_root_is_refused(cfg: Any, tmp_path: Path) -> None:
    src = _file(cfg)
    evil = tmp_path / "evil"
    evil.mkdir()
    _root(cfg).symlink_to(evil)
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and not list(evil.iterdir())


def test_a_symlink_planted_inside_the_trash_is_refused(cfg: Any, tmp_path: Path) -> None:
    src = _file(cfg)
    evil = tmp_path / "evil"
    evil.mkdir()
    day = _root(cfg) / utcnow().date().isoformat()
    day.mkdir(parents=True)
    (day / "Movies").symlink_to(evil)
    with pytest.raises(TrashPathError):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and not list(evil.iterdir()) and _rows() == 0


def test_no_library_mount_configured(cfg: Any) -> None:
    bare = cfg.model_copy(
        update={"torrent": cfg.torrent.model_copy(update={"library_base_dir": None})}
    )
    with pytest.raises(NotConfiguredError):
        tr.move_to_trash(bare, "/a/b.mkv", reason="r")


def test_rename_across_a_boundary_keeps_the_file_and_reports(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _file(cfg)

    def cross_device(_a: str, _b: str) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    removed: list[str] = []
    monkeypatch.setattr(tr.os, "rename", cross_device)
    monkeypatch.setattr(tr.os, "remove", removed.append)
    monkeypatch.setattr(tr.os, "unlink", removed.append)
    with pytest.raises(TrashError, match="other volume|another volume"):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and removed == [] and _rows() == 0


def test_a_trash_on_another_volume_is_refused_before_any_rename(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _file(cfg)
    monkeypatch.setattr(tr, "_same_volume", lambda _a, _b: False)
    with pytest.raises(TrashError, match="kept"):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and _rows() == 0


def _mounted_at(monkeypatch: pytest.MonkeyPatch, *states: bool) -> None:
    """Make the library look like a /Volumes mount with these answers to "mounted?"."""

    from homeTheater.acquisition.torrent import importer

    answers = list(states)

    def gone(_base: str | None) -> bool:
        return not (answers.pop(0) if len(answers) > 1 else answers[0])

    monkeypatch.setattr(importer, "_mount_gone", gone)
    monkeypatch.setattr(tr, "_mount_gone", gone)


def test_mount_dropped_mid_rename_keeps_the_file_and_never_reports_success(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _file(cfg)
    _mounted_at(monkeypatch, False)

    def vanished(_a: str, _b: str) -> None:
        raise FileNotFoundError(errno.ENOENT, "gone")

    monkeypatch.setattr(tr.os, "rename", vanished)
    with pytest.raises(TrashUnavailableError):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and _rows() == 0


@pytest.mark.parametrize("code", [errno.EIO, errno.ENOTCONN, errno.ESTALE])
def test_link_fault_during_the_rename_is_a_nas_problem(
    cfg: Any, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    src = _file(cfg)

    def fault(_a: str, _b: str) -> None:
        raise OSError(code, "fault")

    monkeypatch.setattr(tr.os, "rename", fault)
    with pytest.raises(TrashUnavailableError):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and _rows() == 0


def test_unmounted_library_is_refused_before_anything_happens(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition.torrent.importer import NasUnavailableError

    src = _file(cfg)

    def not_mounted(_base: str, _mount: Any) -> None:
        raise NasUnavailableError("not mounted")

    monkeypatch.setattr("homeTheater.acquisition.torrent.importer.ensure_mounted", not_mounted)
    with pytest.raises(TrashUnavailableError):
        tr.move_to_trash(cfg, str(src), reason="r")
    assert src.exists() and not _root(cfg).exists()


def test_crash_after_the_rename_is_settled_from_disk(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The share goes between the rename and its confirmation: the record stays
    pending, isn't listed as restorable yet, and the next purge sorts it out."""

    src = _file(cfg)
    real_lstat = os.lstat
    calls: list[str] = []

    def lstat_after_rename(path: Any, **kw: Any) -> Any:
        if TRASH_DIRNAME in str(path) and str(path).endswith("x.mkv"):
            calls.append(str(path))
            if os.path.exists(str(path)):
                raise OSError(errno.EIO, "gone")
        return real_lstat(path, **kw)

    monkeypatch.setattr(tr.os, "lstat", lstat_after_rename)
    with pytest.raises(TrashUnavailableError):
        tr.move_to_trash(cfg, str(src), reason="r")
    monkeypatch.undo()
    assert tr.list_trash(cfg) == []

    with session_scope() as s:
        item = s.query(TrashItem).one()
        assert item.state == tr.STATE_PENDING
        item_id = item.id
    tr.purge_expired(cfg)
    assert _item(item_id).state == tr.STATE_TRASHED
    assert len(tr.list_trash(cfg)) == 1


def test_a_pending_row_whose_rename_never_happened_is_dropped(cfg: Any) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    tr._restore(cfg, item_id)
    _set(item_id, state=tr.STATE_PENDING)
    tr.purge_expired(cfg)
    assert _rows() == 0 and src.exists()


def test_a_pending_row_is_never_purged(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _set(item_id, state=tr.STATE_PENDING, trashed_at=utcnow() - timedelta(days=99))
    tr.purge_expired(cfg)
    assert list(_root(cfg).rglob("x.mkv"))  # settled to trashed, kept until a later run


# --- restoring ----------------------------------------------------------------


def test_restore_puts_it_back(cfg: Any) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    assert tr.restore(cfg, item_id) == "Movies/X/x.mkv"
    assert src.read_bytes() == b"movie"
    assert _item(item_id).state == tr.STATE_RESTORED
    assert tr.list_trash(cfg) == []


def test_restore_recreates_missing_folders(cfg: Any) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    src.parent.rmdir()
    tr.restore(cfg, item_id)
    assert src.exists()


def test_restore_refuses_to_overwrite(cfg: Any) -> None:
    src = _file(cfg, data=b"old")
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    src.write_bytes(b"new")
    with pytest.raises(TrashConflictError):
        tr.restore(cfg, item_id)
    assert src.read_bytes() == b"new"
    assert _item(item_id).state == tr.STATE_TRASHED
    assert (_root(cfg) / _item(item_id).trash_path).read_bytes() == b"old"


def test_restore_refuses_a_dangling_symlink_in_the_way(cfg: Any, tmp_path: Path) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    src.symlink_to(tmp_path / "nowhere")
    with pytest.raises(TrashConflictError):
        tr.restore(cfg, item_id)
    assert src.is_symlink()


def test_restore_twice_refuses_the_second(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    tr.restore(cfg, item_id)
    with pytest.raises(TrashError):
        tr.restore(cfg, item_id)


def test_restore_of_an_unknown_id(cfg: Any) -> None:
    with pytest.raises(ValueError, match="not found"):
        tr.restore(cfg, 999)


@pytest.mark.parametrize(
    "original", ["../outside.mkv", "/etc/x.mkv", "a/../../outside.mkv", f"{TRASH_DIRNAME}/x.mkv"]
)
def test_restore_refuses_a_tampered_destination(cfg: Any, tmp_path: Path, original: str) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _set(item_id, original_path=original)
    with pytest.raises(TrashPathError):
        tr.restore(cfg, item_id)
    assert not (tmp_path / "outside.mkv").exists()
    assert list(_root(cfg).rglob("x.mkv"))


def test_restore_refuses_a_tampered_source(cfg: Any, tmp_path: Path) -> None:
    outside = tmp_path / "secret.mkv"
    outside.write_bytes(b"s")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _set(item_id, trash_path="../../secret.mkv")
    with pytest.raises(TrashPathError):
        tr.restore(cfg, item_id)
    assert outside.exists()


def test_restore_through_a_symlink_out_of_the_library_is_refused(cfg: Any, tmp_path: Path) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    src.parent.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    src.parent.symlink_to(elsewhere)
    with pytest.raises(TrashPathError):
        tr.restore(cfg, item_id)
    assert not list(elsewhere.iterdir())


def test_restore_with_the_mount_gone_changes_nothing(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _mounted_at(monkeypatch, False)

    def vanished(_a: str, _b: str) -> None:
        raise FileNotFoundError(errno.ENOENT, "gone")

    monkeypatch.setattr(tr.os, "rename", vanished)
    with pytest.raises(TrashUnavailableError):
        tr._restore(cfg, item_id)
    assert _item(item_id).state == tr.STATE_TRASHED


def test_restore_waits_for_an_import(cfg: Any) -> None:
    from homeTheater.locks import job_lock

    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    with job_lock(cfg, "sync"), pytest.raises(JobBusyError):
        tr.restore(cfg, item_id)


# --- purging ------------------------------------------------------------------


def test_purge_only_takes_what_is_past_retention(cfg: Any) -> None:
    old = tr.move_to_trash(cfg, str(_file(cfg, "a/old.mkv")), reason="r")
    new = tr.move_to_trash(cfg, str(_file(cfg, "a/new.mkv")), reason="r")
    _age(old, 31)
    _age(new, 29)

    stats = tr.purge_expired(cfg)

    assert (stats.expired, stats.purged, stats.purged_bytes) == (1, 1, 5)
    assert not (_root(cfg) / _item(old).trash_path).exists()
    assert _item(old).state == tr.STATE_PURGED
    assert (_root(cfg) / _item(new).trash_path).exists()


def test_retention_comes_from_config(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 3)
    short = cfg.model_copy(update={"trash": cfg.trash.model_copy(update={"retention_days": 2})})
    assert tr.purge_expired(cfg).purged == 0
    assert tr.purge_expired(short).purged == 1


def test_purge_dry_run_touches_nothing(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 40)
    stats = tr.purge(cfg, dry_run=True)
    assert stats.expired == 1 and stats.purged == 0
    assert (_root(cfg) / _item(item_id).trash_path).exists()


def test_purge_removes_emptied_folders_but_never_the_root(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 40)
    tr.purge_expired(cfg)
    assert _root(cfg).is_dir() and list(_root(cfg).iterdir()) == []


def test_purge_leaves_a_folder_holding_something_else(cfg: Any) -> None:
    a = tr.move_to_trash(cfg, str(_file(cfg, "M/a.mkv")), reason="r")
    tr.move_to_trash(cfg, str(_file(cfg, "M/b.mkv")), reason="r")
    _age(a, 40)
    tr.purge_expired(cfg)
    assert list(_root(cfg).rglob("b.mkv"))


def test_purge_unlinks_a_symlink_and_leaves_its_target(cfg: Any) -> None:
    target = _file(cfg, "Movies/Keep/keep.mkv", b"precious")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    trashed = _root(cfg) / _item(item_id).trash_path
    trashed.unlink()
    trashed.symlink_to(target)
    _age(item_id, 40)

    tr.purge_expired(cfg)

    assert not trashed.is_symlink() and target.read_bytes() == b"precious"


@pytest.mark.parametrize(
    "stored",
    ["../Movies/Keep/keep.mkv", "/etc/passwd", "a/../../Movies/Keep/keep.mkv", "", "."],
)
def test_purge_refuses_a_tampered_path(cfg: Any, stored: str) -> None:
    target = _file(cfg, "Movies/Keep/keep.mkv", b"precious")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _set(item_id, trash_path=stored)
    _age(item_id, 40)

    stats = tr.purge_expired(cfg)

    assert stats.purged == 0 and stats.errors
    assert target.read_bytes() == b"precious"
    assert _item(item_id).state == tr.STATE_TRASHED


def test_purge_refuses_a_stored_absolute_path_to_a_real_file(cfg: Any, tmp_path: Path) -> None:
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _set(item_id, trash_path=str(outside))
    _age(item_id, 40)
    tr.purge_expired(cfg)
    assert outside.exists()


def test_purge_refuses_a_path_through_a_symlinked_folder(cfg: Any) -> None:
    target = _file(cfg, "Movies/Keep/keep.mkv", b"precious")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _root(cfg).joinpath("link").symlink_to(target.parent)
    _set(item_id, trash_path="link/keep.mkv")
    _age(item_id, 40)

    stats = tr.purge_expired(cfg)

    assert stats.errors and target.read_bytes() == b"precious"


def test_purge_refuses_when_the_root_became_a_symlink(cfg: Any) -> None:
    target = _file(cfg, "Movies/Keep/keep.mkv", b"precious")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _set(item_id, trash_path="keep.mkv")
    _age(item_id, 40)
    real_root = _root(cfg)
    real_root.rename(real_root.with_name("moved"))
    real_root.symlink_to(target.parent)

    with pytest.raises(TrashPathError):
        tr.purge_expired(cfg)
    assert target.read_bytes() == b"precious"


def test_purge_never_empties_a_folder(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    trashed = _root(cfg) / _item(item_id).trash_path
    trashed.unlink()
    trashed.mkdir()
    (trashed / "inner.mkv").write_bytes(b"x")
    _age(item_id, 40)

    stats = tr.purge_expired(cfg)

    assert stats.errors and (trashed / "inner.mkv").exists()


def test_purge_ignores_files_it_has_no_record_of(cfg: Any) -> None:
    stray = _root(cfg) / "2020-01-01" / "stray.mkv"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"x")
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 40)
    tr.purge_expired(cfg)
    assert stray.exists()


def test_purge_with_the_mount_dropping_keeps_records(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tr.move_to_trash(cfg, str(_file(cfg, "a/1.mkv")), reason="r")
    second = tr.move_to_trash(cfg, str(_file(cfg, "a/2.mkv")), reason="r")
    _age(first, 40)
    _age(second, 40)
    _mounted_at(monkeypatch, True, False)  # up for the first file, gone for the second

    stats = tr.purge_expired(cfg)

    assert any("dropped" in e for e in stats.errors) and stats.purged == 1
    # Not recorded as gone just because it couldn't be seen.
    assert sorted((_item(first).state, _item(second).state)) == [
        tr.STATE_PURGED,
        tr.STATE_TRASHED,
    ]


def test_a_missing_file_is_recorded_as_gone_only_with_the_mount_up(cfg: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    (_root(cfg) / _item(item_id).trash_path).unlink()
    _age(item_id, 40)
    stats = tr.purge_expired(cfg)
    assert stats.already_gone == 1 and _item(item_id).state == tr.STATE_PURGED


def test_purge_with_nothing_expired_doesnt_touch_the_nas(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_base: str) -> None:
        raise AssertionError("a purge with nothing to do must not touch the share")

    monkeypatch.setattr(tr, "_require_mounted", explode)
    tr.purge_expired(cfg)


def test_cancel_stops_the_purge_between_files(cfg: Any) -> None:
    ids = [tr.move_to_trash(cfg, str(_file(cfg, f"a/{n}.mkv")), reason="r") for n in range(2)]
    for item_id in ids:
        _age(item_id, 40)
    cancel = threading.Event()
    cancel.set()
    assert tr.purge_expired(cfg, cancel=cancel).purged == 0


def test_purge_errors_are_redacted(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 40)

    def leaky(_path: str) -> None:
        raise OSError(errno.EACCES, "denied https://x.test/?api_key=SECRET123")

    monkeypatch.setattr(tr.os, "unlink", leaky)
    stats = tr.purge_expired(cfg)
    assert stats.errors and "SECRET123" not in " ".join(stats.errors)


# --- invisibility -------------------------------------------------------------


def test_the_scanner_skips_the_trash(cfg: Any) -> None:
    from homeTheater.scanner.filesystem import LocalFileSystem, is_ignored_dir

    assert is_ignored_dir(TRASH_DIRNAME)
    kept = _file(cfg, "Movies/Kept (2000)/Kept (2000).mkv")
    tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    fs = LocalFileSystem(cfg.torrent.library_base_dir)
    assert [e.path for e in fs.walk("")] == [str(kept)]


def test_a_scan_of_the_share_root_catalogs_no_trash(cfg: Any) -> None:
    from homeTheater.db.models import OwnedFile, TitleKind
    from homeTheater.scanner import LocalFileSystem, scan_library

    _file(cfg, "Movies/Kept (2000)/Kept (2000).mkv")
    tr.move_to_trash(cfg, str(_file(cfg, "Movies/Gone (2001)/Gone (2001).mkv")), reason="r")
    fs = LocalFileSystem(base_dir=cfg.torrent.library_base_dir)
    scan_library(fs, {TitleKind.movie: ""})
    with session_scope() as s:
        paths = [o.path for o in s.query(OwnedFile)]
    assert paths and not any(TRASH_DIRNAME in p for p in paths)


def test_the_extras_finder_skips_the_trash(cfg: Any) -> None:
    from homeTheater.cleanup import find_nas_extras

    extra = _file(cfg, "Show/Season 01/Featurettes/Tour.mkv")
    tr.move_to_trash(cfg, str(_file(cfg, "Show/Season 01/Samples/Old.mkv")), reason="r")
    nas = cfg.nas.model_copy(update={"tv_root": "."})
    extras, _notes = find_nas_extras(cfg.model_copy(update={"nas": nas}))
    assert [os.path.normpath(e.path) for e in extras] == [str(extra)]


# --- cleanup ------------------------------------------------------------------


def test_cleanup_sends_extras_to_the_trash(cfg: Any) -> None:
    from homeTheater.cleanup import delete_nas_extras

    extra = _file(cfg, "TV Shows/S/Season 01/Featurettes/Tour.mkv")
    assert delete_nas_extras(cfg, [str(extra)]) == (1, [])
    assert not extra.exists()
    assert [e.original_path for e in tr.list_trash(cfg)] == [
        "TV Shows/S/Season 01/Featurettes/Tour.mkv"
    ]


def test_cleanup_keeps_a_file_the_trash_cant_take(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import cleanup

    extra = _file(cfg, "TV Shows/S/Featurettes/Tour.mkv")

    def fail(_a: str, _b: str) -> None:
        raise OSError(errno.EXDEV, "Cross-device link")

    removed: list[str] = []
    monkeypatch.setattr(tr.os, "rename", fail)
    monkeypatch.setattr(cleanup.os, "remove", removed.append)
    deleted, errors = cleanup.delete_nas_extras(cfg, [str(extra)])
    assert deleted == 0 and errors and extra.exists() and removed == []


def test_cleanup_doesnt_count_a_dropped_mount_as_already_gone(
    cfg: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater import cleanup

    extra = _file(cfg, "TV Shows/S/Featurettes/Tour.mkv")
    _mounted_at(monkeypatch, False)

    def vanished(_a: str, _b: str) -> None:
        raise FileNotFoundError(errno.ENOENT, "gone")

    monkeypatch.setattr(tr.os, "rename", vanished)
    deleted, errors = cleanup.delete_nas_extras(cfg, [str(extra)])
    assert deleted == 0 and errors


def test_cleanup_really_gone_still_counts(cfg: Any) -> None:
    from homeTheater.cleanup import delete_nas_extras

    (Path(cfg.torrent.library_base_dir) / "Show").mkdir()
    path = f"{cfg.torrent.library_base_dir}/Show/never.mkv"
    assert delete_nas_extras(cfg, [path]) == (1, [])


def test_trash_off_deletes_outright(cfg: Any) -> None:
    from homeTheater.cleanup import delete_nas_extras

    off = cfg.model_copy(update={"trash": cfg.trash.model_copy(update={"enabled": False})})
    extra = _file(cfg, "TV Shows/S/Featurettes/Tour.mkv")
    assert delete_nas_extras(off, [str(extra)]) == (1, [])
    assert not extra.exists() and not _root(cfg).exists()


# --- API, job, CLI ------------------------------------------------------------


@pytest.fixture
def client(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    _reset()
    from homeTheater.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


AUTH = {"X-Auth-Token": "test-token"}


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/trash"), ("post", "/api/trash/1/restore"), ("post", "/api/trash/purge")],
)
def test_api_is_gated(client: Any, method: str, path: str) -> None:
    assert getattr(client, method)(path).status_code in (401, 403)


def test_api_lists_and_restores(cfg: Any, client: Any) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="cleanup: NAS extra")

    listing = client.get("/api/trash", headers=AUTH).json()
    assert [i["id"] for i in listing["items"]] == [item_id]
    assert listing["retention_days"] == 30

    r = client.post(f"/api/trash/{item_id}/restore", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"restored": "Movies/X/x.mkv"}
    assert src.exists()


def test_api_restore_conflict_is_409(cfg: Any, client: Any) -> None:
    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")
    src.write_bytes(b"new")
    assert client.post(f"/api/trash/{item_id}/restore", headers=AUTH).status_code == 409


def test_api_restore_unknown_is_404(client: Any) -> None:
    assert client.post("/api/trash/999/restore", headers=AUTH).status_code == 404


def test_api_purge(cfg: Any, client: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 40)
    assert client.post("/api/trash/purge", headers=AUTH).json()["purged"] == 1


def test_status_page_lists_the_trash_with_restore(cfg: Any, client: Any) -> None:
    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="cleanup: NAS extra")
    html = client.get("/status").text
    assert "Movies/X/x.mkv" in html and "cleanup: NAS extra" in html
    assert f'data-action="trash-restore" data-id="{item_id}"' in html


async def test_purge_is_a_scheduled_job(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.config.settings import Schedule
    from homeTheater.scheduler import jobs
    from homeTheater.scheduler.catchup import job_intervals
    from homeTheater.scheduler.scheduler import _JOBS

    assert job_intervals(Schedule())["trash"] == 1440
    assert _JOBS["trash"] is jobs.run_trash_job
    assert jobs.JOB_RESOURCES["trash"] == jobs.JOB_RESOURCES["sync"]

    item_id = tr.move_to_trash(cfg, str(_file(cfg)), reason="r")
    _age(item_id, 40)
    await jobs.run_trash_job(cfg)
    assert _item(item_id).state == tr.STATE_PURGED


def test_cli(cfg: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import sys

    from homeTheater import main as cli

    src = _file(cfg)
    item_id = tr.move_to_trash(cfg, str(src), reason="r")

    monkeypatch.setattr(sys, "argv", ["home-theater", "trash", "list"])
    cli.main()
    assert "Movies/X/x.mkv" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["home-theater", "trash", "purge", "--dry-run"])
    cli.main()
    assert "0 item(s)" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["home-theater", "trash", "purge"])
    cli.main()
    assert "Purged 0" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["home-theater", "trash", "restore", str(item_id)])
    cli.main()
    assert src.exists()

    monkeypatch.setattr(sys, "argv", ["home-theater", "trash", "restore", "999"])
    with pytest.raises(SystemExit):
        cli.main()


def test_config_defaults_and_typos(cfg: Any) -> None:
    from pydantic import ValidationError

    from homeTheater.config.settings import Trash

    assert (Trash().enabled, Trash().retention_days) == (True, 30)
    with pytest.raises(ValidationError):
        Trash(retention_days=0)  # purge-on-sight isn't a retention period
    with pytest.raises(ValidationError):
        Trash(retentiondays=5)  # type: ignore[call-arg]


def test_the_restore_button_has_a_handler() -> None:
    import homeTheater

    script = Path(homeTheater.__file__).parent / "web" / "static" / "app.js"
    source = script.read_text(encoding="utf-8")
    assert 'act === "trash-restore"' in source and "/api/trash/${btn.dataset.id}/restore" in source
