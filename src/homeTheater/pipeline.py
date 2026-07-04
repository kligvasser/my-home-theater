"""Execution-pipeline transparency: what is each in-flight candidate doing?

Assembles a per-candidate :class:`ExecutionState` from the DB (candidate status,
its ``Download`` row, the title's owned-file subtitle coverage) plus, for the
torrent backend, a live Transmission poll (progress, seeders, speed, ETA). The
dashboard's Activity view renders these as a stepper:

    Grabbed → Downloading → Imported (copied to NAS) → Subtitles

so "is it downloading? was it copied? are the subs there?" is answerable at a
glance. Read-only and defensive: a Transmission outage degrades to DB-only state.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from .config import AppConfig
from .db.models import Candidate, CandidateStatus, Download, OwnedFile, Title
from .db.session import session_scope
from .errors import redact_exc
from .logging_setup import get_logger

log = get_logger(__name__)

# Candidates worth showing on the Activity view: only mid-flight items (grab →
# download → import). Once a movie's video is on the NAS it's ``imported`` and
# drops off here — subtitle backfill is best-effort and reported on the Library /
# Subtitles pages (a title may legitimately never get a Hebrew sub).
_ACTIVE = (CandidateStatus.queued, CandidateStatus.downloading, CandidateStatus.failed)


@dataclass(frozen=True, slots=True)
class Step:
    key: str  # grab | download | import | subs
    label: str
    state: str  # pending | active | done | failed


@dataclass
class ExecutionState:
    candidate_id: int
    title: str
    year: int | None
    kind: str
    status: str
    stage: str  # human summary, e.g. "Downloading 42%"
    release: str | None
    progress: float | None  # 0..1
    down_rate: int | None  # bytes/sec (live)
    seeders: int | None
    eta_seconds: int | None
    save_path: str | None
    error: str | None
    subtitle_present: list[str]
    subtitle_target: list[str]
    steps: list[Step] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        return d


@dataclass(frozen=True, slots=True)
class _Row:
    candidate_id: int
    title: str
    year: int | None
    kind: str
    status: CandidateStatus
    infohash: str | None
    release: str | None
    dl_state: str | None
    dl_progress: float | None
    save_path: str | None
    error: str | None
    subtitle_present: list[str]


def _collect(config: AppConfig) -> list[_Row]:
    rows: list[_Row] = []
    with session_scope() as s:
        pairs = s.execute(
            select(Candidate, Title)
            .join(Title, Title.id == Candidate.title_id)
            .where(Candidate.status.in_(_ACTIVE))  # imported items drop off here
            .order_by(Candidate.decided_at.desc(), Candidate.id.desc())
            .limit(100)
        ).all()
        for cand, title in pairs:
            present = sorted(
                {
                    lang
                    for f in s.scalars(
                        select(OwnedFile).where(OwnedFile.title_id == title.id)
                    ).all()
                    for lang in (f.subtitle_langs or [])
                }
            )
            dl = s.scalar(
                select(Download)
                .where(Download.candidate_id == cand.id)
                .order_by(Download.id.desc())
            )
            rows.append(
                _Row(
                    candidate_id=cand.id,
                    title=title.title,
                    year=title.year,
                    kind=str(title.kind),
                    status=cand.status,
                    infohash=dl.external_id if dl else None,
                    release=dl.release if dl else None,
                    dl_state=dl.state if dl else None,
                    dl_progress=dl.progress if dl else None,
                    save_path=dl.save_path if dl else None,
                    error=dl.error if dl else None,
                    subtitle_present=present,
                )
            )
    return rows


async def _live_progress(config: AppConfig, rows: list[_Row]) -> dict[str, Any]:
    """Poll Transmission for the hashes still transferring (torrent backend)."""

    if config.acquisition.backend != "torrent":
        return {}
    hashes = [r.infohash for r in rows if r.infohash and r.dl_state in ("queued", "downloading")]
    if not hashes:
        return {}
    from .acquisition.torrent.service import _download_client

    live: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
            client = _download_client(config, http)
            for infohash in hashes:
                try:
                    st = await client.status(infohash)
                except Exception as exc:
                    log.debug("pipeline.live_poll_failed", detail=redact_exc(exc))
                    continue
                if st is not None:
                    live[infohash] = st
    except Exception as exc:  # client construction / transport-wide failure
        log.debug("pipeline.live_unavailable", detail=redact_exc(exc))
    return live


def _build(row: _Row, live: Any, target: list[str]) -> ExecutionState:
    imported = row.status is CandidateStatus.imported or row.dl_state == "imported"
    failed = row.status is CandidateStatus.failed or row.dl_state == "failed"
    importing = row.dl_state == "importing"
    downloading = row.status is CandidateStatus.downloading or row.dl_state == "downloading"

    progress = row.dl_progress
    down_rate = seeders = eta = None
    # While importing, dl_progress is the NAS-copy fraction — don't let the live
    # torrent poll (which reads 100% downloaded) clobber it.
    if live is not None and not importing:
        progress = live.progress
        down_rate, seeders, eta = live.down_rate, live.seeders, live.eta_seconds

    subs_done = bool(target) and set(target).issubset(row.subtitle_present)
    downloaded = imported or importing or (progress or 0) >= 1.0

    if failed:
        dl_step = "failed"
    elif downloaded:
        dl_step = "done"
    else:  # queued or downloading — it's in the client, transferring
        dl_step = "active"
    # Once the bytes are down, "import" is the next active step until it lands.
    if imported:
        imp_step = "done"
    elif failed:
        imp_step = "failed"
    elif downloaded:  # completed or actively importing
        imp_step = "active"
    else:
        imp_step = "pending"
    subs_step = "done" if subs_done else ("active" if imported else "pending")

    steps = [
        Step("grab", "Grabbed", "done"),
        Step("download", "Downloading", dl_step),
        Step("import", "Imported to NAS", imp_step),
        Step("subs", "Subtitles", subs_step),
    ]
    stage = _stage(failed, imported, importing, downloading, progress, subs_done, target, row)
    return ExecutionState(
        candidate_id=row.candidate_id,
        title=row.title,
        year=row.year,
        kind=row.kind,
        status=str(row.status),
        stage=stage,
        release=row.release,
        progress=progress,
        down_rate=down_rate,
        seeders=seeders,
        eta_seconds=eta,
        save_path=row.save_path,
        error=row.error,
        subtitle_present=row.subtitle_present,
        subtitle_target=list(target),
        steps=steps,
    )


def _stage(
    failed: bool,
    imported: bool,
    importing: bool,
    downloading: bool,
    progress: float | None,
    subs_done: bool,
    target: list[str],
    row: _Row,
) -> str:
    if failed:
        return "Failed"
    if importing:
        return f"Importing to NAS {round((progress or 0) * 100)}%"
    if imported:
        if subs_done or not target:
            return "Done"
        missing = [lang for lang in target if lang not in row.subtitle_present]
        return f"Fetching subtitles ({', '.join(missing)})"
    if downloading:
        pct = round((progress or 0) * 100)
        return "Downloaded — importing…" if pct >= 100 else f"Downloading {pct}%"
    return "Queued"


async def activity(config: AppConfig) -> list[ExecutionState]:
    """Live execution state for every in-flight candidate."""

    # _collect is synchronous SQLAlchemy — keep it off the event loop (the Activity
    # page polls this, so a slow/locked DB shouldn't stall the whole server).
    rows = await asyncio.to_thread(_collect, config)
    live = await _live_progress(config, rows)
    target = list(config.subtitles.languages)
    return [_build(r, live.get(r.infohash or ""), target) for r in rows]
