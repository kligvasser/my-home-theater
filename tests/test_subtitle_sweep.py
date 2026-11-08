"""The native sweep end to end, with fake providers and a temp library."""

from __future__ import annotations

import asyncio
import errno
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from homeTheater.db.models import (
    JobRun,
    OwnedFile,
    RunStatus,
    Subtitle,
    SubtitleAttempt,
    Title,
    TitleKind,
)
from homeTheater.errors import JobBusyError, NotConfiguredError
from homeTheater.subtitles.native import service as native
from homeTheater.subtitles.native.base import (
    SourceAuthError,
    SourceQuotaError,
    SubtitleResult,
    SubtitleSourceError,
)

GOOD = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
HTML = b"<html><body>quota</body></html>"


class FakeSource:
    def __init__(
        self,
        name: str = "fake",
        *,
        results: Callable[[Any], list[SubtitleResult]] | None = None,
        payloads: dict[str, bytes | Exception] | None = None,
        search_error: Exception | None = None,
        langs: tuple[str, ...] = ("en", "he"),
    ) -> None:
        self.name = name
        self._results = results
        self._payloads = payloads or {}
        self._search_error = search_error
        self._langs = langs
        self.searched: list[Any] = []
        self.downloaded: list[str] = []

    def supports(self, lang: str) -> bool:
        return lang in self._langs

    async def search(self, query: Any) -> list[SubtitleResult]:
        self.searched.append(query)
        if self._search_error is not None:
            raise self._search_error
        if self._results is not None:
            return self._results(query)
        return [_result(self.name, "only")]

    async def download(self, result: SubtitleResult) -> bytes:
        self.downloaded.append(result.ref["id"])
        payload = self._payloads.get(result.ref["id"], GOOD)
        if isinstance(payload, Exception):
            raise payload
        return payload


def _result(source: str, ref: str, score: float = 1.0, **kw: Any) -> SubtitleResult:
    return SubtitleResult(source=source, lang="en", name=ref, score=score, ref={"id": ref}, **kw)


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extra: str = "",
    languages: str = "[en]",
) -> None:
    from homeTheater.config import loader
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session

    (tmp_path / "subs.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'subs.db'}'}}\n"
        "subtitles:\n"
        "  backend: native\n"
        "  sources: [opensubtitles]\n"
        f"  languages: {languages}\n" + extra
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "subs.yaml"))
    monkeypatch.setenv("OPENSUBTITLES_API_KEY", "key")
    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()


def _use_sources(monkeypatch: pytest.MonkeyPatch, *sources: Any) -> None:
    monkeypatch.setattr(native, "_build_sources", lambda _config, _http: list(sources))


def _add_file(
    tmp_path: Path,
    name: str,
    *,
    created: datetime | None = None,
    present: bool = True,
    kind: TitleKind = TitleKind.movie,
    season: int | None = None,
    episode: int | None = None,
) -> int:
    from homeTheater.db import session_scope

    folder = tmp_path / "lib" / name
    folder.mkdir(parents=True)
    media = folder / f"{name}.mkv"
    if present:
        media.write_bytes(b"m" * 200_000)
    with session_scope() as s:
        title = Title(title=name, year=2020, kind=kind, imdb_id=None)
        s.add(title)
        s.flush()
        row = OwnedFile(
            title_id=title.id, path=str(media), kind=kind, season=season, episode=episode
        )
        if created is not None:
            row.created_at = created
        s.add(row)
        s.flush()
        return row.id


def _dest(tmp_path: Path, name: str, lang: str = "en", ext: str = "srt") -> Path:
    return tmp_path / "lib" / name / "Subs" / f"{name}.{lang}.{ext}"


def _attempt(owned_id: int, lang: str = "en") -> SubtitleAttempt | None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return s.query(SubtitleAttempt).filter_by(owned_file_id=owned_id, lang=lang).one_or_none()


def _last_run() -> JobRun:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return s.query(JobRun).order_by(JobRun.id.desc()).first()


def _set_attempt(owned_id: int, *, ago: timedelta, fail_count: int = 1, lang: str = "en") -> None:
    from homeTheater.db import session_scope

    with session_scope() as s:
        s.add(
            SubtitleAttempt(
                owned_file_id=owned_id,
                lang=lang,
                last_tried=datetime.now(UTC) - ago,
                fail_count=fail_count,
            )
        )


