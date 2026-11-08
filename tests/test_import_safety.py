"""Data-safety rules of the library import: nothing deletes the local copy, and
nothing overwrites the library, unless the NAS copy is proven good. Local targets
over tmp dirs, Transmission mocked with respx; nothing touches a real NAS."""

from __future__ import annotations

import errno
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from homeTheater.acquisition.torrent import importer
from homeTheater.acquisition.torrent.importer import (
    ImportError_,
    LibraryConflictError,
    LocalLibraryTarget,
    NasUnavailableError,
    SMBLibraryTarget,
    find_unimported_videos,
)
from homeTheater.db.models import TitleKind

TRANSMISSION = "http://transmission.test/transmission/rpc"
HASH = "a" * 40
MOVIE_DIR = "Movies/M"


def _content(size: int, seed: int = 0) -> bytes:
    return bytes((i * 7 + seed + i // 251) % 256 for i in range(size))


def _src(tmp_path: Path, data: bytes, name: str = "src.mkv") -> Path:
    src = tmp_path / name
    src.write_bytes(data)
    return src


def _dest_dir(tmp_path: Path) -> Path:
    path = tmp_path / "lib" / "Movies" / "M"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _target(tmp_path: Path) -> LocalLibraryTarget:
    return LocalLibraryTarget(str(tmp_path / "lib"))


def _stamp_part(part: Path, src: Path, infohash: str | None = None) -> None:
    importer._write_sidecar(str(part), importer._source_stamp(src.stat(), infohash))


# --- A1: .part provenance ---------------------------------------------------


def _foreign_part(data: bytes, length: int) -> bytes:
    """Another release's leftover whose last bytes happen to agree with ``data``, so
    only the sidecar can reject it (the seam check alone would let it through)."""

    return _content(length // 2, seed=99) + data[length // 2 : length]


def test_a1_part_without_sidecar_restarts_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced bug: release A's head + release B's tail passed the size check."""
    monkeypatch.setattr(importer, "_VERIFY_CHUNK", 64)
    data = _content(10_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(_foreign_part(data, 6_000))

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert Path(out).read_bytes() == data


def test_a1_full_size_part_without_sidecar_is_not_trusted(tmp_path: Path) -> None:
    data = _content(5_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(_content(5_000, seed=42))

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert Path(out).read_bytes() == data


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda m: m.update(size=m["size"] + 1), id="size"),
        pytest.param(lambda m: m.update(mtime_ns=m["mtime_ns"] + 1), id="mtime"),
        pytest.param(lambda m: m.update(infohash="b" * 40), id="infohash"),
    ],
)
def test_a1_mismatched_sidecar_restarts_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: Callable[[dict[str, Any]], None]
) -> None:
    monkeypatch.setattr(importer, "_VERIFY_CHUNK", 64)
    data = _content(10_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(_foreign_part(data, 6_000))
    meta = importer._source_stamp(src.stat(), HASH)
    change(meta)
    Path(importer._sidecar_path(str(part))).write_text(json.dumps(meta))

    seen: list[int] = []
    out = _target(tmp_path).import_file(
        str(src), MOVIE_DIR, "M.mkv", lambda c, _t: seen.append(c), infohash=HASH
    )

    assert Path(out).read_bytes() == data
    assert seen[0] > 0 and seen[0] != 6_000  # did not start from the leftover


def test_a1_source_rewritten_after_the_stamp_restarts_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importer, "_VERIFY_CHUNK", 64)
    data = _content(10_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(_foreign_part(data, 6_000))
    _stamp_part(part, src)
    os.utime(src, ns=(1_000, 1_000))

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert Path(out).read_bytes() == data


def test_a1_corrupt_sidecar_restarts_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importer, "_VERIFY_CHUNK", 64)
    data = _content(10_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(_foreign_part(data, 6_000))
    Path(importer._sidecar_path(str(part))).write_text("{not json")

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert Path(out).read_bytes() == data


def test_a1_torn_seam_in_a_stamped_part_restarts_from_zero(tmp_path: Path) -> None:
    """A fault can leave the last bytes of a same-source .part unwritten."""
    data = _content(10_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(data[:5_990] + b"\0" * 10)
    _stamp_part(part, src, HASH)

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv", infohash=HASH)

    assert Path(out).read_bytes() == data


def test_a1_same_source_resumes_with_infohash(tmp_path: Path) -> None:
    data = _content(10_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(data[:6_000])
    _stamp_part(part, src, HASH.upper())

    seen: list[int] = []
    out = _target(tmp_path).import_file(
        str(src), MOVIE_DIR, "M.mkv", lambda c, _t: seen.append(c), infohash=HASH
    )

    assert Path(out).read_bytes() == data
    assert seen[0] == 6_000


def test_a1_sidecar_written_for_retry_and_removed_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A faulted copy leaves a stamped .part, the retry resumes it, success cleans up."""
    data = _content(importer._COPY_CHUNK + 5_000)
    src = _src(tmp_path, data)
    real_write = importer._copy_stream
    calls = {"n": 0}

    def fault_after_one_chunk(fsrc: Any, fdst: Any, *_a: Any, **_k: Any) -> int:
        calls["n"] += 1
        fdst.write(fsrc.read(importer._COPY_CHUNK))
        fdst.flush()
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(importer, "_copy_stream", fault_after_one_chunk)
    with pytest.raises(NasUnavailableError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv", infohash=HASH)
    monkeypatch.setattr(importer, "_copy_stream", real_write)

    part = _dest_dir(tmp_path) / "M.mkv.part"
    meta = Path(importer._sidecar_path(str(part)))
    assert part.stat().st_size == importer._COPY_CHUNK
    assert json.loads(meta.read_text())["infohash"] == HASH

    seen: list[int] = []
    out = _target(tmp_path).import_file(
        str(src), MOVIE_DIR, "M.mkv", lambda c, _t: seen.append(c), infohash=HASH
    )

    assert seen[0] == importer._COPY_CHUNK
    assert Path(out).read_bytes() == data
    assert not part.exists() and not meta.exists()


def test_a1_import_passes_infohash_to_target(tmp_path: Path) -> None:
    """The movie and episode entry points both tie the copy to the torrent."""
    seen: list[str | None] = []

    class Recording:
        def import_file(
            self,
            _src: str,
            _d: str,
            _f: str,
            _p: Any = None,
            *,
            infohash: Any = None,
            cancel: Any = None,
        ):
            seen.append(infohash)
            return "/lib/x.mkv"

        def list_dir(self, _rel: str) -> list[str]:
            return []

        def ready(self) -> None:
            pass

    src = _src(tmp_path, b"v" * 100, "Show.S01E01.mkv")
    config = SimpleNamespace(nas=SimpleNamespace(movies_root="Movies", tv_root="TV Shows"))
    importer.import_completed_movie(
        config, Recording(), content_path=str(src), title="M", year=None, infohash=HASH
    )
    importer.import_completed_episodes(
        config, Recording(), content_path=str(src), series_title="S", season=1, infohash=HASH
    )
    assert seen == [HASH, HASH]


# --- A2: read-back verification ---------------------------------------------


def test_a2_sample_ranges_stay_cheap_and_cover_head_and_tail() -> None:
    size = 40 * 1024**3
    ranges = importer._sample_ranges(size)

    assert ranges[0][0] == 0
    assert ranges[-1][0] + ranges[-1][1] == size
    assert len(ranges) == importer._VERIFY_MIDDLE_CHUNKS + 2
    assert sum(length for _o, length in ranges) <= 8 * 1024**2


def test_a2_small_file_is_compared_whole() -> None:
    assert importer._sample_ranges(1000) == [(0, 1000)]


def test_a2_copy_is_fsynced_before_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tmp_path, _content(1000))
    fsynced: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsynced.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(importer.os, "fsync", recording_fsync)

    _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert len(fsynced) >= 2  # the sidecar and the .part


def test_a2_unsupported_fsync_does_not_block_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))

    def unsupported(_fd: int) -> None:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(importer.os, "fsync", unsupported)

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert Path(out).exists()


def test_a2_failed_fsync_keeps_part_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))

    def eio(_fd: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(importer.os, "fsync", eio)

    with pytest.raises(NasUnavailableError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert not (_dest_dir(tmp_path) / "M.mkv").exists()


def test_a2_corrupted_copy_never_lands_and_restarts_next_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes that didn't reach the NAS are caught by the read-back, even in the middle
    of a large file where a size check sees nothing."""
    monkeypatch.setattr(importer, "_VERIFY_CHUNK", 64)
    data = _content(100_000)
    src = _src(tmp_path, data)
    real_copy = importer._copy_stream

    def corrupting(fsrc: Any, fdst: Any, *a: Any, **k: Any) -> int:
        written = real_copy(fsrc, fdst, *a, **k)
        fdst.flush()
        fdst.seek(50_000)
        fdst.write(b"\xff" * 64)
        return written

    monkeypatch.setattr(importer, "_copy_stream", corrupting)
    with pytest.raises(ImportError_, match="read-back"):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    dest_dir = _dest_dir(tmp_path)
    assert not (dest_dir / "M.mkv").exists()
    assert not (dest_dir / "M.mkv.part").exists()

    monkeypatch.setattr(importer, "_copy_stream", real_copy)
    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert Path(out).read_bytes() == data


def test_a2_corrupted_full_size_part_is_recopied_not_renamed(tmp_path: Path) -> None:
    """The ``have == src_size`` shortcut is verified too, sidecar or not."""
    data = _content(5_000)
    src = _src(tmp_path, data)
    part = _dest_dir(tmp_path) / "M.mkv.part"
    part.write_bytes(data[:2_000] + b"\0" * 3_000)
    _stamp_part(part, src)

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert Path(out).read_bytes() == data


def test_a2_in_place_file_with_other_content_authorises_nothing(tmp_path: Path) -> None:
    src = _src(tmp_path, _content(5_000))
    dest = _dest_dir(tmp_path) / "M.mkv"
    dest.write_bytes(_content(5_000, seed=1))
    before = dest.read_bytes()

    with pytest.raises(LibraryConflictError, match="contents differ"):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert dest.read_bytes() == before
    assert src.exists()


def test_a2_dropped_mount_fails_after_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _src(tmp_path, _content(1000))
    monkeypatch.setattr(importer, "_mount_gone", lambda _base: True)

    with pytest.raises(NasUnavailableError, match="mount dropped"):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert not (_dest_dir(tmp_path) / "M.mkv").exists()


def test_a2_dropped_mount_fails_the_in_place_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))
    (_dest_dir(tmp_path) / "M.mkv").write_bytes(_content(1000))
    monkeypatch.setattr(importer, "_mount_gone", lambda _base: True)

    with pytest.raises(NasUnavailableError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")


def test_a2_post_rename_mismatch_removes_our_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _content(1000)
    src = _src(tmp_path, data)
    real_finalize = importer._finalize_local

    def rename_then_damage(tmp: str, dest: str) -> None:
        real_finalize(tmp, dest)
        Path(dest).write_bytes(b"\0" * len(data))

    monkeypatch.setattr(importer, "_finalize_local", rename_then_damage)
    with pytest.raises(ImportError_, match="after the rename"):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert not (_dest_dir(tmp_path) / "M.mkv").exists()


# --- A7: never replace a library file ---------------------------------------


def test_a7_refuses_to_replace_a_different_size_file(tmp_path: Path) -> None:
    src = _src(tmp_path, _content(5_000))
    dest = _dest_dir(tmp_path) / "M.mkv"
    original = _content(9_000, seed=5)
    dest.write_bytes(original)

    with pytest.raises(LibraryConflictError, match="refusing to replace"):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")

    assert dest.read_bytes() == original
    assert src.exists()
    assert not (dest.parent / "M.mkv.part").exists()  # refused before copying anything


def test_a7_file_that_appears_during_the_copy_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(5_000))
    dest = _dest_dir(tmp_path) / "M.mkv"
    real_copy = importer._copy_stream

    def appears(*a: Any, **k: Any) -> int:
        written = real_copy(*a, **k)
        dest.write_bytes(b"arrived mid-copy")
        return written

    monkeypatch.setattr(importer, "_copy_stream", appears)

    with pytest.raises(LibraryConflictError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert dest.read_bytes() == b"arrived mid-copy"


def test_a7_unreadable_dest_is_not_treated_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stat failure on a flaky share must not read as "nothing to protect"."""
    src = _src(tmp_path, _content(1000))
    dest = _dest_dir(tmp_path) / "M.mkv"
    dest.write_bytes(b"precious")
    real_getsize = os.path.getsize

    def flaky(path: Any) -> int:
        if str(path) == str(dest):
            raise OSError(errno.EIO, "Input/output error")
        return real_getsize(path)

    monkeypatch.setattr(importer.os.path, "getsize", flaky)

    with pytest.raises(NasUnavailableError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert dest.read_bytes() == b"precious"


def test_a7_only_an_empty_leftover_may_be_replaced() -> None:
    assert importer._may_replace(0)
    assert not importer._may_replace(1)


# --- SMB target gets the same rules -----------------------------------------


@pytest.fixture
def fake_smb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``smbclient`` module that maps UNC paths under a tmp dir."""

    root = tmp_path / "share"
    root.mkdir()

    def local(unc: str) -> str:
        return str(root / unc.replace("\\\\host\\T\\", "").replace("\\", "/"))

    module = SimpleNamespace(
        register_session=lambda *a, **k: None,
        ClientConfig=lambda **k: None,
        listdir=lambda p: os.listdir(local(p)),
        makedirs=lambda p, exist_ok=False: os.makedirs(local(p), exist_ok=exist_ok),
        open_file=lambda p, mode="rb": open(local(p), mode),  # noqa: SIM115
        stat=lambda p: os.stat(local(p)),
        remove=lambda p: os.remove(local(p)),
        rename=lambda a, b: os.rename(local(a), local(b)),
    )
    monkeypatch.setitem(sys.modules, "smbclient", module)
    return root


def _smb_target() -> SMBLibraryTarget:
    return SMBLibraryTarget("host", "T", None, None)


def test_a7_smb_refuses_to_replace(tmp_path: Path, fake_smb: Path) -> None:
    src = _src(tmp_path, _content(1000))
    dest = fake_smb / "Movies" / "M" / "M.mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"precious")

    with pytest.raises(LibraryConflictError):
        _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")
    assert dest.read_bytes() == b"precious"
    assert not (dest.parent / "M.mkv.part").exists()  # refused before copying anything


def test_a7_smb_file_that_appears_during_the_copy_is_not_replaced(
    tmp_path: Path, fake_smb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))
    dest = fake_smb / "Movies" / "M" / "M.mkv"
    real_copy = importer._copy_stream

    def appears(*a: Any, **k: Any) -> int:
        written = real_copy(*a, **k)
        dest.write_bytes(b"arrived mid-copy")
        return written

    monkeypatch.setattr(importer, "_copy_stream", appears)

    with pytest.raises(LibraryConflictError):
        _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")
    assert dest.read_bytes() == b"arrived mid-copy"


def test_a2_smb_copy_is_read_back(
    tmp_path: Path, fake_smb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))
    real_copy = importer._copy_stream

    def corrupting(fsrc: Any, fdst: Any, *a: Any, **k: Any) -> int:
        written = real_copy(fsrc, fdst, *a, **k)
        fdst.seek(0)
        fdst.write(b"\xff")
        return written

    monkeypatch.setattr(importer, "_copy_stream", corrupting)

    with pytest.raises(ImportError_, match="read-back"):
        _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")
    assert not (fake_smb / "Movies" / "M" / "M.mkv").exists()


def test_a2_smb_in_place_copy_must_match(tmp_path: Path, fake_smb: Path) -> None:
    src = _src(tmp_path, _content(1000))
    dest = fake_smb / "Movies" / "M" / "M.mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_content(1000, seed=3))

    with pytest.raises(LibraryConflictError):
        _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")


def test_smb_import_round_trip(tmp_path: Path, fake_smb: Path) -> None:
    data = _content(1000)
    src = _src(tmp_path, data)

    _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")
    assert (fake_smb / "Movies" / "M" / "M.mkv").read_bytes() == data

    _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")  # idempotent


def test_a17_smb_enospc_blocks_the_sweep(
    tmp_path: Path, fake_smb: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))

    def full(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(sys.modules["smbclient"], "makedirs", full)
    with pytest.raises(NasUnavailableError):
        _smb_target().import_file(str(src), MOVIE_DIR, "M.mkv")


# --- A17: error classification and best-effort progress ---------------------


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EROFS, errno.EDQUOT])
def test_a17_capacity_errors_block_the_sweep(code: int) -> None:
    classified = importer._classify_oserror(OSError(code, "nope"), "/lib/M.mkv")
    assert isinstance(classified, NasUnavailableError)


def test_a17_enospc_during_copy_keeps_the_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))

    def full(*_a: Any, **_k: Any) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(importer, "_copy_stream", full)
    with pytest.raises(NasUnavailableError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")
    assert (_dest_dir(tmp_path) / "M.mkv.part").exists()


def test_a17_makedirs_errors_are_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _src(tmp_path, _content(1000))

    def stale(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.ESTALE, "Stale NFS file handle")

    monkeypatch.setattr(importer.os, "makedirs", stale)
    with pytest.raises(NasUnavailableError):
        _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv")


def test_a17_failing_progress_callback_does_not_abort_the_copy(tmp_path: Path) -> None:
    data = _content(importer._COPY_CHUNK + 1000)
    src = _src(tmp_path, data)

    def locked(_done: int, _total: int) -> None:
        raise RuntimeError("database is locked")

    out = _target(tmp_path).import_file(str(src), MOVIE_DIR, "M.mkv", locked)

    assert Path(out).read_bytes() == data


def test_a17_failing_progress_does_not_abort_a_batch(tmp_path: Path) -> None:
    """The episode importer's batch wrapper goes through the same guard."""
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "Show.S01E01.mkv").write_bytes(b"e1" * 20)
    config = SimpleNamespace(nas=SimpleNamespace(tv_root="TV Shows"))

    def locked(_done: int, _total: int) -> None:
        raise RuntimeError("database is locked")

    out = importer.import_completed_episodes(
        config,
        _target(tmp_path),
        content_path=str(pack),
        series_title="Show",
        season=1,
        on_progress=locked,
    )
    assert len(out) == 1


# --- A3: videos left behind -------------------------------------------------


def test_a3_finds_a_sizeable_video_that_was_not_imported(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    ep = pack / "Show.S01E01.mkv"
    ep.write_bytes(b"e" * 1000)
    lost = pack / "Show - Pilot.mkv"
    lost.write_bytes(b"e" * 900)
    (pack / "Featurette.mkv").write_bytes(b"f" * 100)
    (pack / "sample.mkv").write_bytes(b"s" * 900)

    assert find_unimported_videos(str(pack), [str(ep)]) == [(str(lost), 900)]


def test_a3_movie_default_treats_the_primary_as_imported(tmp_path: Path) -> None:
    release = tmp_path / "movie"
    release.mkdir()
    (release / "Movie.CD1.mkv").write_bytes(b"a" * 1000)
    cd2 = release / "Movie.CD2.mkv"
    cd2.write_bytes(b"b" * 999)

    assert find_unimported_videos(str(release)) == [(str(cd2), 999)]


def test_a3_sample_cap_bounds_the_size_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A large extra next to huge episodes is still held."""
    monkeypatch.setattr(importer, "_SAMPLE_MAX_BYTES", 300)
    pack = tmp_path / "pack"
    pack.mkdir()
    ep = pack / "Show.S01E01.mkv"
    ep.write_bytes(b"e" * 4000)
    extra = pack / "Making Of.mkv"
    extra.write_bytes(b"x" * 300)
    (pack / "Trailer.mkv").write_bytes(b"t" * 299)

    assert find_unimported_videos(str(pack), [str(ep)]) == [(str(extra), 300)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a3_partly_unreadable_torrent_is_an_error_not_a_short_list(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    hidden = pack / "Disc2"
    hidden.mkdir(parents=True)
    (pack / "Show.S01E01.mkv").write_bytes(b"e" * 100)
    (hidden / "Show.S01E02.mkv").write_bytes(b"e" * 100)
    hidden.chmod(0)
    try:
        with pytest.raises(ImportError_, match="could not read everything"):
            find_unimported_videos(str(pack), [str(pack / "Show.S01E01.mkv")])
    finally:
        hidden.chmod(0o755)


# --- sync flows -------------------------------------------------------------


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, delete_local: bool = True) -> None:
    cfg = tmp_path / "torrent.yaml"
    cfg.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'torrent.db'}'}}\n"
        "features: {dry_run: false, auto_approve: false}\n"
        "acquisition: {backend: torrent}\n"
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        f"  library_base_dir: {tmp_path / 'lib'}\n"
        f"  delete_local_after_import: {str(delete_local).lower()}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(cfg))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)
    _reset()


def _seed(
    *,
    kind: TitleKind = TitleKind.movie,
    season: int | None = None,
    target: int | None = None,
    release: str = "m",
) -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        CandidateStatus,
        Download,
        Title,
    )

    init_db()
    with session_scope() as s:
        title = Title(tmdb_id=1, title="Silo", year=2023, kind=kind)
        s.add(title)
        s.flush()
        cand = Candidate(
            title_id=title.id,
            season=season,
            source=CandidateSource.discovery,
            status=CandidateStatus.downloading,
            features={"season_episodes": target} if target else None,
        )
        s.add(cand)
        s.flush()
        s.add(
            Download(candidate_id=cand.id, external_id=HASH, state="downloading", release=release)
        )
        return cand.id


class FakeTransmission:
    """Answers torrent-get for one complete torrent and records every RPC."""

    def __init__(self, dl_dir: Path, name: str) -> None:
        self.dl_dir = dl_dir
        self.name = name
        self.calls: list[str] = []
        self.gone = False
        self.remove_error = False
        self.on_remove: Callable[[], None] | None = None
        self._negotiated = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if not self._negotiated:
            self._negotiated = True
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "s"})
        body = json.loads(request.content)
        self.calls.append(body["method"])
        if body["method"] == "torrent-remove":
            if self.on_remove is not None:
                self.on_remove()
            if self.remove_error:
                return httpx.Response(500)
            self.gone = True
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        torrent = {
            "hashString": body["arguments"]["ids"][0],
            "name": self.name,
            "percentDone": 1.0,
            "status": 6,
            "downloadDir": str(self.dl_dir),
            "error": 0,
            "errorString": "",
        }
        return httpx.Response(
            200,
            json={
                "result": "success",
                "arguments": {"torrents": [] if self.gone else [torrent]},
            },
        )


def _transmission(dl_dir: Path, name: str) -> FakeTransmission:
    fake = FakeTransmission(dl_dir, name)
    respx.post(TRANSMISSION).mock(side_effect=fake)
    return fake


async def _sync() -> Any:
    from homeTheater.acquisition import sync_downloads
    from homeTheater.config import get_config

    return await sync_downloads(get_config())


def _end_import_backoff() -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import DownloadTracking

    with session_scope() as s:
        for track in s.query(DownloadTracking).all():
            track.next_import_at = None


def _state() -> tuple[str, str | None, Any]:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, Download

    with session_scope() as s:
        dl = s.query(Download).one()
        return dl.state, dl.error, s.get(Candidate, dl.candidate_id).status


def _movie_files(tmp_path: Path, name: str = "movie.mkv", size: int = 2000) -> Path:
    dl_dir = tmp_path / "dl"
    dl_dir.mkdir()
    (dl_dir / name).write_bytes(_content(size))
    return dl_dir


@respx.mock
async def test_a1_sync_ties_the_part_to_the_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real sync: a leftover from another torrent at the same dest is not resumed."""
    monkeypatch.setattr(importer, "_VERIFY_CHUNK", 64)
    _config(tmp_path, monkeypatch, delete_local=False)
    dl_dir = _movie_files(tmp_path)
    src = dl_dir / "movie.mkv"
    _seed()
    dest_dir = tmp_path / "lib" / "Movies" / "Silo (2023)"
    dest_dir.mkdir(parents=True)
    part = dest_dir / "Silo (2023).mkv.part"
    part.write_bytes(_foreign_part(src.read_bytes(), 1000))
    _stamp_part(part, src, "b" * 40)  # same file stats, other torrent
    _transmission(dl_dir, "movie.mkv")

    await _sync()

    assert (dest_dir / "Silo (2023).mkv").read_bytes() == src.read_bytes()


@respx.mock
async def test_a1_sync_resumes_a_part_from_the_same_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch, delete_local=False)
    dl_dir = _movie_files(tmp_path)
    src = dl_dir / "movie.mkv"
    _seed()
    dest_dir = tmp_path / "lib" / "Movies" / "Silo (2023)"
    dest_dir.mkdir(parents=True)
    part = dest_dir / "Silo (2023).mkv.part"
    part.write_bytes(src.read_bytes()[:1000])
    _stamp_part(part, src, HASH)
    inode = part.stat().st_ino
    _transmission(dl_dir, "movie.mkv")

    await _sync()

    dest = dest_dir / "Silo (2023).mkv"
    assert dest.read_bytes() == src.read_bytes()
    assert dest.stat().st_ino == inode  # appended to, not rewritten


@respx.mock
async def test_a2_verification_failure_keeps_local_and_stays_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db.models import CandidateStatus

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")
    with monkeypatch.context() as patched:
        patched.setattr(importer, "_copy_matches", lambda *_a: False)
        stats = await _sync()

    assert stats.completed == 0
    assert "torrent-remove" not in fake.calls
    assert (dl_dir / "movie.mkv").exists()
    state, error, status = _state()
    assert state == "completed" and error and "read-back" in error
    assert status == CandidateStatus.downloading

    _end_import_backoff()
    stats = await _sync()

    assert stats.completed == 1
    assert "torrent-remove" in fake.calls


@respx.mock
async def test_a2_in_place_mismatch_does_not_authorise_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    dest = tmp_path / "lib" / "Movies" / "Silo (2023)" / "Silo (2023).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(_content(2000, seed=8))
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")

    stats = await _sync()

    assert stats.completed == 0
    assert "torrent-remove" not in fake.calls
    assert _state()[0] == "completed"


@respx.mock
async def test_a7_sync_refuses_and_keeps_both_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    dest = tmp_path / "lib" / "Movies" / "Silo (2023)" / "Silo (2023).mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"the copy already in the library")
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")

    stats = await _sync()

    assert dest.read_bytes() == b"the copy already in the library"
    assert (dl_dir / "movie.mkv").exists()
    assert "torrent-remove" not in fake.calls
    assert stats.completed == 0 and stats.errors
    state, error, _status = _state()
    assert state == "completed" and error and "refusing to replace" in error


@respx.mock
async def test_a17_enospc_stops_the_sweep_after_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full share fails the same way for every file: stop instead of thrashing."""
    from homeTheater.db import session_scope
    from homeTheater.db.models import Download

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    cid = _seed()
    with session_scope() as s:
        s.add(Download(candidate_id=cid, external_id="b" * 40, state="downloading", release="n"))
    attempts: list[int] = []

    def full(*_a: Any, **_k: Any) -> int:
        attempts.append(1)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(importer, "_copy_stream", full)
    _transmission(dl_dir, "movie.mkv")

    stats = await _sync()

    assert attempts == [1]
    assert stats.errors and "NAS unavailable" in stats.errors[0]


@respx.mock
async def test_a3_pack_with_an_unparsable_episode_is_held_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduced bug: the skipped video went down with the torrent."""
    from homeTheater.db import session_scope
    from homeTheater.db.models import CandidateStatus, OwnedFile

    _config(tmp_path, monkeypatch)
    pack = "Silo.S03.COMPLETE.1080p.WEB.H264-GROUP"
    dl_dir = tmp_path / "dl"
    (dl_dir / pack).mkdir(parents=True)
    (dl_dir / pack / "Silo.S03E01.1080p.WEB.mkv").write_bytes(b"e1" * 500)
    (dl_dir / pack / "Silo.S03E02.1080p.WEB.mkv").write_bytes(b"e2" * 500)
    lost = dl_dir / pack / "Silo - The Finale.mkv"
    lost.write_bytes(b"e3" * 500)
    _seed(kind=TitleKind.series, season=3, target=3, release=pack)
    fake = _transmission(dl_dir, pack)

    stats = await _sync()

    assert "torrent-remove" not in fake.calls
    assert lost.exists()
    assert stats.completed == 0
    assert any("held:" in e and "Silo - The Finale.mkv" in e for e in stats.errors)
    state, error, status = _state()
    assert state == "completed" and error and error.startswith("held:")
    assert status == CandidateStatus.downloading  # still visible on Activity
    with session_scope() as s:
        assert {o.episode for o in s.query(OwnedFile).all()} == {1, 2}  # those did land

    # The owner renames it; the next sweep imports it and only then removes the torrent.
    lost.rename(dl_dir / pack / "Silo.S03E03.1080p.WEB.mkv")
    await _sync()

    assert "torrent-remove" in fake.calls
    assert _state() == ("imported", None, CandidateStatus.imported)


@respx.mock
async def test_a3_hold_does_not_refetch_subtitles_every_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held download re-runs each sweep, which must not spend provider quota."""
    import homeTheater.subtitles.native.service as subs

    _config(tmp_path, monkeypatch)
    cfg = tmp_path / "torrent.yaml"
    cfg.write_text(cfg.read_text() + "subtitles: {backend: native}\n")
    _reset()
    release = tmp_path / "dl"
    (release / "m").mkdir(parents=True)
    (release / "m" / "Movie.CD1.mkv").write_bytes(b"a" * 1000)
    (release / "m" / "Movie.CD2.mkv").write_bytes(b"b" * 999)
    _seed()
    fake = _transmission(release, "m")
    fetched: list[int] = []

    async def fetch(_config_arg: Any, owned_id: int) -> list[Any]:
        fetched.append(owned_id)
        return []

    monkeypatch.setattr(subs, "fetch_for_owned_file", fetch)

    await _sync()

    assert fetched == []
    assert "torrent-remove" not in fake.calls


@respx.mock
async def test_a3_movie_with_a_second_real_video_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path, monkeypatch)
    release = tmp_path / "dl"
    (release / "m").mkdir(parents=True)
    (release / "m" / "Movie.CD1.mkv").write_bytes(b"a" * 1000)
    (release / "m" / "Movie.CD2.mkv").write_bytes(b"b" * 999)
    _seed()
    fake = _transmission(release, "m")

    await _sync()

    assert "torrent-remove" not in fake.calls
    state, error, _status = _state()
    assert state == "completed" and error and "Movie.CD2.mkv" in error


@respx.mock
async def test_a3_short_pack_of_a_finished_season_stays_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release calls itself complete; only what was imported counts."""
    from homeTheater.db.models import CandidateStatus

    _config(tmp_path, monkeypatch, delete_local=False)
    pack = "Silo.S03.COMPLETE.1080p.WEB.H264-GROUP"
    dl_dir = tmp_path / "dl"
    (dl_dir / pack).mkdir(parents=True)
    (dl_dir / pack / "Silo.S03E01.1080p.WEB.mkv").write_bytes(b"e1" * 500)
    _seed(kind=TitleKind.series, season=3, target=2, release=pack)
    _transmission(dl_dir, pack)

    await _sync()

    assert _state() == ("imported", None, CandidateStatus.approved)


async def test_a3_top_up_is_not_blocked_by_an_imported_short_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back at approved, a short pack must not read as "pack in flight" forever."""
    from homeTheater.acquisition.torrent.service import _grabbed_episodes
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, Download, OwnedFile

    _config(tmp_path, monkeypatch)
    pack = "Silo.S03.COMPLETE.1080p.WEB.H264-GROUP"
    cid = _seed(kind=TitleKind.series, season=3, target=2, release=pack)
    with session_scope() as s:
        s.query(Download).one().state = "imported"
        title_id = s.get(Candidate, cid).title_id
        s.add(
            OwnedFile(
                path="/nas/e1.mkv", title_id=title_id, kind=TitleKind.series, season=3, episode=1
            )
        )

    assert _grabbed_episodes(cid, title_id, 3) == {1}

    with session_scope() as s:
        s.query(Download).one().state = "downloading"
    assert _grabbed_episodes(cid, title_id, 3) is None  # a live pack still blocks


# --- A18: commit before removing --------------------------------------------


@respx.mock
async def test_a18_imported_commits_before_the_torrent_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db.models import CandidateStatus

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")
    seen: list[tuple[str, str | None, Any]] = []
    fake.on_remove = lambda: seen.append(_state())

    await _sync()

    assert seen and seen[0][0] == "imported"
    assert seen[0][2] == CandidateStatus.imported
    assert _state() == ("imported", None, CandidateStatus.imported)  # marker cleared


@respx.mock
async def test_a18_crash_at_removal_does_not_regrab_and_next_sweep_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition.torrent.service import _REMOVAL_PENDING
    from homeTheater.db.models import CandidateStatus

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")

    class Crash(BaseException):
        pass

    def crash() -> None:
        raise Crash

    fake.on_remove = crash
    with pytest.raises(Crash):
        await _sync()

    assert _state() == ("imported", _REMOVAL_PENDING, CandidateStatus.imported)

    fake.on_remove = None
    fake.calls.clear()
    await _sync()

    assert "torrent-remove" in fake.calls
    assert "torrent-add" not in fake.calls
    assert _state() == ("imported", None, CandidateStatus.imported)


@respx.mock
async def test_a18_failed_removal_retries_later_without_touching_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition.torrent.service import _REMOVAL_PENDING
    from homeTheater.db.models import CandidateStatus

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")
    fake.remove_error = True

    stats = await _sync()

    assert stats.completed == 1
    assert _state() == ("imported", _REMOVAL_PENDING, CandidateStatus.imported)

    fake.remove_error = False
    await _sync()

    assert _state() == ("imported", None, CandidateStatus.imported)


@respx.mock
async def test_a18_pending_removal_is_never_a_quarantine_ban(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition.torrent.service import _REMOVAL_PENDING, _quarantined_hashes

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")
    fake.remove_error = True

    await _sync()

    assert _state()[1] == _REMOVAL_PENDING
    assert _quarantined_hashes() == frozenset()


@respx.mock
async def test_a18_removal_waits_for_the_library_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.acquisition.torrent import service

    _config(tmp_path, monkeypatch)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")
    monkeypatch.setattr(service, "mount_lost", lambda _config_arg: True)

    await _sync()

    assert "torrent-remove" not in fake.calls
    assert _state()[1] == service._REMOVAL_PENDING

    monkeypatch.setattr(service, "mount_lost", lambda _config_arg: False)
    await _sync()

    assert "torrent-remove" in fake.calls
    assert _state()[1] is None


@respx.mock
async def test_a18_no_removal_when_delete_local_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db.models import CandidateStatus

    _config(tmp_path, monkeypatch, delete_local=False)
    dl_dir = _movie_files(tmp_path)
    _seed()
    fake = _transmission(dl_dir, "movie.mkv")

    await _sync()

    assert "torrent-remove" not in fake.calls
    assert _state() == ("imported", None, CandidateStatus.imported)
