"""F6: the timing check, with ffsubsync faked at subprocess.Popen."""

from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from homeTheater.db.models import (
    OwnedFile,
    Subtitle,
    SubtitleReject,
    SubtitleSync,
    Title,
    TitleKind,
)
from homeTheater.errors import JobBusyError, NotConfiguredError
from homeTheater.subtitles import sync
from homeTheater.subtitles.native import service as native
from homeTheater.subtitles.native.base import SubtitleResult

SUB = (
    b"1\n00:00:10,000 --> 00:00:12,000\nhello\n\n"
    b"2\n00:01:00,000 --> 00:01:02,000\nthere\n\n"
    b"3\n00:02:00,000 --> 00:02:02,000\nfriend\n"
)
SHIFTED = SUB.replace(b"00:00:10", b"00:00:12").replace(b"00:01:00", b"00:01:02")
SHIFTED = SHIFTED.replace(b"00:02:00", b"00:02:02")


class FakePopen:
    """Stands in for ffsubsync: writes ``output`` to the -o path, or never finishes."""

    calls: list[list[str]] = []
    output: bytes | None = SHIFTED
    code: int = 0
    hangs: bool = False

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        type(self).calls.append(argv)
        self.kwargs = kwargs
        self.pid = 4242
        self.killed = False
        self.waits = 0
        if type(self).output is not None and not type(self).hangs:
            Path(argv[argv.index("-o") + 1]).write_bytes(type(self).output)

    def wait(self, timeout: float | None = None) -> int:
        if type(self).hangs and not self.killed:
            self.waits += 1
            assert self.waits < 1000, "nothing stopped a hung ffsubsync"
            raise subprocess.TimeoutExpired("ffsubsync", timeout or 0)
        return -9 if self.killed else type(self).code


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    killed: list[int] = []
    FakePopen.calls = []
    FakePopen.output = SHIFTED
    FakePopen.code = 0
    FakePopen.hangs = False
    active: list[FakePopen] = []

    def factory(argv: list[str], **kwargs: Any) -> FakePopen:
        proc = FakePopen(argv, **kwargs)
        active.append(proc)
        return proc

    def killpg(pid: int, _sig: int) -> None:
        killed.append(pid)
        for proc in active:
            proc.killed = True

    monkeypatch.setattr(sync.subprocess, "Popen", factory)
    monkeypatch.setattr(sync.os, "killpg", killpg)
    monkeypatch.setattr(sync, "POLL_S", 0.0)
    monkeypatch.setattr(sync.shutil, "which", lambda name: f"/opt/{name}")
    return killed


def _setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True, extra: str = ""
) -> Any:
    from homeTheater.config import get_config, loader
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session

    (tmp_path / "subs.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'subs.db'}'}}\n"
        "subtitles:\n"
        "  backend: native\n"
        "  sources: [opensubtitles]\n"
        "  languages: [en]\n"
        f"  sync: {{enabled: {'true' if enabled else 'false'}{extra}}}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "subs.yaml"))
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "key")
    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()
    return get_config()


def _add(
    tmp_path: Path, name: str = "Film", *, state: str | None = None, data: bytes = SUB
) -> tuple[int, Path]:
    from homeTheater.db import session_scope

    folder = tmp_path / "lib" / name
    (folder / "Subs").mkdir(parents=True)
    media = folder / f"{name}.mkv"
    media.write_bytes(b"m" * 1000)
    sub_path = folder / "Subs" / f"{name}.en.srt"
    sub_path.write_bytes(data)
    with session_scope() as s:
        title = Title(title=name, kind=TitleKind.movie)
        s.add(title)
        s.flush()
        owned = OwnedFile(title_id=title.id, path=str(media), kind=TitleKind.movie)
        s.add(owned)
        s.flush()
        s.add(
            Subtitle(
                title_id=title.id,
                owned_file_id=owned.id,
                lang="en",
                provider="ktuvit",
                path=str(sub_path),
                status="downloaded",
            )
        )
        if state is not None:
            s.add(
                SubtitleSync(
                    owned_file_id=owned.id,
                    lang="en",
                    state=state,
                    hash_match=state == "hash_match",
                    fail_count=0,
                )
            )
        return owned.id, sub_path


def _state(owned_id: int) -> SubtitleSync:
    from homeTheater.db import session_scope

    with session_scope() as s:
        row = s.query(SubtitleSync).filter_by(owned_file_id=owned_id, lang="en").one()
        s.expunge(row)
        return row