async def _sweep() -> Any:
    from homeTheater.config import get_config
    from homeTheater.subtitles import sweep_subtitles

    return await sweep_subtitles(get_config())


# --- S1: poison items, cooldown, ordering ------------------------------------


def _busy_write_for(monkeypatch: pytest.MonkeyPatch, poison: str) -> None:
    real = native.write_subtitle

    def write(dest: str, data: bytes) -> None:
        if poison in dest:
            raise OSError(errno.EBUSY, "Resource busy", dest + ".part")
        real(dest, data)

    monkeypatch.setattr(native, "write_subtitle", write)


async def test_poison_item_never_stops_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    poison = _add_file(tmp_path, "Dead Mans Wire")
    _add_file(tmp_path, "Healthy")
    _use_sources(monkeypatch, FakeSource())
    _busy_write_for(monkeypatch, "Dead Mans Wire")

    stats = await _sweep()

    assert stats.downloaded == 1 and stats.failed == 1
    assert _dest(tmp_path, "Healthy").exists()
    assert any("Dead Mans Wire" in e for e in stats.errors)
    attempt = _attempt(poison)
    assert attempt is not None and attempt.fail_count == 1 and attempt.last_outcome == "error"
    assert _last_run().status is RunStatus.partial


async def test_not_found_enters_cooldown_and_costs_no_budget_next_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Nothing")
    source = FakeSource(results=lambda q: [])
    _use_sources(monkeypatch, source)

    first = await _sweep()
    assert first.not_found == 1 and len(source.searched) == 1
    attempt = _attempt(owned)
    assert attempt is not None and attempt.fail_count == 1

    second = await _sweep()
    assert second.cooling_down == 1 and second.not_found == 0
    assert second.considered == 1 and second.capped == 0
    assert len(source.searched) == 1  # not searched again


def test_cooldown_doubles_up_to_a_ceiling() -> None:
    assert native.cooldown_for(1) == timedelta(days=1)
    assert native.cooldown_for(2) == timedelta(days=2)
    assert native.cooldown_for(4) == timedelta(days=8)
    assert native.cooldown_for(6) == timedelta(days=30)
    assert native.cooldown_for(500) == timedelta(days=30)


async def test_fail_count_climbs_after_the_cooldown_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Nothing")
    _set_attempt(owned, ago=timedelta(days=9), fail_count=4)  # 8-day cooldown is over
    _use_sources(monkeypatch, FakeSource(results=lambda q: []))

    await _sweep()

    attempt = _attempt(owned)
    assert attempt is not None and attempt.fail_count == 5


async def test_order_is_never_tried_newest_first_then_oldest_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    now = datetime.now(UTC)
    retry_recent = _add_file(tmp_path, "RetryRecent", created=now - timedelta(days=90))
    retry_oldest = _add_file(tmp_path, "RetryOldest", created=now - timedelta(days=80))
    _add_file(tmp_path, "FreshOld", created=now - timedelta(days=30))
    _add_file(tmp_path, "FreshNew", created=now - timedelta(days=1))
    cooling = _add_file(tmp_path, "Cooling", created=now)
    _set_attempt(retry_recent, ago=timedelta(days=2))
    _set_attempt(retry_oldest, ago=timedelta(days=20))
    _set_attempt(cooling, ago=timedelta(hours=1))
    source = FakeSource(results=lambda q: [])
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert [q.title for q in source.searched] == [
        "FreshNew",
        "FreshOld",
        "RetryOldest",
        "RetryRecent",
    ]
    assert stats.cooling_down == 1


async def test_budget_rotates_through_an_unmatchable_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch, extra="  max_searches_per_sweep: 2\n")
    for i in range(5):
        _add_file(tmp_path, f"Movie{i}")
    source = FakeSource(results=lambda q: [])
    _use_sources(monkeypatch, source)

    runs = [await _sweep() for _ in range(3)]

    assert [(r.not_found, r.capped) for r in runs] == [(2, 3), (2, 1), (1, 0)]
    assert sorted(q.title for q in source.searched) == [f"Movie{i}" for i in range(5)]


# --- S1: temp files ----------------------------------------------------------


