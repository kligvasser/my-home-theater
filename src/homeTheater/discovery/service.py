"""Discovery orchestration (plan §5.4): gather -> dedup -> enrich -> filter -> rank.

Writes ranked ``candidate`` rows with human-readable reasons, excluding titles you
already own (on disk or in Radarr/Sonarr), already have a live candidate for, or
already rejected. Honors ``auto_approve``. Each created candidate snapshots its
feature vector (``Candidate.features``) — the training data for the preference
model, labeled later by your approve/reject/import decisions.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import (
    Candidate,
    CandidateStatus,
    JobRun,
    OwnedFile,
    RunStatus,
    Title,
    TitleKind,
)
from ..db.session import session_scope
from ..errors import NotConfiguredError, redact_exc
from ..features import extract_features
from ..logging_setup import bind_run, clear_run, get_logger
from ..metadata.dto import OmdbRatings, TmdbTitle
from ..metadata.omdb import OMDbClient
from ..metadata.service import apply_tmdb_details
from ..metadata.tmdb import TMDbClient
from .filters import evaluate, score
from .sources import CandidateSource, Discovered, build_sources

log = get_logger(__name__)

# Statuses that mean "we're already handling (or have handled) this title" — a new
# discovery hit for it is a duplicate. ``imported`` is included so an already-grabbed
# title is never re-suggested even if its owned-file record isn't in the catalog yet
# (e.g. a torrent import before the next NAS scan).
LIVE_STATUSES = (
    CandidateStatus.new,
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
    CandidateStatus.imported,
)

# A rejected title stays rejected: trending will resurface it forever otherwise,
# and the rejection is a training label we must not bury under duplicates. A
# cancelled one (you stopped it, or unfollowed the show) is no label, but it isn't
# an invitation to suggest the same thing again either.
BLOCKING_STATUSES = (*LIVE_STATUSES, CandidateStatus.rejected, CandidateStatus.cancelled)

# A whole-title candidate still being worked on covers every season of the show.
IN_FLIGHT = (
    CandidateStatus.new,
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
)

# A failed candidate is retried by reusing its row, after 1, 2, 4... days.
RETRY_BASE_DAYS = 1
RETRY_MAX_DAYS = 14
_RETRIES_KEY = "discovery_retries"

_Key = tuple[TitleKind, int]


@dataclass
class DiscoveryStats:
    sources: int = 0
    fetched: int = 0
    deduped: int = 0
    owned_skipped: int = 0
    live_skipped: int = 0
    rejected_skipped: int = 0
    backoff_skipped: int = 0
    sources_failed: int = 0
    considered: int = 0
    created: int = 0
    retried: int = 0
    filtered: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Enriched:
    disc: Discovered
    details: TmdbTitle | None
    ratings: OmdbRatings | None
    error: str | None = None


def _owned_live_rejected() -> tuple[set[_Key], set[_Key], set[_Key]]:
    """(kind, tmdb_id) sets: owned, with a live candidate, with a rejection.

    Keyed by kind because TMDb movie and TV ids are independent sequences —
    a bare tmdb_id match would confuse tv/1396 with movie/1396.
    """

    with session_scope() as s:

        def _ids(*where: Any) -> set[_Key]:
            rows = s.execute(
                select(Title.kind, Title.tmdb_id).where(Title.tmdb_id.is_not(None), *where)
            ).all()
            return {(kind, tid) for kind, tid in rows}

        owned = _ids(exists().where(OwnedFile.title_id == Title.id) | Title.arr_has_file.is_(True))
        live = _ids(
            exists().where((Candidate.title_id == Title.id) & (Candidate.status.in_(LIVE_STATUSES)))
        )
        rejected = _ids(
            exists().where(
                (Candidate.title_id == Title.id)
                & (Candidate.status.in_((CandidateStatus.rejected, CandidateStatus.cancelled)))
            )
        )
        return owned, live, rejected


async def _enrich(
    disc: Discovered, tmdb: TMDbClient, omdb: OMDbClient | None, sem: asyncio.Semaphore
) -> _Enriched:
    async with sem:
        try:
            details = await tmdb.details(disc.tmdb.tmdb_id, disc.kind)
            ratings = None
            if omdb is not None and details.imdb_id:
                # OMDb failure is non-fatal: fall back to TMDb rating/votes.
                try:
                    ratings = await omdb.by_imdb_id(details.imdb_id)
                except Exception as exc:
                    log.warning(
                        "discovery.omdb_failed", title=disc.tmdb.title, error=redact_exc(exc)
                    )
            return _Enriched(disc, details, ratings)
        except Exception as exc:
            log.warning("discovery.enrich_failed", title=disc.tmdb.title, error=redact_exc(exc))
            return _Enriched(disc, None, None, error=redact_exc(exc))


def _upsert_title(session: Session, kind: TitleKind, t: TmdbTitle) -> Title:
    """Get-or-create by (tmdb_id, kind) — never flips an existing row's kind."""

    title = session.scalar(select(Title).where(Title.tmdb_id == t.tmdb_id, Title.kind == kind))
    if title is None:
        title = Title(tmdb_id=t.tmdb_id, kind=kind, title=t.title)
        session.add(title)
    apply_tmdb_details(session, title, t)
    session.flush()
    return title