async def test_out_of_time_subtitle_is_corrected_and_the_original_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned_id, path = _add(tmp_path)

    stats = await sync.run_sync_check(config)

    assert stats.corrected == 1
    assert path.read_bytes() == SHIFTED
    backup = Path(str(path) + ".orig")
    assert backup.read_bytes() == SUB
    row = _state(owned_id)
    assert row.state == "corrected"
    assert row.offset_seconds == pytest.approx(2.0)
    assert row.original_path == str(backup)
    assert row.original_source == "ktuvit"
    assert not kills


async def test_ffsubsync_gets_the_media_and_a_temp_copy_never_the_library_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    _, path = _add(tmp_path)

    await sync.run_sync_check(config)

    argv = FakePopen.calls[0]
    assert argv[:2] == ["ffsubsync", str(path.parent.parent / "Film.mkv")]
    assert argv[2] == "-i" and argv[4] == "-o"
    assert str(path) not in argv


async def test_a_subtitle_already_in_time_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned_id, path = _add(tmp_path)
    FakePopen.output = SUB.replace(b"00:00:10,000", b"00:00:10,100")

    stats = await sync.run_sync_check(config)

    assert stats.in_sync == 1 and stats.corrected == 0
    assert path.read_bytes() == SUB
    assert not Path(str(path) + ".orig").exists()
    assert _state(owned_id).state == "in_sync"


@pytest.mark.parametrize(
    ("output", "code"),
    [
        (b"<html>not a subtitle</html>", 0),  # fails validate_subtitle
        (SHIFTED.split(b"\n\n")[0], 0),  # cues went missing
        (SUB.replace(b"00:00:10", b"00:10:10"), 0),  # 10 minutes: implausible
        (SHIFTED, 1),  # ffsubsync failed
        (None, 0),  # wrote nothing
    ],
)
async def test_a_bad_result_touches_nothing_and_cools_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kills: list[int],
    output: bytes | None,
    code: int,
) -> None:
    from homeTheater.db.base import utcnow

    config = _setup(tmp_path, monkeypatch)
    owned_id, path = _add(tmp_path)
    FakePopen.output, FakePopen.code = output, code

    stats = await sync.run_sync_check(config)

    assert stats.failed == 1
    assert path.read_bytes() == SUB
    assert not Path(str(path) + ".orig").exists()
    row = _state(owned_id)
    assert row.state == "failed" and row.fail_count == 1 and row.detail
    assert sync._collect(utcnow()) == []  # cooling down
    assert sync._collect(utcnow() + timedelta(days=2))  # then due again


async def test_a_format_change_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned_id, path = _add(tmp_path)
    FakePopen.output = (
        b"[Script Info]\nScriptType: v4.00+\n"
        b"Dialogue: 0,0:00:12.00,0:00:14.00,D,,0,0,0,,hello\n"
        b"Dialogue: 0,0:01:02.00,0:01:04.00,D,,0,0,0,,there\n"
        b"Dialogue: 0,0:02:02.00,0:02:04.00,D,,0,0,0,,friend\n"
    )

    stats = await sync.run_sync_check(config)

    assert stats.failed == 1 and path.read_bytes() == SUB
    assert "format" in (_state(owned_id).detail or "")


async def test_a_frame_rate_drift_counts_even_with_no_median_shift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    _, path = _add(tmp_path)
    FakePopen.output = SUB.replace(b"00:00:10", b"00:00:09").replace(b"00:02:00", b"00:02:01")

    assert (await sync.run_sync_check(config)).corrected == 1
    assert Path(str(path) + ".orig").exists()


def test_a_second_correction_keeps_the_true_original(tmp_path: Path) -> None:
    path = tmp_path / "x.en.srt"
    path.write_bytes(SHIFTED)

    sync._store_corrected(str(path), SUB, SHIFTED)
    sync._store_corrected(str(path), SHIFTED, b"later")

    assert Path(str(path) + ".orig").read_bytes() == SUB
    assert path.read_bytes() == b"later"


def test_backup_failure_leaves_the_subtitle_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "x.en.srt"
    path.write_bytes(SUB)

    def boom(_dest: str, _data: bytes) -> None:
        raise OSError("NAS said no")

    monkeypatch.setattr(sync, "write_subtitle", boom)
    with pytest.raises(OSError, match="NAS said no"):
        sync._store_corrected(str(path), SUB, SHIFTED)
    assert path.read_bytes() == SUB