def test_write_ignores_a_stale_busy_part(tmp_path: Path) -> None:
    from homeTheater.subtitles.native.placement import write_subtitle

    dest = tmp_path / "Subs" / "m.he.srt"
    dest.parent.mkdir()
    # A directory can be neither opened for writing nor unlinked: as stuck as it gets.
    Path(f"{dest}.part").mkdir()

    write_subtitle(str(dest), GOOD)

    assert dest.read_bytes() == GOOD


def test_write_removes_a_stale_part_when_it_can(tmp_path: Path) -> None:
    from homeTheater.subtitles.native.placement import write_subtitle

    dest = tmp_path / "Subs" / "m.he.srt"
    dest.parent.mkdir()
    Path(f"{dest}.part").write_bytes(b"old")

    write_subtitle(str(dest), GOOD)

    assert sorted(p.name for p in dest.parent.iterdir()) == ["m.he.srt"]


def test_write_cleans_up_its_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.subtitles.native import placement

    def boom(_src: str, _dst: str) -> None:
        raise OSError(errno.EBUSY, "busy")

    monkeypatch.setattr(placement.os, "replace", boom)
    dest = tmp_path / "Subs" / "m.he.srt"

    with pytest.raises(OSError, match="busy"):
        placement.write_subtitle(str(dest), GOOD)

    assert list(dest.parent.iterdir()) == []


# --- S2: honest run status ---------------------------------------------------


async def test_clean_sweep_with_nothing_found_is_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Nothing")
    _use_sources(monkeypatch, FakeSource(results=lambda q: []))

    await _sweep()

    assert _last_run().status is RunStatus.success


async def test_cancelled_sweep_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Slow")
    entered = asyncio.Event()

    class Hanging(FakeSource):
        async def search(self, query: Any) -> list[SubtitleResult]:
            entered.set()
            await asyncio.sleep(3600)
            return []

    _use_sources(monkeypatch, Hanging())
    task = asyncio.create_task(_sweep())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run = _last_run()
    assert run.status is RunStatus.failed and run.finished_at is not None
    assert "cancelled" in run.stats["errors"][0]


async def test_timed_out_sweep_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Slow")

    class Hanging(FakeSource):
        async def search(self, query: Any) -> list[SubtitleResult]:
            await asyncio.sleep(3600)
            return []

    _use_sources(monkeypatch, Hanging())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_sweep(), 0.2)

    assert _last_run().status is RunStatus.failed


async def test_all_sources_down_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "A")
    _add_file(tmp_path, "B")
    _use_sources(monkeypatch, FakeSource(search_error=SubtitleSourceError("down")))

    with pytest.raises(RuntimeError, match="sweep failed"):
        await _sweep()

    run = _last_run()
    assert run.status is RunStatus.failed and run.stats["failed"] == 2


async def test_some_errors_with_progress_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "A")
    _add_file(tmp_path, "B")

    def results(query: Any) -> list[SubtitleResult]:
        if query.title == "A":
            raise SubtitleSourceError("hiccup")
        return [_result("fake", "x")]

    _use_sources(monkeypatch, FakeSource(results=results))

    stats = await _sweep()

    assert stats.downloaded == 1 and stats.failed == 1
    assert _last_run().status is RunStatus.partial


async def test_unexpected_crash_is_failed_and_error_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)

    def boom(_config: Any, _now: Any) -> Any:
        raise OSError("connect https://x/?api_key=SECRET123 failed")

    monkeypatch.setattr(native, "_collect_work", boom)
    _use_sources(monkeypatch, FakeSource())

    with pytest.raises(RuntimeError):
        await _sweep()

    run = _last_run()
    assert run.status is RunStatus.failed
    assert "SECRET123" not in str(run.stats)


# --- S4: nothing blocking on the event loop ----------------------------------


async def test_nas_and_db_work_runs_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    _use_sources(monkeypatch, FakeSource())
    loop_thread = threading.current_thread()
    seen: dict[str, threading.Thread] = {}

    def spy(name: str) -> None:
        real = getattr(native, name)

        def wrapper(*args: Any, **kw: Any) -> Any:
            seen[name] = threading.current_thread()
            return real(*args, **kw)

        wrapper.__name__ = name
        monkeypatch.setattr(native, name, wrapper)

    for name in (
        "opensubtitles_hash",
        "media_present",
        "find_valid_subtitle",
        "write_subtitle",
        "library_mounted",
        "_collect_work",
        "_record",
    ):
        spy(name)

    stats = await _sweep()

    assert stats.downloaded == 1
    assert len(seen) == 7
    assert all(thread is not loop_thread for thread in seen.values()), seen


