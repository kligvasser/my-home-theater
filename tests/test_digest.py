"""F2: the morning digest and first-failure job alerts. No test reaches Telegram."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

URL = "https://api.telegram.org/bot123/sendMessage"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def _enable(config_file: Path, monkeypatch: pytest.MonkeyPatch, telegram: bool = True) -> Any:
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    with config_file.open("a") as f:
        f.write("digest:\n  enabled: true\n  hour: 8\n")
    if telegram:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    get_config.cache_clear()
    init_db()
    return get_config()


def _at(hour: int, day: int = 7) -> datetime:
    """A moment at a local wall-clock hour, whatever zone the tests run in."""

    return datetime(2026, 11, day, hour, 30).astimezone()


def _text(route: respx.Route, call: int = -1) -> str:
    return json.loads(route.calls[call].request.content)["text"]


def _import(name: str, at: datetime, season: int | None = None, year: int | None = 2020) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        CandidateStatus,
        Download,
        Title,
        TitleKind,
    )

    with session_scope() as s:
        title = Title(title=name, kind=TitleKind.movie, year=year)
        s.add(title)
        s.flush()
        cand = Candidate(
            title_id=title.id,
            season=season,
            source=CandidateSource.manual,
            status=CandidateStatus.imported,
        )
        s.add(cand)
        s.flush()
        s.add(Download(candidate_id=cand.id, state="imported", completed_at=at))


def test_digest_day_turns_over_at_the_configured_hour() -> None:
    from homeTheater.digest import digest_day

    assert digest_day(datetime(2026, 11, 7, 8, 0), 8) == "2026-11-07"
    assert digest_day(datetime(2026, 11, 7, 7, 59), 8) == "2026-11-06"


def test_digest_is_off_by_default(config_file: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.digest import digest_due

    cfg = get_config()
    assert cfg.digest.enabled is False and cfg.digest.hour == 8
    assert digest_due(cfg) is False


def test_digest_hour_is_validated() -> None:
    from homeTheater.config.settings import Digest

    with pytest.raises(ValueError):
        Digest(hour=24)


@respx.mock
async def test_second_send_for_the_same_day_does_nothing(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.digest import digest_due, send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    assert await send_digest(cfg, _at(9)) is True
    assert digest_due(cfg, _at(9)) is False
    assert await send_digest(cfg, _at(23)) is False
    assert route.call_count == 1
    assert await send_digest(cfg, _at(9, day=8)) is True  # the next day has its own
    assert route.call_count == 2


@respx.mock
async def test_digest_missed_overnight_is_due_on_wake_once(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slept through 08:00 on the 7th, woke at 03:00 on the 8th: the 7th's digest is
    still owed, and 08:00 on the 8th brings the 8th's, not a repeat."""

    from homeTheater.digest import digest_due, send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await send_digest(cfg, _at(9, day=6))

    assert digest_due(cfg, _at(3, day=8)) is True
    await send_digest(cfg, _at(3, day=8))
    assert digest_due(cfg, _at(4, day=8)) is False
    assert route.call_count == 2