async def test_hard_timeout_kills_the_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned_id, path = _add(tmp_path)
    FakePopen.hangs = True
    clock = iter([0.0, 10_000.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(sync.time, "time", lambda: next(clock, 10_000.0))

    stats = await sync.run_sync_check(config)

    assert kills == [4242]
    assert stats.failed == 1 and path.read_bytes() == SUB
    assert "timed out" in (_state(owned_id).detail or "")


async def test_a_process_that_survives_sigkill_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    _add(tmp_path, "A")
    _add(tmp_path, "B")

    def never(*_a: Any, **_k: Any) -> None:
        raise sync._StuckError("ffsubsync would not die")

    monkeypatch.setattr(sync, "_retime", never)

    stats = await sync.run_sync_check(config)

    assert stats.failed == 1  # B was never started


async def test_import_active_defers_the_whole_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    from homeTheater.locks import import_active

    config = _setup(tmp_path, monkeypatch)
    _, path = _add(tmp_path)

    with import_active(config):
        stats = await sync.run_sync_check(config)

    assert stats.deferred and not FakePopen.calls
    assert path.read_bytes() == SUB


async def test_an_import_starting_mid_run_kills_ffsubsync_without_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    from homeTheater.db.base import utcnow

    config = _setup(tmp_path, monkeypatch)
    _, path = _add(tmp_path)
    FakePopen.hangs = True
    answers = iter([False, True])
    monkeypatch.setattr(sync, "is_import_active", lambda _c: next(answers, True))

    stats = await sync.run_sync_check(config)

    assert kills == [4242]
    assert stats.deferred and stats.failed == 0
    assert path.read_bytes() == SUB
    assert sync._collect(utcnow())  # still due: a deferral is not a failure


async def test_one_run_at_a_time_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    from homeTheater.locks import job_lock

    config = _setup(tmp_path, monkeypatch)
    _add(tmp_path)

    with job_lock(config, sync.SYNC_LOCK), pytest.raises(JobBusyError):
        await sync.run_sync_check(config)
    assert not FakePopen.calls


async def test_files_are_checked_serially_up_to_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch, extra=", max_per_run: 2")
    for name in "ABC":
        _add(tmp_path, name)

    stats = await sync.run_sync_check(config)

    assert stats.corrected == 2 and stats.capped == 1
    assert len(FakePopen.calls) == 2


@pytest.mark.parametrize("state", ["hash_match", "approved", "reverted", "in_sync", "corrected"])
async def test_settled_subtitles_are_not_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int], state: str
) -> None:
    config = _setup(tmp_path, monkeypatch)
    _add(tmp_path, state=state)

    stats = await sync.run_sync_check(config)

    assert stats.considered == 0 and not FakePopen.calls


async def test_missing_tools_are_a_clear_skip_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    from homeTheater.scheduler import jobs

    config = _setup(tmp_path, monkeypatch)
    _add(tmp_path)
    monkeypatch.setattr(
        sync.shutil, "which", lambda name: None if name == "ffsubsync" else "/bin/x"
    )

    with pytest.raises(NotConfiguredError, match="ffsubsync not found on PATH"):
        await sync.run_sync_check(config)
    await jobs.run_subtitle_sync_job(config)

    assert "ffsubsync not found on PATH" in jobs.skip_reasons["subtitle_sync"]
    assert not FakePopen.calls


async def test_off_by_default_nothing_runs_or_registers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    from homeTheater.scheduler.catchup import job_intervals

    config = _setup(tmp_path, monkeypatch, enabled=False)

    with pytest.raises(NotConfiguredError, match="enabled is off"):
        await sync.run_sync_check(config)
    assert "subtitle_sync" not in job_intervals(config.schedule, config)
    assert type(config.subtitles.sync).model_fields["enabled"].default is False


def test_the_check_needs_the_native_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    config = _setup(tmp_path, monkeypatch)
    assert sync.unavailable_reason(config) is None
    bazarr = config.model_copy(
        update={"subtitles": config.subtitles.model_copy(update={"backend": "bazarr"})}
    )
    assert "native" in (sync.unavailable_reason(bazarr) or "")


async def test_scheduled_deferral_is_a_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kills: list[int]
) -> None:
    from homeTheater.locks import import_active
    from homeTheater.scheduler import jobs

    config = _setup(tmp_path, monkeypatch)
    _add(tmp_path)

    with import_active(config):
        await jobs.run_subtitle_sync_job(config)

    assert "deferred" in jobs.skip_reasons["subtitle_sync"]


def test_ass_cues_are_measured_too() -> None:
    before = "[Script Info]\nDialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,hi\n"
    after = before.replace("0:00:10.00", "0:00:13.00")
    assert sync.measure_shift(before.encode(), after.encode())[0] == pytest.approx(3.0)


def test_the_scanner_and_kodi_naming_ignore_the_backup() -> None:
    from homeTheater.scanner.parse import is_subtitle_file

    assert is_subtitle_file("Film.en.srt")
    assert not is_subtitle_file("Film.en.srt" + sync.ORIGINAL_SUFFIX)


# --- the sweep: provenance and rejected subtitles