async def test_event_loop_stays_live_during_slow_nas_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    _use_sources(monkeypatch, FakeSource())
    release = threading.Event()
    entered = threading.Event()

    def slow_hash(_path: str) -> None:
        entered.set()
        release.wait(5)

    monkeypatch.setattr(native, "opensubtitles_hash", slow_hash)
    task = asyncio.create_task(_sweep())
    while not entered.is_set():
        await asyncio.sleep(0.01)  # only reachable if the loop isn't blocked
    release.set()
    assert (await task).downloaded == 1


async def test_wedged_nas_stops_the_sweep_instead_of_piling_up_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    for i in range(6):
        _add_file(tmp_path, f"M{i}")
    source = FakeSource()
    _use_sources(monkeypatch, source)
    monkeypatch.setattr(native, "NAS_TIMEOUT_S", 0.05)
    release = threading.Event()
    calls: list[str] = []

    def wedged(path: str) -> None:
        calls.append(path)
        release.wait(5)

    monkeypatch.setattr(native, "opensubtitles_hash", wedged)
    try:
        with pytest.raises(RuntimeError, match="sweep failed"):
            await _sweep()
    finally:
        release.set()

    assert len(calls) == native.MAX_NAS_TIMEOUTS
    assert source.searched == []
    run = _last_run()
    assert run.status is RunStatus.failed and run.stats["failed"] == native.MAX_NAS_TIMEOUTS
    from homeTheater.db import session_scope

    with session_scope() as s:
        assert s.query(SubtitleAttempt).count() == native.MAX_NAS_TIMEOUTS


# --- S5: mount, absent media, adoption ---------------------------------------


async def test_unmounted_library_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "stub").mkdir()  # the bare directory a dropped mount leaves behind
    _setup(tmp_path, monkeypatch, extra=f"  library_base_dir: {tmp_path / 'stub'}\n")
    owned = _add_file(tmp_path, "Movie")
    source = FakeSource()
    _use_sources(monkeypatch, source)

    with pytest.raises(RuntimeError, match="not mounted"):
        await _sweep()

    assert source.searched == []
    assert _attempt(owned) is None  # the file isn't to blame
    run = _last_run()
    assert run.status is RunStatus.failed and "not mounted" in run.stats["errors"][0]


async def test_mounted_library_is_swept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.subtitles.native import placement

    _setup(tmp_path, monkeypatch, extra=f"  library_base_dir: {tmp_path}\n")
    _add_file(tmp_path, "Movie")
    monkeypatch.setattr(placement.os.path, "ismount", lambda p: p == str(tmp_path))
    _use_sources(monkeypatch, FakeSource())

    assert (await _sweep()).downloaded == 1


async def test_mount_lost_mid_sweep_stops_rather_than_marking_everything_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.subtitles.native import placement

    _setup(tmp_path, monkeypatch, extra=f"  library_base_dir: {tmp_path}\n")
    now = datetime.now(UTC)
    _add_file(tmp_path, "Here", created=now)
    _add_file(tmp_path, "Gone", created=now - timedelta(days=1), present=False)
    _add_file(tmp_path, "Later", created=now - timedelta(days=2))
    answers = iter([True, False])
    monkeypatch.setattr(placement.os.path, "ismount", lambda _p: next(answers))
    source = FakeSource()
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert stats.downloaded == 1 and stats.absent == 0
    assert [q.title for q in source.searched] == ["Here"]
    run = _last_run()
    assert run.status is RunStatus.partial and "mount lost" in str(run.stats["errors"])


async def test_absent_media_costs_no_provider_call_or_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch, extra="  max_searches_per_sweep: 1\n")
    now = datetime.now(UTC)
    _add_file(tmp_path, "Gone", created=now, present=False)
    _add_file(tmp_path, "Here", created=now - timedelta(days=1))
    source = FakeSource()
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert stats.absent == 1 and stats.downloaded == 1
    assert [q.title for q in source.searched] == ["Here"]
    assert _last_run().status is RunStatus.success


