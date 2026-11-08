"""The morning digest: one Telegram message a day, however the Mac slept.

A digest belongs to a *digest day*, which starts at ``digest.hour``: wake at 03:00
having missed yesterday's and you get yesterday's, then today's at the usual hour.
The cron fire, a misfire replay on wake and catch-up can all reach :func:`send_digest`;
the record in the ``setting`` table lets only the first through, restarts included.

That record is written *before* the send. A crash mid-send then costs a digest rather
than doubling one, and the same goes for a timeout after the request went out, when
nobody knows whether it arrived. A failure that certainly delivered nothing puts the
record back, so catch-up tries again.

Each digest covers everything since the last one, so a long sleep loses nothing and
nothing is reported twice.

The text uses Telegram's HTML mode, so every value from the catalog goes through
:func:`_esc`. Telegram refuses anything over 4096 characters, so sections are trimmed
to fit whole lines at a time: a cut inside a tag or an entity would fail the send too.
"""

from __future__ import annotations

import asyncio
import html
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from .config import AppConfig
from .dashboard import human_size
from .db.base import utcnow
from .db.models import (
    MARKER_REMOVED,
    Candidate,
    Download,
    JobRun,
    RunStatus,
    Setting,
    Subtitle,
    Title,
)
from .db.session import session_scope
from .errors import NotConfiguredError, redact_exc
from .locks import job_lock
from .logging_setup import get_logger
from .notifications import send_telegram, telegram_configured

log = get_logger(__name__)

STATE_KEY = "digest_state"
PARSE_MODE = "HTML"
MAX_MESSAGE = 4000  # Telegram's limit is 4096; escapes and emoji make that tight
MAX_LINES = 10
MAX_TEXT = 160

# The request may have reached Telegram: sending again could double the digest.
_DELIVERY_UNKNOWN = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def _esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def _clip(text: str, limit: int = MAX_TEXT) -> str:
    """Shorten raw text. Before escaping, so an entity can't be cut in half."""

    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def digest_day(moment: datetime, hour: int) -> str:
    """The digest day a local moment falls in."""

    day = moment.date() if moment.hour >= hour else moment.date() - timedelta(days=1)
    return day.isoformat()


def _read_state() -> str | None:
    with session_scope() as s:
        row = s.get(Setting, STATE_KEY)
        return row.value if row is not None else None


def _write_state(value: str | None) -> None:
    with session_scope() as s:
        row = s.get(Setting, STATE_KEY)
        if row is None:
            row = Setting(key=STATE_KEY)
            s.add(row)
        row.value = value


def _parse(raw: str | None) -> dict[str, Any]:
    try:
        state = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return state if isinstance(state, dict) else {}


def digest_due(config: AppConfig, now: datetime | None = None) -> bool:
    """Whether the current digest day's digest is still to go out. Synchronous."""

    if not config.digest.enabled or not telegram_configured(config):
        return False
    local = (now or utcnow()).astimezone()
    return _parse(_read_state()).get("day") != digest_day(local, config.digest.hour)


@dataclass(slots=True)
class Section:
    heading: str
    lines: list[str] = field(default_factory=list)
    more: int = 0  # lines dropped

    def text(self) -> str:
        rows = [f"<b>{_esc(self.heading)}</b>", *(f"• {line}" for line in self.lines)]
        if self.more:
            rows.append(f"…and {self.more} more")
        return "\n".join(rows)


def _capped(heading: str, lines: list[str]) -> Section:
    return Section(heading, lines[:MAX_LINES], max(len(lines) - MAX_LINES, 0))


def render(header: str, sections: list[Section], footer: str) -> str:
    """The message, trimmed by whole lines from the longest section until it fits."""

    def body() -> str:
        parts = [header, *(s.text() for s in sections if s.lines or s.more), footer]
        return "\n\n".join(p for p in parts if p)

    while len(body()) > MAX_MESSAGE:
        longest = max((s for s in sections if s.lines), key=lambda s: len(s.text()), default=None)
        if longest is None:
            break
        longest.lines.pop()
        longest.more += 1
    return body()


def _label(name: str, year: int | None, season: int | None = None) -> str:
    label = _esc(_clip(name, 80))
    if year:
        label += f" ({year})"
    return label if season is None else f"{label} S{season:02d}"


def _arrived(since: datetime, until: datetime) -> list[str]:
    with session_scope() as s:
        rows = s.execute(
            select(Title.id, Title.title, Title.year, Candidate.season)
            .join(Candidate, Candidate.title_id == Title.id)
            .join(Download, Download.candidate_id == Candidate.id)
            .where(
                Download.state == "imported",
                Download.completed_at >= since,
                Download.completed_at < until,
            )
            .order_by(Download.completed_at)
        ).all()
    counts: dict[tuple[int, int | None], tuple[str, int]] = {}
    for title_id, name, year, season in rows:
        key = (title_id, season)
        label = _label(name, year, season)
        counts[key] = (label, counts[key][1] + 1 if key in counts else 1)
    return [f"{label} ×{n}" if n > 1 else label for label, n in counts.values()]