def retry_due(failed: Candidate, now: datetime | None = None) -> bool:
    """Has a failed candidate waited out its backoff?"""

    retries = int((failed.features or {}).get(_RETRIES_KEY, 0))
    days = min(RETRY_BASE_DAYS * 2**retries, RETRY_MAX_DAYS)
    failed_at = failed.updated_at
    if failed_at.tzinfo is None:  # SQLite hands back naive UTC
        failed_at = failed_at.replace(tzinfo=UTC)
    return (now or utcnow()) - failed_at >= timedelta(days=days)


def _latest_failed(session: Session, title_id: int, season: int | None) -> Candidate | None:
    """The newest failed row for (title, season). Older ones are left as history."""

    return session.scalar(
        select(Candidate)
        .where(
            Candidate.title_id == title_id,
            Candidate.status == CandidateStatus.failed,
            Candidate.season.is_(None) if season is None else Candidate.season == season,
        )
        .order_by(Candidate.id.desc())
    )


def _blocked_in_db(
    session: Session, title_id: int, season: int | None = None, *, top_up: bool = False
) -> str | None:
    """Re-check owned/live/rejected inside the write transaction (TOCTOU guard).

    Season-scoped candidates check per (title, season): the title itself is
    owned by definition, and rejecting S3 must not block a future S4 (each
    season decision is its own training label). A whole-title candidate still in
    flight covers the season too. ``top_up`` is a partly owned season: owning some
    of it, or an import that finished before these episodes aired, is the point.
    """

    statuses = tuple(s for s in BLOCKING_STATUSES if not (top_up and s is CandidateStatus.imported))
    cand_where = [Candidate.title_id == title_id, Candidate.status.in_(statuses)]
    if season is not None:
        cand_where = [
            Candidate.title_id == title_id,
            or_(
                and_(Candidate.season == season, Candidate.status.in_(statuses)),
                and_(Candidate.season.is_(None), Candidate.status.in_(IN_FLIGHT)),
            ),
        ]
    status = session.scalar(select(Candidate.status).where(*cand_where))
    if status is not None:
        return f"candidate already {status.value}"
    if top_up:
        return None
    owned_where = [OwnedFile.title_id == title_id]
    if season is not None:
        owned_where.append(OwnedFile.season == season)
    owned = session.scalar(select(OwnedFile.id).where(*owned_where))
    if owned is not None:
        return "season already owned" if season is not None else "already owned"
    return None