async def test_existing_dest_is_adopted_without_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    dest = _dest(tmp_path, "Movie")
    dest.parent.mkdir()
    dest.write_bytes(GOOD)
    source = FakeSource()
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert stats.adopted == 1 and stats.downloaded == 0 and source.searched == []
    with session_scope() as s:
        row = s.query(Subtitle).one()
        assert row.status == "adopted" and row.path == str(dest)
        assert s.get(OwnedFile, owned).subtitle_langs == ["en"]


async def test_garbage_dest_is_replaced_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    dest = _dest(tmp_path, "Movie")
    dest.parent.mkdir()
    dest.write_bytes(HTML)
    _use_sources(monkeypatch, FakeSource())

    stats = await _sweep()

    assert stats.adopted == 0 and stats.downloaded == 1
    assert dest.read_bytes() == GOOD


async def test_nothing_is_hashed_when_a_sub_can_be_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    dest = _dest(tmp_path, "Movie")
    dest.parent.mkdir()
    dest.write_bytes(GOOD)
    _use_sources(monkeypatch, FakeSource())

    def no_hash(_path: str) -> None:
        raise AssertionError("hashed a file that needed no search")

    monkeypatch.setattr(native, "opensubtitles_hash", no_hash)

    assert (await _sweep()).adopted == 1


# --- S6: validation in the sweep ---------------------------------------------


async def test_invalid_payload_tries_the_next_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    source = FakeSource(
        results=lambda q: [_result("fake", "html", 9), _result("fake", "good", 1)],
        payloads={"html": HTML},
    )
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert source.downloaded == ["html", "good"]
    assert stats.downloaded == 1 and _dest(tmp_path, "Movie").read_bytes() == GOOD


async def test_invalid_payload_falls_through_to_the_next_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    bad = FakeSource("bad", payloads={"only": HTML})
    good = FakeSource("good")
    _use_sources(monkeypatch, bad, good)

    assert (await _sweep()).downloaded == 1
    assert good.downloaded == ["only"]


async def test_only_invalid_payloads_is_a_failed_attempt_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    _use_sources(monkeypatch, FakeSource(payloads={"only": HTML}))

    stats = await _sweep()

    assert stats.invalid == 1 and stats.downloaded == 0
    assert not _dest(tmp_path, "Movie").exists()
    attempt = _attempt(owned)
    assert attempt is not None and attempt.last_outcome == "invalid"


async def test_candidates_per_source_are_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    names = [f"c{i}" for i in range(6)]
    source = FakeSource(
        results=lambda q: [_result("fake", n) for n in names],
        payloads=dict.fromkeys(names, HTML),
    )
    _use_sources(monkeypatch, source)

    await _sweep()

    assert len(source.downloaded) == native.MAX_CANDIDATES_PER_SOURCE


async def test_ass_keeps_its_extension_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    ass = b"[Script Info]\nScriptType: v4.00+\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,hi\n"
    _use_sources(monkeypatch, FakeSource(payloads={"only": ass}))

    await _sweep()

    assert _dest(tmp_path, "Movie", ext="ass").read_bytes() == ass
    assert not _dest(tmp_path, "Movie").exists()


async def test_cp1255_payload_lands_on_disk_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch, languages="[he]")
    _add_file(tmp_path, "Movie")
    payload = "1\n00:00:01,000 --> 00:00:02,000\nשלום\n".encode("cp1255")
    _use_sources(monkeypatch, FakeSource(payloads={"only": payload}))

    await _sweep()

    assert "שלום" in _dest(tmp_path, "Movie", "he").read_text("utf-8")


# --- S8: selection -----------------------------------------------------------


async def test_release_match_decides_which_candidate_is_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie.2020.1080p.BluRay.x264-SPARKS")
    source = FakeSource(
        results=lambda q: [
            _result("fake", "Movie.2020.720p.WEBRip-OTHER", 9000),
            _result("fake", "Movie.2020.1080p.BluRay.x264-SPARKS", 1),
        ]
    )
    _use_sources(monkeypatch, source)

    await _sweep()

    assert source.downloaded == ["Movie.2020.1080p.BluRay.x264-SPARKS"]


