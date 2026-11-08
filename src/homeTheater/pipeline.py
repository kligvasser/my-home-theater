"""Execution-pipeline transparency: what is each in-flight candidate doing?

Assembles a per-candidate :class:`ExecutionState` from the DB (candidate status,
its ``Download`` row, the title's owned-file subtitle coverage) plus, for the
torrent backend, a live Transmission poll (progress, seeders, speed, ETA). The
dashboard's Activity view renders these as a stepper:

    Grabbed → Downloading → Imported (copied to NAS) → Subtitles

so "is it downloading? was it copied? are the subs there?" is answerable at a
glance. Read-only and defensive: a Transmission outage degrades to DB-only state.

The page polls this every few seconds from every open tab, so the cost is bounded:
one batched ``torrent-get`` per poll over a shared client, an overall time limit,
a short cache so tabs coalesce, and ``IN`` queries instead of one per candidate.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, cast

import httpx
from sqlalchemy import or_, select

from .acquisition.torrent.base import TorrentStatus
from .acquisition.torrent.transmission import TransmissionClient
from .config import AppConfig
from .db.models import (
    LIVE_DOWNLOAD_STATES,
    MARKER_HELD,
    MARKER_NEEDS_ATTENTION,
    MARKER_REMOVAL_PENDING,
    MARKER_REMOVED,
    STOPPED_STATUSES,
    Candidate,
    CandidateStatus,
    Download,
    OwnedFile,
    Title,
)
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
# The states whose numbers come from the download client.
_TRANSFERRING = ("queued", "downloading")

_HELD_STAGE = "Held — action needed"
_NEEDS_ATTENTION_STAGE = "Needs attention"
_MAX_ERRORS = 5

# Live poll limits. One slow Transmission must not hold the page, and a dead one
# isn't asked again on every poll of every tab.
LIVE_TIMEOUT_S = 3.0
LIVE_TTL_S = 2.0
LIVE_DOWN_TTL_S = 5.0


@dataclass(frozen=True, slots=True)
class Step:
    key: str  # grab | download | import | subs
    label: str
    state: str  # pending | active | held | done | failed


@dataclass(frozen=True, slots=True)
class EpisodeState:
    """One download inside a multi-episode (season) candidate, for the Activity
    card's per-episode breakdown."""

    label: str  # "S02E01", or "S02 pack" / the release name when unparseable
    stage: str  # human summary, same vocabulary as ExecutionState.stage
    state: str  # pending | active | held | done | failed
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
    # What the owner has to do for a parked download; separate from ``error``
    # because nothing has failed and nothing will progress without them.
    attention: str | None = None
    # A parked import that ``POST /api/candidates/{id}/retry-import`` can send round.
    can_retry_import: bool = False
    # Transmission didn't answer, so the transfer numbers are the last saved ones.
    live_down: bool = False

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


def _shown(dls: Iterable[_Dl]) -> list[_Dl]:
    """The rows that describe this grab: not ban records, not abandoned attempts."""

    return [
        d for d in dls if d.state != "cancelled" and not (d.error or "").startswith(MARKER_REMOVED)
    ]


def _transferring(dl: _Dl) -> bool:
    return dl.state in _TRANSFERRING


def _attention_of(error: str | None) -> str | None:
    """What the owner has to do about a parked download, if it is parked. A hold's
    text is its own instruction; a gave-up import only says what went wrong."""

    if error and error.startswith(MARKER_HELD):
        return error.removeprefix(MARKER_HELD).strip()
    if error and error.startswith(MARKER_NEEDS_ATTENTION):
        reason = error.removeprefix(MARKER_NEEDS_ATTENTION).strip()
        return f"Import gave up: {reason}. Fix that, then press Retry import."
    return None


def _parked_stage(error: str | None) -> str | None:
    if error and error.startswith(MARKER_NEEDS_ATTENTION):
        return _NEEDS_ATTENTION_STAGE
    return _HELD_STAGE if error and error.startswith(MARKER_HELD) else None


def _primary(dls: list[_Dl]) -> _Dl | None:
    """The download a candidate's headline should track: the least-advanced one
    still in flight (so a lagging episode wins over a finished sibling), falling
    back to the furthest-along row when nothing is in flight (all failed). Ban
    records and cancelled attempts never count."""

    shown = _shown(dls)
    if not shown:
        return None
    in_flight = [d for d in shown if d.state in LIVE_DOWNLOAD_STATES]
    if in_flight:
        return min(
            in_flight, key=lambda d: (_STATE_RANK.get(d.state or "", 9), d.progress or 0.0, d.id)
        )
    # Nothing transferring: show the most-advanced row (its error, if failed).
    return max(shown, key=lambda d: (_STATE_RANK.get(d.state or "", -1), d.id))