class _Scorers:
    """Taste index + preference model. Both only improve the ranking, so a broken
    one means ranking without it, never a failed run."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._indexes: dict[TitleKind, Any] = {}

    def taste_index(self, kind: TitleKind) -> Any:
        """Lazy per-kind index over the owned library (content similarity — sklearn
        import deferred so discovery without taste stays light)."""

        taste = self._config.taste
        if kind not in self._indexes:
            self._indexes[kind] = None
            if taste.enabled:
                try:
                    from ..taste import build_index

                    self._indexes[kind] = build_index(kind, min_library=taste.min_library)
                except Exception as exc:
                    log.warning("discovery.taste_failed", kind=kind, error=redact_exc(exc))
        return self._indexes[kind]

    def p_like(self, feats: dict[str, Any]) -> float | None:
        """P(you'd approve this) from the trained model, if a usable one exists."""

        from ..preferences import predict

        return predict(self._config, feats)


def _persist_one(
    item: _Enriched, config: AppConfig, scorers: _Scorers, stats: DiscoveryStats
) -> None:
    """Write one discovered item in its own transaction."""

    details = item.details
    assert details is not None
    taste_cfg = config.taste
    disc = item.disc
    auto = config.features.auto_approve or disc.auto_grab
    imdb_rating = item.ratings.imdb_rating if item.ratings else None
    imdb_votes = item.ratings.imdb_votes if item.ratings else None

    with session_scope() as session:
        title = _upsert_title(session, disc.kind, details)
        if imdb_rating is not None:
            title.imdb_rating = imdb_rating
        if imdb_votes is not None:
            title.imdb_votes = imdb_votes

        blocked = _blocked_in_db(session, title.id, disc.season, top_up=disc.top_up)
        if blocked is not None:
            stats.live_skipped += 1
            return

        failed = _latest_failed(session, title.id, disc.season)
        if failed is not None and not retry_due(failed):
            stats.backoff_skipped += 1
            return

        if disc.skip_filter:
            # Watchlist / new season: the human already chose it — no threshold gate.
            base_reason = disc.reason or "on your watchlist"
        else:
            outcome = evaluate(
                imdb_rating=imdb_rating,
                imdb_votes=imdb_votes,
                tmdb_rating=details.tmdb_rating,
                tmdb_votes=details.tmdb_votes,
                genres=details.genres,
                thresholds=config.thresholds.for_kind(disc.kind.value),
                excluded_genres=config.discovery.excluded_genres,
            )
            if not outcome.passed:
                stats.filtered += 1
                return
            base_reason = outcome.reason

        feats = extract_features(title)
        quality = score(
            imdb_rating,
            imdb_votes,
            details.popularity,
            tmdb_rating=details.tmdb_rating,
            tmdb_votes=details.tmdb_votes,
        )
        reason = f"{base_reason}; via {disc.source}"

        index = scorers.taste_index(disc.kind)
        if index is not None:
            sim = index.similarity(feats, k=taste_cfg.neighbors)
            feats["taste"] = {"score": sim.score, "like": sim.like}
            quality = round(quality + taste_cfg.weight * 10 * sim.score, 3)
            if sim.like:
                reason += f"; taste {sim.score:.2f} (like: {', '.join(sim.like[:3])})"

        p_like = scorers.p_like(feats)
        if p_like is not None:
            feats["model"] = {"p_like": p_like}
            quality = round(quality + taste_cfg.model_weight * 10 * p_like, 3)
            reason += f"; model {p_like:.0%}"

        if disc.season is not None:
            feats["season"] = disc.season
            if disc.season_episodes:
                feats["season_episodes"] = disc.season_episodes
        if auto:
            feats["auto"] = True  # approved by rule: no label for the preference model

        status = CandidateStatus.approved if auto else CandidateStatus.new
        decided_at = utcnow() if auto else None
        if failed is not None:
            retries = int((failed.features or {}).get(_RETRIES_KEY, 0)) + 1
            feats[_RETRIES_KEY] = retries
            failed.status = status
            failed.reason = reason
            failed.score = quality
            failed.features = feats
            failed.decided_at = decided_at
            stats.retried += 1
            return

        session.add(
            Candidate(
                title_id=title.id,
                season=disc.season,
                source=disc.origin,
                status=status,
                reason=reason,
                score=quality,
                features=feats,
                decided_at=decided_at,
            )
        )
        stats.created += 1


def _persist(enriched: list[_Enriched], config: AppConfig, stats: DiscoveryStats) -> None:
    scorers = _Scorers(config)
    for item in enriched:
        if item.error or item.details is None:
            if item.error:
                stats.errors.append(f"{item.disc.tmdb.title}: {item.error}")
            continue
        try:
            _persist_one(item, config, scorers, stats)
        except Exception as exc:  # one bad row must not lose the others
            log.warning(
                "discovery.persist_failed", title=item.disc.tmdb.title, error=redact_exc(exc)
            )
            stats.errors.append(f"{item.disc.tmdb.title}: {redact_exc(exc)}")


async def _fetch_source(
    source: CandidateSource, tmdb: TMDbClient, limit: int, stats: DiscoveryStats
) -> list[Discovered]:
    """One source's items. A source that fails costs only its own items."""

    try:
        return await source.fetch(tmdb, limit)
    except Exception as exc:
        error = redact_exc(exc)
        log.warning("discovery.source_failed", source=source.name, error=error)
        stats.errors.append(f"{source.name}: {error}")
        stats.sources_failed += 1
        return []


async def run_discovery(config: AppConfig) -> DiscoveryStats:
    """Run discovery under the cross-process job lock, so a manual "discover more"
    can't race the scheduled run (double TMDb quota, duplicate candidate inserts).
    Raises JobBusyError if a discovery run is already in progress elsewhere."""

    from ..locks import job_lock

    with job_lock(config, "discovery"):
        return await _run_discovery(config)


async def _run_discovery(config: AppConfig) -> DiscoveryStats:
    """Run discovery across configured sources. Records a ``discovery`` job_run."""

    secrets = config.secrets
    if secrets.tmdb_api_key is None:
        raise NotConfiguredError("TMDB_API_KEY is not set in .env; discovery needs it.")

    with session_scope() as session:
        run = JobRun(kind="discovery", started_at=utcnow(), status=RunStatus.running)
        session.add(run)
        session.flush()
        run_id = run.id

    bind_run(run_id=run_id, job="discovery")
    stats = DiscoveryStats()
    status = RunStatus.success
    try:
        sources = build_sources(config.discovery, config.secrets)
        stats.sources = len(sources)
        log.info("discovery.start", sources=[s.name for s in sources])

        if sources:
            sem = asyncio.Semaphore(config.metadata.max_concurrency)
            async with httpx.AsyncClient(timeout=15.0) as http:
                tmdb = TMDbClient(
                    secrets.tmdb_api_key.get_secret_value(),
                    http,
                    language=config.metadata.language,
                    cache_days=config.metadata.cache_days,
                )
                omdb = (
                    OMDbClient(
                        secrets.omdb_api_key.get_secret_value(),
                        http,
                        cache_days=config.metadata.cache_days,
                    )
                    if secrets.omdb_api_key is not None
                    else None
                )
                if omdb is None:
                    log.warning(
                        "discovery.no_omdb",
                        detail="OMDB_API_KEY unset; falling back to TMDb ratings",
                    )

                fetched_lists = await asyncio.gather(
                    *(
                        _fetch_source(s, tmdb, config.discovery.max_per_source, stats)
                        for s in sources
                    )
                )
                discovered = [d for lst in fetched_lists for d in lst]
                stats.fetched = len(discovered)

                # Dedup by (kind, tmdb_id, season), keeping the first source that
                # surfaced it. Season-scoped items dedup independently of the
                # title-level hit for the same show.
                seen: dict[tuple[TitleKind, int, int | None], Discovered] = {}
                for d in discovered:
                    seen.setdefault((d.kind, d.tmdb.tmdb_id, d.season), d)
                stats.deduped = len(seen)

                owned, live, rejected = _owned_live_rejected()
                to_consider: list[Discovered] = []
                for (kind, tid, season), d in seen.items():
                    if season is not None:
                        # New-season items are owned titles by definition; their
                        # per-season live/rejected check happens in _blocked_in_db.
                        to_consider.append(d)
                    elif (kind, tid) in owned:
                        stats.owned_skipped += 1
                    elif (kind, tid) in live:
                        stats.live_skipped += 1
                    elif (kind, tid) in rejected:
                        stats.rejected_skipped += 1
                    else:
                        to_consider.append(d)
                stats.considered = len(to_consider)

                enriched = await asyncio.gather(*(_enrich(d, tmdb, omdb, sem) for d in to_consider))
            _persist(list(enriched), config, stats)
            if stats.sources_failed == len(sources):
                status = RunStatus.failed
            elif stats.errors:
                status = RunStatus.partial
        log.info("discovery.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(redact_exc(exc))
        log.error("discovery.failed", error=redact_exc(exc))
    finally:
        with session_scope() as session:
            job_run = session.get(JobRun, run_id)
            if job_run is not None:
                job_run.finished_at = utcnow()
                job_run.status = status
                job_run.stats = stats.as_dict()
        clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Discovery failed: {stats.errors[:3]}")
    return stats