async def test_hearing_impaired_setting_reaches_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch, extra="  hearing_impaired: true\n")
    _add_file(tmp_path, "Movie")
    source = FakeSource(
        results=lambda q: [
            _result("fake", "plain", 100),
            _result("fake", "captions", 1, hearing_impaired=True),
        ]
    )
    _use_sources(monkeypatch, source)

    await _sweep()

    assert source.downloaded == ["captions"]


# --- S11 / S12: configuration, exclusion, idempotent records -----------------


async def test_no_sources_raises_not_configured_and_records_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENSUBTITLES_API_KEY")
    from homeTheater.config import loader

    loader.get_config.cache_clear()

    with pytest.raises(NotConfiguredError):
        await _sweep()

    with session_scope() as s:
        assert s.query(JobRun).count() == 0


async def test_unc_paths_without_a_mount_are_not_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    with session_scope() as s:
        title = Title(title="X", year=2020, kind=TitleKind.movie)
        s.add(title)
        s.flush()
        s.add(OwnedFile(title_id=title.id, path="\\\\h\\s\\Movies\\x.mkv", kind=TitleKind.movie))
    source = FakeSource()
    _use_sources(monkeypatch, source)

    with pytest.raises(NotConfiguredError, match="library_base_dir"):
        await _sweep()

    assert source.searched == []
    with session_scope() as s:
        assert s.query(JobRun).count() == 0


async def test_sweep_holds_the_job_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import session_scope
    from homeTheater.locks import job_lock
    from homeTheater.subtitles.service import SWEEP_LOCK

    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    source = FakeSource()
    _use_sources(monkeypatch, source)

    with job_lock(get_config(), SWEEP_LOCK), pytest.raises(JobBusyError):
        await _sweep()

    assert source.searched == []
    with session_scope() as s:
        assert s.query(JobRun).count() == 0
    assert (await _sweep()).downloaded == 1  # released; the next one runs


def test_record_is_idempotent_and_clears_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    _set_attempt(owned, ago=timedelta(days=5))
    with session_scope() as s:
        title_id = s.get(OwnedFile, owned).title_id
    item = native._Work(
        owned, title_id, "en", "/x.mkv", TitleKind.movie, "Movie", 2020, None, None, None
    )

    native._record(item, "ktuvit", "/a.srt")
    native._record(item, "opensubtitles", "/b.srt")

    with session_scope() as s:
        row = s.query(Subtitle).one()
        assert row.provider == "opensubtitles" and row.path == "/b.srt"
    assert _attempt(owned) is None


def test_duplicate_subtitle_rows_are_refused_by_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError

    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    with session_scope() as s:
        title_id = s.get(OwnedFile, owned).title_id
        s.add(Subtitle(title_id=title_id, owned_file_id=owned, lang="en"))
    with pytest.raises(IntegrityError), session_scope() as s:
        s.add(Subtitle(title_id=title_id, owned_file_id=owned, lang="en"))
    with session_scope() as s:  # unlinked rows don't collide
        s.add(Subtitle(title_id=title_id, owned_file_id=None, lang="en"))
        s.add(Subtitle(title_id=title_id, owned_file_id=None, lang="en"))


def test_pruning_an_owned_file_removes_its_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import delete

    from homeTheater.db import session_scope

    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    _set_attempt(owned, ago=timedelta(days=1))

    with session_scope() as s:  # what the scanner's prune does, foreign keys on
        s.execute(delete(OwnedFile).where(OwnedFile.id == owned))

    assert _attempt(owned) is None


# --- S14: circuit breaker ----------------------------------------------------


async def test_source_is_disabled_after_consecutive_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    for i in range(6):
        _add_file(tmp_path, f"M{i}")
    flaky = FakeSource("flaky", search_error=SubtitleSourceError("500"))
    backup = FakeSource("backup", results=lambda q: [])
    _use_sources(monkeypatch, flaky, backup)

    stats = await _sweep()

    assert len(flaky.searched) == native.MAX_SOURCE_FAILURES
    assert len(backup.searched) == 6
    assert list(stats.disabled_sources) == ["flaky"]
    assert _last_run().status is RunStatus.partial