def _present_langs(files: Sequence[tuple[int | None, list[str]]], season: int | None) -> list[str]:
    """Subtitle languages a candidate actually has. A season candidate only counts
    its own season, and a language only once every episode there has it; one old
    copy of a movie having subs is enough for the movie."""

    if season is None:
        return sorted({lang for _, langs in files for lang in langs})
    scoped = [set(langs) for file_season, langs in files if file_season == season]
    return sorted(set.intersection(*scoped)) if scoped else []


def _collect(config: AppConfig, only: Collection[int] | None = None) -> list[_Row]:
    """Rows for the Activity view, or for exactly the candidates in ``only``."""

    rows: list[_Row] = []
    with session_scope() as s:
        query = (
            select(Candidate, Title)
            .join(Title, Title.id == Candidate.title_id)
            .order_by(Candidate.decided_at.desc(), Candidate.id.desc())
        )
        if only is None:
            query = query.where(Candidate.status.in_(_ACTIVE)).limit(100)  # imported drop off
        else:
            query = query.where(Candidate.id.in_(only))
        pairs = s.execute(query).all()
        files: dict[int, list[tuple[int | None, list[str]]]] = {}
        for title_id, season, langs in s.execute(
            select(OwnedFile.title_id, OwnedFile.season, OwnedFile.subtitle_langs).where(
                OwnedFile.title_id.in_([title.id for _, title in pairs])
            )
        ):
            files.setdefault(title_id, []).append((season, langs or []))
        downloads: dict[int, list[_Dl]] = {}
        for dl in s.scalars(
            select(Download)
            .where(Download.candidate_id.in_([cand.id for cand, _ in pairs]))
            .order_by(Download.id.asc())
        ):
            downloads.setdefault(dl.candidate_id, []).append(
                _Dl(
                    id=dl.id,
                    infohash=dl.external_id,
                    release=dl.release,
                    state=dl.state,
                    progress=dl.progress,
                    save_path=dl.save_path,
                    error=dl.error,
                )
            )
        for cand, title in pairs:
            dls = downloads.get(cand.id, [])
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
                    subtitle_present=_present_langs(files.get(title.id, []), cand.season),
                    downloads=tuple(dls),
                )
            )
    return rows


@dataclass(slots=True)
class _LivePoller:
    """Transmission polling shared by every Activity poll on one event loop.

    Keeps the HTTP client (and so Transmission's session id) between polls, and
    the last answer for a moment, so N tabs cost one ``torrent-get``.
    """

    loop: asyncio.AbstractEventLoop
    key: tuple[Any, ...]
    client: TransmissionClient
    task: asyncio.Task[None] | None = None
    task_hashes: frozenset[str] = frozenset()
    hashes: frozenset[str] = frozenset()
    live: dict[str, TorrentStatus] = field(default_factory=dict)
    down: bool = False
    expires: float = 0.0

    async def fetch(self, hashes: frozenset[str]) -> None:
        down = False
        live: dict[str, TorrentStatus] = {}
        try:
            async with asyncio.timeout(LIVE_TIMEOUT_S):
                found = await self.client.statuses(sorted({h.lower() for h in hashes}))
            live = {h: found[h.lower()] for h in hashes if h.lower() in found}
        except Exception as exc:  # timeout / transport failure: DB-only output
            down = True
            log.debug("pipeline.live_unavailable", detail=redact_exc(exc))
        self.hashes, self.live, self.down = hashes, live, down
        self.expires = time.monotonic() + (LIVE_DOWN_TTL_S if down else LIVE_TTL_S)
        if self.task is asyncio.current_task():
            self.task = None

    async def poll(self, hashes: frozenset[str]) -> tuple[dict[str, TorrentStatus], bool]:
        if time.monotonic() >= self.expires or not (self.down or hashes <= self.hashes):
            task = self.task
            if task is None or task.done() or not hashes <= self.task_hashes:
                self.task_hashes = hashes | (self.task_hashes if self.task else frozenset())
                task = self.task = self.loop.create_task(self.fetch(self.task_hashes))
            # Shielded: a tab going away mustn't cancel the poll other tabs wait on.
            await asyncio.shield(task)
        return self.live, self.down


_poller: _LivePoller | None = None