def test_catch_up_lists_a_due_digest_and_backs_off_after_an_attempt(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import catchup, jobs

    cfg = _enable(config_file, monkeypatch)
    assert "digest" in catchup.due_jobs(cfg, NOW)
    jobs.last_attempts["digest"] = NOW.timestamp()
    assert "digest" not in catchup.due_jobs(cfg, NOW + timedelta(minutes=5))
    assert "digest" in catchup.due_jobs(cfg, NOW + timedelta(minutes=31))


def test_catch_up_skips_a_digest_that_went_out(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.digest import _write_state, digest_day
    from homeTheater.scheduler import catchup

    cfg = _enable(config_file, monkeypatch)
    day = digest_day(NOW.astimezone(), cfg.digest.hour)
    _write_state(json.dumps({"day": day, "sent_at": NOW.isoformat()}))
    assert "digest" not in catchup.due_jobs(cfg, NOW)


def test_catch_up_ignores_the_digest_without_telegram(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import catchup

    cfg = _enable(config_file, monkeypatch, telegram=False)
    assert "digest" not in catchup.due_jobs(cfg, NOW)


async def test_catch_up_pulls_the_digest_job_forward(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from homeTheater.scheduler import catchup

    cfg = _enable(config_file, monkeypatch)
    sched = AsyncIOScheduler()
    catchup.register_clock_jobs(sched, cfg)
    sched.start(paused=True)
    try:
        trigger = sched.get_job("digest").trigger
        assert "hour='8'" in str(trigger)
        fired = await catchup.catch_up(sched, cfg, "wake")
        assert "digest" in fired
        pulled = sched.get_job("digest").next_run_time
        assert pulled <= datetime.now(sched.timezone)
    finally:
        sched.shutdown(wait=False)


async def test_no_digest_job_when_disabled(config_file: Path) -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from homeTheater.config import get_config
    from homeTheater.scheduler import catchup

    sched = AsyncIOScheduler()
    catchup.register_clock_jobs(sched, get_config())
    assert sched.get_job("digest") is None


async def test_digest_job_skips_quietly_without_telegram(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import JobRun
    from homeTheater.scheduler import jobs

    cfg = _enable(config_file, monkeypatch, telegram=False)
    sent: list[str] = []

    async def fake_notify(config: Any, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)
    await jobs.run_digest_job(cfg)
    assert sent == []
    assert "Telegram" in jobs.skip_reasons["digest"]
    with session_scope() as s:
        assert s.scalars(select(JobRun)).first() is None


@respx.mock
async def test_digest_job_sends_exactly_one_message(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler import jobs

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await jobs.run_digest_job(cfg)
    await jobs.run_digest_job(cfg)  # the wake catch-up landing on the cron fire
    assert route.call_count == 1


@respx.mock
async def test_a_definite_failure_leaves_the_digest_owed(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.digest import digest_due, send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(RuntimeError):
        await send_digest(cfg, _at(9))
    assert digest_due(cfg, _at(10)) is True

    route.mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await send_digest(cfg, _at(10)) is True


@respx.mock
async def test_an_unknown_delivery_is_never_resent(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read timeout may be a digest that arrived: never twice beats never missed."""

    from homeTheater.digest import digest_due, send_digest

    cfg = _enable(config_file, monkeypatch)
    respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        await send_digest(cfg, _at(9))
    assert digest_due(cfg, _at(10)) is False


@respx.mock
async def test_titles_are_escaped_for_html_mode(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.digest import send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    now = _at(9)
    _import("Tom & Jerry <3>", now - timedelta(hours=1))
    await send_digest(cfg, now)

    body = json.loads(route.calls[0].request.content)
    assert body["parse_mode"] == "HTML"
    assert "Tom &amp; Jerry &lt;3&gt; (2020)" in body["text"]
    assert "<3" not in body["text"]


@respx.mock
async def test_digest_reports_subtitles_failures_attention_and_nas(
    config_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import (
        MARKER_REMOVED,
        Candidate,
        CandidateSource,
        CandidateStatus,
        Download,
        JobRun,
        RunStatus,
        Subtitle,
        Title,
        TitleKind,
    )
    from homeTheater.digest import send_digest

    cfg = _enable(config_file, monkeypatch)
    cfg.torrent.library_base_dir = str(tmp_path)
    cfg.acquisition.backend = "torrent"
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    now = _at(9)
    with session_scope() as s:
        title = Title(title="Has Subs", kind=TitleKind.movie, year=2001)
        s.add(title)
        s.flush()
        s.add(Subtitle(title_id=title.id, lang="he", status="downloaded", created_at=now))
        s.add(Subtitle(title_id=title.id, lang="en", status="adopted", created_at=now))
        s.add(
            JobRun(
                kind="scan",
                started_at=now - timedelta(hours=2),
                status=RunStatus.failed,
                stats={"errors": ["Movies: <refused>"]},
            )
        )
        cand = Candidate(
            title_id=title.id, source=CandidateSource.manual, status=CandidateStatus.failed
        )
        s.add(cand)
        s.flush()
        for error in ("import failed: disk", f"{MARKER_REMOVED} junk"):
            s.add(Download(candidate_id=cand.id, state="failed", error=error, updated_at=now))

    await send_digest(cfg, now + timedelta(minutes=1))
    text = _text(route)
    assert "Has Subs (2001): he" in text and "en" not in text.split("Has Subs (2001): he")[1][:4]
    assert "scan job, 1 run: Movies: &lt;refused&gt;" in text
    assert "import failed: disk" in text and "junk" not in text
    assert "Needs attention (" in text and "Failed: Has Subs (2001)" in text
    assert re.search(r"<b>NAS</b>: .* free of ", text)


@respx.mock
async def test_digest_goes_out_even_when_the_nas_cannot_be_read(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.digest import send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    await send_digest(cfg, _at(9))
    assert "free space unknown" in _text(route)
    assert "quiet night" in _text(route)


@respx.mock
async def test_each_digest_covers_only_what_followed_the_last(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.digest import send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    first = _at(9, day=6)
    _import("Old Arrival", first - timedelta(hours=1))
    await send_digest(cfg, first)
    # Three days asleep: everything since the last digest, one message.
    later = _at(9, day=9)
    _import("New Arrival", later - timedelta(days=2))
    await send_digest(cfg, later)

    assert "Old Arrival" in _text(route, 0)
    assert "New Arrival" in _text(route, 1) and "Old Arrival" not in _text(route, 1)


@respx.mock
async def test_a_season_arrives_as_one_line(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate, Download
    from homeTheater.digest import send_digest

    cfg = _enable(config_file, monkeypatch)
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    now = _at(9)
    _import("Show", now - timedelta(hours=1), season=2, year=None)
    with session_scope() as s:
        cand = s.scalars(select(Candidate)).one()
        s.add(
            Download(candidate_id=cand.id, state="imported", completed_at=now - timedelta(hours=1))
        )
        s.add(
            Download(
                candidate_id=cand.id,
                external_id="x",
                state="imported",
                completed_at=now - timedelta(hours=1),
            )
        )
    await send_digest(cfg, now)
    assert "Show S02 ×3" in _text(route)


def test_a_long_digest_fits_telegram_with_whole_lines() -> None:
    from homeTheater.digest import MAX_MESSAGE, Section, _capped, _esc, _label, render

    lines = [_label("&" * 80 + f" {i}", 2000) for i in range(30)]
    arrived = _capped("Arrived", lines * 5)
    assert arrived.more == 140
    sections = [arrived, Section("Failed", [_esc("<" * 900) for _ in range(9)])]
    text = render("head", sections, "<b>NAS</b>: ok")

    assert len(text) <= MAX_MESSAGE
    assert "…and" in text and text.endswith("<b>NAS</b>: ok")
    assert not re.search(r"&[a-z]*(?:\n|$)", text)  # no entity cut in half
    assert text.count("<b>") == text.count("</b>")


# --- first-failure alerts ---------------------------------------------------------


async def _run(jobs: Any, cfg: Any, outcomes: list[Any], name: str = "discovery") -> None:
    for outcome in outcomes:

        async def body(result: Any = outcome) -> str | None:
            if isinstance(result, Exception):
                raise result
            return result

        await jobs._guarded(name, cfg, body)


def _sent(monkeypatch: pytest.MonkeyPatch, result: bool = True) -> list[str]:
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: Any, text: str) -> bool:
        sent.append(text)
        return result

    monkeypatch.setattr(jobs, "notify", fake_notify)
    return sent


async def test_only_the_first_failure_alerts_and_recovery_is_announced_once(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)
    await _run(jobs, get_config(), [RuntimeError("a"), RuntimeError("b"), RuntimeError("c")])
    assert len(sent) == 1 and "a" in sent[0]

    await _run(jobs, get_config(), [None, None])
    assert len(sent) == 2
    assert sent[1] == "✅ discovery job recovered after 3 failed runs"

    await _run(jobs, get_config(), [RuntimeError("again")])
    assert len(sent) == 3 and "again" in sent[2]  # a new outage alerts again


async def test_recovery_and_a_success_message_are_one_message(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)
    await _run(jobs, get_config(), [RuntimeError("a"), "🍿 2 new candidate(s)"])
    assert len(sent) == 2
    assert "recovered after 1 failed run\n" in sent[1] and "🍿" in sent[1]


async def test_routine_success_messages_still_go_out(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)
    await _run(jobs, get_config(), ["🍿 1 new candidate(s)"])
    assert sent == ["🍿 1 new candidate(s)"]


async def test_a_skip_is_not_a_recovery(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.errors import JobBusyError
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)
    await _run(jobs, get_config(), [RuntimeError("a"), JobBusyError("busy")])
    assert len(sent) == 1
    await _run(jobs, get_config(), [None])
    assert "recovered after 1 failed run" in sent[1]  # the skip wasn't counted


async def test_failing_jobs_are_tracked_separately(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)
    await _run(jobs, get_config(), [RuntimeError("a")], name="scan")
    await _run(jobs, get_config(), [RuntimeError("b")], name="sync")
    assert len(sent) == 2


async def test_an_alert_lost_to_an_outage_is_owed(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch, result=False)
    await _run(jobs, get_config(), [RuntimeError("a"), RuntimeError("b")])
    assert len(sent) == 2  # neither got through, so each tried

    delivered = _sent(monkeypatch, result=True)
    await _run(jobs, get_config(), [RuntimeError("c"), RuntimeError("d")])
    assert len(delivered) == 1


async def test_nobody_is_told_of_recovery_from_a_failure_they_never_heard_of(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    _sent(monkeypatch, result=False)
    await _run(jobs, get_config(), [RuntimeError("a")])
    sent = _sent(monkeypatch, result=True)
    await _run(jobs, get_config(), [None])
    assert sent == []


async def test_alert_state_survives_a_restart(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launchd restarts the app; a job still failing must not alert all over again."""

    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)
    await _run(jobs, get_config(), [RuntimeError("a")])
    db_session._engine = None
    db_session._SessionFactory = None
    await _run(jobs, get_config(), [RuntimeError("b"), None])
    assert len(sent) == 2 and "recovered after 2 failed runs" in sent[1]


async def test_a_refused_scan_alerts_once(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal repeats every scan; the first-failure rule is what stops the noise."""

    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.scheduler import jobs

    init_db()
    sent = _sent(monkeypatch)

    def refused(config: Any, **kwargs: Any) -> None:
        raise RuntimeError("Scan failed: refusing to prune 6 of 20")

    monkeypatch.setattr("homeTheater.scanner.run_scan", refused)
    await jobs.run_scan_job(get_config())
    await jobs.run_scan_job(get_config())
    assert len(sent) == 1


async def test_notify_reports_whether_it_went_out(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.notifications import notify

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    get_config.cache_clear()
    with respx.mock:
        respx.post(URL).mock(return_value=httpx.Response(500))
        assert await notify(get_config(), "x") is False
    with respx.mock:
        route = respx.post(URL).mock(return_value=httpx.Response(200))
        assert await notify(get_config(), "a < b") is True
    # Plain messages carry no parse mode, so they're sent as they are.
    assert json.loads(route.calls[0].request.content) == {"chat_id": "chat", "text": "a < b"}


@respx.mock
async def test_a_huge_alert_is_cut_rather_than_refused() -> None:
    """Telegram rejects over 4096 characters, and a failed scan's message lists every
    error: the alert would be lost, and owed again on every failure."""

    from homeTheater.notifications import TelegramNotifier

    route = respx.post(URL).mock(return_value=httpx.Response(200))
    async with httpx.AsyncClient() as http:
        await TelegramNotifier("123", "chat", http).send("x" * 9000)
    assert len(_text(route)) == 4000