async def test_a_success_resets_the_failure_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    for i in range(5):
        _add_file(tmp_path, f"M{i}")
    calls: list[int] = []

    def alternate(_query: Any) -> list[SubtitleResult]:
        calls.append(1)
        if len(calls) % 2:
            raise SubtitleSourceError("flaky")
        return []

    source = FakeSource(results=alternate)
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert stats.disabled_sources == {} and len(source.searched) == 5


@pytest.mark.parametrize("error", [SourceAuthError("bad key"), SourceQuotaError("spent")])
async def test_auth_or_quota_disables_a_source_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    _setup(tmp_path, monkeypatch)
    for i in range(4):
        _add_file(tmp_path, f"M{i}")
    dead = FakeSource("dead", search_error=error)
    backup = FakeSource("backup", results=lambda q: [])
    _use_sources(monkeypatch, dead, backup)

    stats = await _sweep()

    assert len(dead.searched) == 1 and len(backup.searched) == 4
    assert "dead" in stats.disabled_sources


async def test_a_spent_quota_with_other_sources_working_is_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "A")
    spent = FakeSource("spent", search_error=SourceQuotaError("20/day used"))
    backup = FakeSource("backup", results=lambda q: [])
    _use_sources(monkeypatch, spent, backup)

    await _sweep()

    assert _last_run().status is RunStatus.success


async def test_sole_source_out_of_quota_does_not_burn_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    ids = [_add_file(tmp_path, f"M{i}") for i in range(3)]
    source = FakeSource(search_error=SourceQuotaError("spent"))
    _use_sources(monkeypatch, source)

    with pytest.raises(RuntimeError):
        await _sweep()

    run = _last_run()
    assert len(source.searched) == 1
    assert run.status is RunStatus.failed and run.stats["source_down"] == 2
    assert sum(_attempt(i) is not None for i in ids) == 1  # the rest aren't penalised


async def test_download_failure_is_an_error_not_no_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    _add_file(tmp_path, "Movie")
    _use_sources(monkeypatch, FakeSource(payloads={"only": SubtitleSourceError("reset")}))

    with pytest.raises(RuntimeError):
        await _sweep()

    run = _last_run()
    assert run.stats["not_found"] == 0 and run.stats["failed"] == 1


async def test_language_no_source_serves_costs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch, languages="[fr]")
    owned = _add_file(tmp_path, "Movie")
    source = FakeSource()
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert stats.unsupported == 1 and source.searched == [] and _attempt(owned) is None


# --- S20: NULL season/episode ------------------------------------------------


async def test_series_file_without_numbers_is_skipped_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch, extra="  max_searches_per_sweep: 1\n")
    now = datetime.now(UTC)
    _add_file(tmp_path, "Extra", created=now, kind=TitleKind.series, season=1)
    _add_file(tmp_path, "Movie", created=now - timedelta(days=1))
    source = FakeSource()
    _use_sources(monkeypatch, source)

    stats = await _sweep()

    assert stats.unidentified == 1 and stats.downloaded == 1
    assert [q.title for q in source.searched] == ["Movie"]


# --- fetch_for_owned_file ----------------------------------------------------


async def test_import_fetch_validates_and_notes_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config

    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    _use_sources(monkeypatch, FakeSource(payloads={"only": HTML}))

    assert await native.fetch_for_owned_file(get_config(), owned) == []

    attempt = _attempt(owned)
    assert attempt is not None and attempt.fail_count == 1
    assert not _dest(tmp_path, "Movie").exists()


async def test_import_fetch_writes_and_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config

    _setup(tmp_path, monkeypatch)
    owned = _add_file(tmp_path, "Movie")
    _use_sources(monkeypatch, FakeSource())

    assert await native.fetch_for_owned_file(get_config(), owned) == [str(_dest(tmp_path, "Movie"))]


async def test_import_fetch_skips_an_unmounted_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config

    _setup(tmp_path, monkeypatch, extra=f"  library_base_dir: {tmp_path / 'nope'}\n")
    owned = _add_file(tmp_path, "Movie")
    source = FakeSource()
    _use_sources(monkeypatch, source)

    assert await native.fetch_for_owned_file(get_config(), owned) == []
    assert source.searched == []