def _live_poller(config: AppConfig) -> _LivePoller:
    """The shared poller, rebuilt if the loop or the Transmission login changed."""

    from .acquisition.torrent.service import _download_client

    global _poller
    loop = asyncio.get_running_loop()
    s = config.secrets
    key = (s.transmission_url, s.transmission_user, s.transmission_pass)
    if _poller is None or _poller.loop is not loop or _poller.key != key:
        http = httpx.AsyncClient(timeout=LIVE_TIMEOUT_S)
        _poller = _LivePoller(loop, key, cast(TransmissionClient, _download_client(config, http)))
    return _poller


async def _live_progress(
    config: AppConfig, rows: list[_Row]
) -> tuple[dict[str, TorrentStatus], bool]:
    """Live stats for the hashes still transferring (torrent backend), and whether
    Transmission failed to answer."""

    if config.acquisition.backend != "torrent":
        return {}, False
    # Poll every still-transferring download, not just each candidate's primary:
    # a season card shows live progress per episode.
    hashes = frozenset(
        d.infohash for r in rows for d in _shown(r.downloads) if d.infohash and _transferring(d)
    )
    if not hashes:
        return {}, False
    try:
        poller = _live_poller(config)
    except Exception as exc:  # client construction
        log.debug("pipeline.live_unavailable", detail=redact_exc(exc))
        return {}, True
    return await poller.poll(hashes)


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


def _transfer_stage(
    state: str | None, progress: float | None, parked: str | None
) -> tuple[str, str]:
    """(step state, stage text) of one download. Driven by its state; ``progress``
    is only ever a number within that state, because its meaning changes: while
    importing it's the NAS-copy fraction, and 99.6% must not read as finished."""

    fraction = progress or 0.0
    pct = round(fraction * 100)
    if state == "failed":
        return "failed", "Failed"
    if state == "imported":
        return "done", "Done"
    if parked:
        return "held", parked
    if state == "importing":
        return "active", f"Importing to NAS {pct}%"
    if state == "completed" or fraction >= 1.0:
        return "active", "Downloaded — importing…"
    if state == "queued" and fraction == 0.0:
        return "active", "Queued"
    return "active", f"Downloading {min(pct, 99)}%"


def _episode_state(dl: _Dl, live: Any) -> tuple[EpisodeState, tuple[int, int]]:
    """Build one :class:`EpisodeState` from a download row (+ its live poll)."""

    progress = dl.progress
    down_rate = seeders = eta = None
    if live is not None and _transferring(dl):
        progress = live.progress
        down_rate, seeders, eta = live.down_rate, live.seeders, live.eta_seconds

    step, stage = _transfer_stage(dl.state, progress, _parked_stage(dl.error))
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


def _notes(dls: Sequence[_Dl], status: CandidateStatus) -> tuple[str | None, str | None]:
    """(error, attention) across every download of a candidate, so a failed episode
    isn't hidden behind the sibling the headline follows. Identical messages share
    one line. A candidate that failed only on ban records still says why."""

    shown = _shown(dls)
    multi = len(shown) > 1
    errors: dict[str, list[str]] = {}
    attention: dict[str, list[str]] = {}
    for d in shown:
        if not d.error or d.state == "imported" or d.error == MARKER_REMOVAL_PENDING:
            continue
        note = _attention_of(d.error)
        into = errors if note is None else attention
        labels = into.setdefault(note or d.error, [])
        if multi:
            labels.append(_episode_label(d.release)[0])

    def joined(notes: dict[str, list[str]]) -> str | None:
        lines = [f"{', '.join(ls)}: {msg}" if ls else msg for msg, ls in notes.items()]
        if len(lines) > _MAX_ERRORS:
            lines[_MAX_ERRORS:] = [f"…and {len(lines) - _MAX_ERRORS} more"]
        return "\n".join(lines) or None

    error = joined(errors)
    if error is None and not shown and status is CandidateStatus.failed:
        banned = [d.error for d in dls if (d.error or "").startswith(MARKER_REMOVED)]
        error = banned[-1] if banned else None
    return error, joined(attention)