def _subtitles(since: datetime, until: datetime) -> list[str]:
    with session_scope() as s:
        rows = s.execute(
            select(Title.id, Title.title, Title.year, Subtitle.lang)
            .join(Title, Title.id == Subtitle.title_id)
            .where(
                Subtitle.status == "downloaded",
                Subtitle.created_at >= since,
                Subtitle.created_at < until,
            )
        ).all()
    langs: dict[int, set[str]] = defaultdict(set)
    names: dict[int, str] = {}
    for title_id, name, year, lang in rows:
        names[title_id] = _label(name, year)
        langs[title_id].add(lang)
    return [f"{names[t]}: {_esc(', '.join(sorted(langs[t])))}" for t in names]


def _failures(since: datetime, until: datetime) -> list[str]:
    with session_scope() as s:
        runs = s.execute(
            select(JobRun.kind, JobRun.stats)
            .where(
                JobRun.status == RunStatus.failed,
                JobRun.started_at >= since,
                JobRun.started_at < until,
            )
            .order_by(JobRun.started_at)
        ).all()
        downloads = s.execute(
            select(Title.title, Title.year, Candidate.season, Download.error)
            .join(Candidate, Candidate.id == Download.candidate_id)
            .join(Title, Title.id == Candidate.title_id)
            .where(
                Download.state == "failed",
                Download.updated_at >= since,
                Download.updated_at < until,
            )
        ).all()
    jobs: dict[str, list[str]] = defaultdict(list)
    for kind, stats in runs:
        stats = stats or {}
        errors = stats.get("errors") or [stats.get("error") or "failed"]
        jobs[kind].append(str(errors[-1]))
    lines = [
        f"{_esc(kind)} job, {len(errs)} run{'' if len(errs) == 1 else 's'}: {_esc(_clip(errs[-1]))}"
        for kind, errs in jobs.items()
    ]
    for name, year, season, error in downloads:
        if (error or "").startswith(MARKER_REMOVED):  # junk thrown out, and replaced
            continue
        lines.append(f"{_label(name, year, season)}: {_esc(_clip(error or 'failed'))}")
    return lines


def _nas_line(reading: Any) -> str:
    if reading.free is None:
        return f"<b>NAS</b>: free space unknown. {_esc(_clip(reading.note))}"
    line = f"<b>NAS</b>: {human_size(reading.free)} free of {human_size(reading.total)}"
    return f"{line} (as of an earlier reading)" if reading.stale else line


async def _compose(config: AppConfig, since: datetime, until: datetime) -> str:
    from .health import check_all
    from .inbox import collect
    from .storage import capacity

    try:
        providers = await check_all(config)
    except Exception as exc:  # the digest is worth more than this one section
        log.warning("digest.providers_failed", error=redact_exc(exc))
        providers = []

    def gather() -> tuple[list[str], list[str], list[str], Any]:
        return (
            _arrived(since, until),
            _subtitles(since, until),
            _failures(since, until),
            collect(config, providers),
        )

    arrived, subtitles, failures, inbox = await asyncio.to_thread(gather)
    reading = await capacity(config)
    attention = [f"{_esc(i.label)}: {_esc(_clip(i.title, 80))}" for i in inbox.items]
    needs = _capped(f"Needs attention ({inbox.total})", attention)
    needs.more += inbox.hidden
    sections = [
        _capped("Arrived", arrived),
        _capped("Subtitles", subtitles),
        _capped("Failed", failures),
        needs,
    ]
    quiet = "" if any(s.lines for s in sections) else "A quiet night: nothing to report."
    header = f"🎬 <b>Morning digest</b> · {until.astimezone():%a %-d %b}"
    return render("\n\n".join(p for p in (header, quiet) if p), sections, _nas_line(reading))


async def send_digest(config: AppConfig, now: datetime | None = None) -> bool:
    """Send the current day's digest unless it has gone out. True if this call sent it.

    Raises :class:`NotConfiguredError` (the scheduler skips quietly) when the digest
    is off or there's no Telegram bot; a failed send raises for the job's alert.
    """

    if not config.digest.enabled:
        raise NotConfiguredError("digest.enabled is off")
    if not telegram_configured(config):
        raise NotConfiguredError("Telegram is not configured")
    now = now or utcnow()
    day = digest_day(now.astimezone(), config.digest.hour)
    with job_lock(config, "digest"):
        previous = await asyncio.to_thread(_read_state)
        state = _parse(previous)
        if state.get("day") == day:
            return False
        try:
            since = datetime.fromisoformat(state["sent_at"])
        except (KeyError, TypeError, ValueError):
            since = now - timedelta(days=1)
        text = await _compose(config, since, now)

        claim = json.dumps({"day": day, "sent_at": now.isoformat()})
        await asyncio.to_thread(_write_state, claim)
        try:
            await send_telegram(config, text, parse_mode=PARSE_MODE)
        except _DELIVERY_UNKNOWN as exc:
            # Maybe delivered. Never twice wins over never missed.
            log.warning("digest.delivery_unknown", day=day, error=redact_exc(exc))
            raise
        except Exception:
            await asyncio.to_thread(_write_state, previous)
            raise
    log.info("digest.sent", day=day)
    return True