class Source:
    name = "fake"

    def __init__(self, results: list[SubtitleResult], payloads: dict[str, bytes]) -> None:
        self.results = results
        self.payloads = payloads
        self.downloaded: list[str] = []

    def supports(self, _lang: str) -> bool:
        return True

    async def search(self, _query: Any) -> list[SubtitleResult]:
        return self.results

    async def download(self, result: SubtitleResult) -> bytes:
        self.downloaded.append(result.ref["id"])
        return self.payloads[result.ref["id"]]


def _result(ref: str, **kw: Any) -> SubtitleResult:
    return SubtitleResult(source="fake", lang="en", name=ref, score=1.0, ref={"id": ref}, **kw)


def _missing_file(tmp_path: Path, name: str = "Film") -> int:
    owned_id, path = _add(tmp_path, name)
    from homeTheater.db import session_scope

    path.unlink()
    with session_scope() as s:
        s.query(Subtitle).delete()
    return owned_id


async def test_sweep_records_the_choice_when_the_check_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    hashed, plain = _missing_file(tmp_path, "A"), _missing_file(tmp_path, "B")
    source = Source([_result("a", hash_match=True)], {"a": SUB})
    monkeypatch.setattr(native, "_build_sources", lambda _c, _h: [source])
    await native.fetch_for_owned_file(config, hashed)
    source.results = [_result("b")]
    source.payloads = {"b": SUB + b"\n"}
    await native.fetch_for_owned_file(config, plain)

    assert _state(hashed).state == "hash_match" and _state(hashed).hash_match
    assert _state(plain).state == "pending" and not _state(plain).hash_match
    assert _state(plain).release == "b" and _state(plain).source == "fake"


async def test_with_the_check_off_the_sweep_writes_no_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    config = _setup(tmp_path, monkeypatch, enabled=False)
    owned_id = _missing_file(tmp_path)
    source = Source([_result("a")], {"a": SUB})
    monkeypatch.setattr(native, "_build_sources", lambda _c, _h: [source])

    assert await native.fetch_for_owned_file(config, owned_id)
    with session_scope() as s:
        assert s.query(SubtitleSync).count() == 0


async def test_a_provenance_failure_does_not_lose_the_subtitle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _setup(tmp_path, monkeypatch)
    owned_id = _missing_file(tmp_path)
    source = Source([_result("a")], {"a": SUB})
    monkeypatch.setattr(native, "_build_sources", lambda _c, _h: [source])

    def boom(*_a: Any) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr(native, "record_choice", boom)
    assert await native.fetch_for_owned_file(config, owned_id)


async def test_rejected_result_is_not_chosen_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.subtitles.choices import result_key

    config = _setup(tmp_path, monkeypatch)
    owned_id = _missing_file(tmp_path)
    bad, good = _result("bad", hash_match=True), _result("good")
    with session_scope() as s:
        s.add(
            SubtitleReject(
                owned_file_id=owned_id, lang="en", result_key=result_key(bad), rejected_at=utcnow()
            )
        )
    source = Source([bad, good], {"bad": SUB, "good": SHIFTED})
    monkeypatch.setattr(native, "_build_sources", lambda _c, _h: [source])

    assert await native.fetch_for_owned_file(config, owned_id)
    assert source.downloaded == ["good"]  # the rejected one cost no download


async def test_rejected_content_is_not_chosen_under_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow
    from homeTheater.subtitles.choices import content_hash

    config = _setup(tmp_path, monkeypatch)
    owned_id = _missing_file(tmp_path)
    with session_scope() as s:
        s.add(
            SubtitleReject(
                owned_file_id=owned_id,
                lang="en",
                content_sha256=content_hash(SUB),
                rejected_at=utcnow(),
            )
        )
    source = Source(
        [_result("renamed", hash_match=True), _result("good")],
        {
            "renamed": SUB,
            "good": SHIFTED,
        },
    )
    monkeypatch.setattr(native, "_build_sources", lambda _c, _h: [source])

    await native.fetch_for_owned_file(config, owned_id)

    with session_scope() as s:
        placed = s.query(Subtitle).one()
        assert Path(placed.path or "").read_bytes() == SHIFTED


def test_a_session_id_in_a_link_does_not_change_the_key() -> None:
    from homeTheater.subtitles.choices import result_key

    def org(link: str) -> SubtitleResult:
        return SubtitleResult(source="org", lang="en", name="n", score=1, ref={"link": link})

    first = org("https://dl.example/en/download/sid-AAA/file/1")
    assert result_key(first) == result_key(org("https://dl.example/en/download/sid-BBB/file/1"))
    assert result_key(first) != result_key(org("https://dl.example/en/download/sid-AAA/file/2"))