def _build(
    row: _Row,
    live: Any,
    target: list[str],
    live_map: dict[str, Any] | None = None,
    live_down: bool = False,
) -> ExecutionState:
    state = row.dl_state
    in_flight = state in LIVE_DOWNLOAD_STATES
    # A failed season can still have an episode finishing: report what's happening.
    failed = state == "failed" or (row.status is CandidateStatus.failed and not in_flight)
    imported = state == "imported" or (row.status is CandidateStatus.imported and not in_flight)
    importing = state == "importing"

    progress = row.dl_progress
    down_rate = seeders = eta = None
    # While importing, dl_progress is the NAS-copy fraction — don't let the live
    # torrent poll (which reads 100% downloaded) clobber it.
    if live is not None and state in _TRANSFERRING:
        progress = live.progress
        down_rate, seeders, eta = live.down_rate, live.seeders, live.eta_seconds

    dls = row.downloads or (
        _Dl(0, row.infohash, row.release, state, row.dl_progress, row.save_path, row.error),
    )
    error, attention = _notes(dls, row.status)
    parked = _parked_stage(row.error)
    subs_done = bool(target) and set(target).issubset(row.subtitle_present)
    downloaded = imported or importing or state == "completed" or (progress or 0.0) >= 1.0

    # No download row means the grab itself hasn't happened.
    grab_step = ("failed" if failed else "active") if state is None else "done"
    if downloaded:
        dl_step = "done"
    elif failed and state is not None:
        dl_step = "failed"
    elif state is None:
        dl_step = "pending"
    else:  # queued or downloading — it's in the client, transferring
        dl_step = "active"
    # Once the bytes are down, "import" is the next active step until it lands.
    if imported and not failed:
        imp_step = "done"
    elif not downloaded:
        imp_step = "pending"
    elif failed:
        imp_step = "failed"
    else:  # completed or actively importing
        imp_step = "held" if parked else "active"
    if subs_done or (imported and not target):
        subs_step = "done"
    else:
        subs_step = "active" if imported and not failed else "pending"

    steps = [
        Step("grab", "Grabbed", grab_step),
        Step("download", "Downloading", dl_step),
        Step("import", "Imported to NAS", imp_step),
        Step("subs", "Subtitles", subs_step),
    ]
    stage = _stage(failed, imported, parked, progress, subs_done, target, row)

    # Season candidates grab one torrent per episode; show each so a lagging
    # episode is visible instead of hidden behind one collapsed row.
    episodes: list[EpisodeState] = []
    shown = _shown(row.downloads)
    if len(shown) > 1:
        built = [_episode_state(d, (live_map or {}).get(d.infohash or "")) for d in shown]
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
        error=error,
        subtitle_present=row.subtitle_present,
        subtitle_target=list(target),
        steps=steps,
        episodes=episodes,
        attention=attention,
        can_retry_import=any(
            (d.error or "").startswith(MARKER_NEEDS_ATTENTION) and d.state == "completed"
            for d in row.downloads
        ),
        live_down=live_down and any(_transferring(d) for d in shown),
    )


def _stage(
    failed: bool,
    imported: bool,
    parked: str | None,
    progress: float | None,
    subs_done: bool,
    target: list[str],
    row: _Row,
) -> str:
    if failed:
        return "Failed"
    if imported:
        if subs_done or not target:
            return "Done"
        missing = [lang for lang in target if lang not in row.subtitle_present]
        return f"Fetching subtitles ({', '.join(missing)})"
    if row.dl_state is None:
        return "Queued"
    return _transfer_stage(row.dl_state, progress, parked)[1]


async def activity(config: AppConfig) -> list[ExecutionState]:
    """Live execution state for every in-flight candidate."""

    # _collect is synchronous SQLAlchemy — keep it off the event loop (the Activity
    # page polls this, so a slow/locked DB shouldn't stall the whole server).
    rows = await asyncio.to_thread(_collect, config)
    live, live_down = await _live_progress(config, rows)
    target = list(config.subtitles.languages)
    return [_build(r, live.get(r.infohash or ""), target, live, live_down) for r in rows]


def attention_states(config: AppConfig) -> list[ExecutionState]:
    """Candidates waiting on a person, wherever they are: failed, or holding a parked
    download. Wider than :func:`activity`, because a top-up season sits at ``approved``
    or ``imported`` with its parked episode. DB only, so it's cheap to ask often."""

    with session_scope() as s:
        parked = select(Download.candidate_id).where(
            Download.state.not_in(("imported", "cancelled", "failed")),
            or_(
                Download.error.like(f"{MARKER_HELD}%"),
                Download.error.like(f"{MARKER_NEEDS_ATTENTION}%"),
            ),
        )
        ids = s.scalars(
            select(Candidate.id).where(
                Candidate.status.not_in(STOPPED_STATUSES),
                or_(Candidate.status == CandidateStatus.failed, Candidate.id.in_(parked)),
            )
        ).all()
    if not ids:
        return []
    target = list(config.subtitles.languages)
    return [_build(row, None, target) for row in _collect(config, only=ids)]
