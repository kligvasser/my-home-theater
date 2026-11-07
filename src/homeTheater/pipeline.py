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

# How "advanced" each download state is, lowest = least done. The Activity card's
# headline tracks the *least-advanced* in-flight download of a candidate, so a
# season whose E02 already imported but whose E01 is still downloading reads
# "Downloading E01", never a premature "Done". States off this list (cancelled)
# rank last so they never win the headline.
_STATE_RANK = {
    "queued": 0,
    "downloading": 1,
    "importing": 2,
    "completed": 3,
    "imported": 4,
    "failed": 5,
}
# Still-transferring / mid-import states (not yet safely on the NAS).
_IN_FLIGHT = ("queued", "downloading", "importing", "completed")


@dataclass(frozen=True, slots=True)
class Step:
    key: str  # grab | download | import | subs
    label: str
    state: str  # pending | active | done | failed


@dataclass(frozen=True, slots=True)
class EpisodeState:
    """One download inside a multi-episode (season) candidate, for the Activity
    card's per-episode breakdown."""

    label: str  # "S02E01", or "S02 pack" / the release name when unparseable
    stage: str  # human summary, same vocabulary as ExecutionState.stage
    state: str  # pending | active | done | failed
    progress: float | None  # 0..1
    down_rate: int | None = None
    seeders: int | None = None
    eta_seconds: int | None = None


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
    # Per-episode breakdown for multi-episode (season) candidates; empty for
    # movies and single-episode grabs (the headline fields say it all).
    episodes: list[EpisodeState] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["steps"] = [asdict(s) for s in self.steps]
        d["episodes"] = [asdict(e) for e in self.episodes]
        return d


@dataclass(frozen=True, slots=True)
class _Dl:
    """One ``Download`` row of a candidate (a season candidate has several)."""

    id: int
    infohash: str | None
    release: str | None
    state: str | None
    progress: float | None
    save_path: str | None
    error: str | None


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
    # Every download of the candidate (season = one per episode). The headline
    # fields above mirror the *primary* one (least-advanced still in flight).
    downloads: tuple[_Dl, ...] = ()


def _primary(dls: list[_Dl]) -> _Dl | None:
    """The download a candidate's headline should track: the least-advanced one
    still in flight (so a lagging episode wins over a finished sibling), falling
    back to the furthest-along row when nothing is in flight (all failed)."""

    if not dls:
        return None
    in_flight = [d for d in dls if d.state in _IN_FLIGHT]
    if in_flight:
        return min(
            in_flight, key=lambda d: (_STATE_RANK.get(d.state or "", 9), d.progress or 0.0, d.id)
        )
    # Nothing transferring: show the most-advanced row (its error, if failed).
    return max(dls, key=lambda d: (_STATE_RANK.get(d.state or "", -1), d.id))


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
            dls = [
                _Dl(
                    id=dl.id,
                    infohash=dl.external_id,
                    release=dl.release,
                    state=dl.state,
                    progress=dl.progress,
                    save_path=dl.save_path,
                    error=dl.error,
                )
                for dl in s.scalars(
                    select(Download)
                    .where(Download.candidate_id == cand.id)
                    .order_by(Download.id.asc())
                ).all()
            ]
            primary = _primary(dls)
            rows.append(
                _Row(
                    candidate_id=cand.id,
                    title=title.title,
                    year=title.year,
                    kind=str(title.kind),
                    status=cand.status,
                    infohash=primary.infohash if primary else None,
                    release=primary.release if primary else None,
                    dl_state=primary.state if primary else None,
                    dl_progress=primary.progress if primary else None,
                    save_path=primary.save_path if primary else None,
                    error=primary.error if primary else None,
                    subtitle_present=present,
                    downloads=tuple(dls),
                )
            )
    return rows


async def _live_progress(config: AppConfig, rows: list[_Row]) -> dict[str, Any]:
    """Poll Transmission for the hashes still transferring (torrent backend)."""

    if config.acquisition.backend != "torrent":
        return {}
    # Poll every still-transferring download, not just each candidate's primary:
    # a season card shows live progress per episode.
    hashes = sorted(
        {
            d.infohash
            for r in rows
            for d in r.downloads
            if d.infohash and d.state in ("queued", "downloading")
        }
    )
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


def _episode_label(release: str | None) -> tuple[str, tuple[int, int]]:
    """A compact ``S02E01`` label (and a (season, episode) sort key) for one
    episode download, parsed from its release name. A season pack → ``S02 pack``;
    anything unparseable falls back to a trimmed release name."""

    from .acquisition.torrent.select import parse_season_episode

    seasons, episodes = parse_season_episode(release or "")
    sn = seasons[0] if seasons else 0
    if episodes:
        ep = min(episodes)
        return f"S{sn:02d}E{ep:02d}", (sn, ep)
    if seasons:
        return f"S{sn:02d} pack", (sn, 0)
    name = (release or "episode").strip()
    return (name[:40] + "…" if len(name) > 41 else name), (0, 0)


def _episode_state(dl: _Dl, live: Any) -> tuple[EpisodeState, tuple[int, int]]:
    """Build one :class:`EpisodeState` from a download row (+ its live poll)."""

    state = dl.state or ""
    failed = state == "failed"
    importing = state == "importing"
    imported = state == "imported"

    progress = dl.progress
    down_rate = seeders = eta = None
    if live is not None and state in ("queued", "downloading"):
        progress = live.progress
        down_rate, seeders, eta = live.down_rate, live.seeders, live.eta_seconds

    pct = round((progress or 0) * 100)
    if failed:
        step, stage = "failed", "Failed"
    elif imported:
        step, stage = "done", "Done"
    elif importing:
        step, stage = "active", f"Importing to NAS {pct}%"
    elif state == "completed" or pct >= 100:
        step, stage = "active", "Downloaded — importing…"
    elif state == "queued" and pct == 0:
        step, stage = "active", "Queued"
    else:
        step, stage = "active", f"Downloading {pct}%"

    label, key = _episode_label(dl.release)
    return (
        EpisodeState(
            label=label,
            stage=stage,
            state=step,
            progress=progress,
            down_rate=down_rate,
            seeders=seeders,
            eta_seconds=eta,
        ),
        key,
    )


def _build(
    row: _Row, live: Any, target: list[str], live_map: dict[str, Any] | None = None
) -> ExecutionState:
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

    # Season candidates grab one torrent per episode; show each so a lagging
    # episode is visible instead of hidden behind one collapsed row.
    episodes: list[EpisodeState] = []
    if len(row.downloads) > 1:
        built = [_episode_state(d, (live_map or {}).get(d.infohash or "")) for d in row.downloads]
        built.sort(key=lambda bk: bk[1])
        episodes = [es for es, _ in built]

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
        episodes=episodes,
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
    return [_build(r, live.get(r.infohash or ""), target, live) for r in rows]
