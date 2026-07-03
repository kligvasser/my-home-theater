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
from typing import Any

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import (
    Candidate,
    CandidateSource,
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
from .sources import Discovered, build_sources

log = get_logger(__name__)

LIVE_STATUSES = (
    CandidateStatus.new,
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
)

# A rejected title stays rejected: trending will resurface it forever otherwise,
# and the rejection is a training label we must not bury under duplicates.
BLOCKING_STATUSES = (*LIVE_STATUSES, CandidateStatus.rejected)

_Key = tuple[TitleKind, int]


@dataclass
class DiscoveryStats:
    sources: int = 0
    fetched: int = 0
    deduped: int = 0
    owned_skipped: int = 0
    live_skipped: int = 0
    rejected_skipped: int = 0
    considered: int = 0
    created: int = 0
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

        owned = _ids(
            exists().where(OwnedFile.title_id == Title.id) | Title.arr_has_file.is_(True)
        )
        live = _ids(
            exists().where(
                (Candidate.title_id == Title.id) & (Candidate.status.in_(LIVE_STATUSES))
            )
        )
        rejected = _ids(
            exists().where(
                (Candidate.title_id == Title.id)
                & (Candidate.status == CandidateStatus.rejected)
            )
        )
        return owned, live, rejected


async def _enrich(
    disc: Discovered, tmdb: TMDbClient, omdb: OMDbClient | None, sem: asyncio.Semaphore
) -> _Enriched:
    async with sem:
        try:
            details = await tmdb.details(disc.tmdb.tmdb_id, disc.kind)
            ratings = (
                await omdb.by_imdb_id(details.imdb_id)
                if omdb is not None and details.imdb_id
                else None
            )
            return _Enriched(disc, details, ratings)
        except Exception as exc:
            log.warning("discovery.enrich_failed", title=disc.tmdb.title, error=redact_exc(exc))
            return _Enriched(disc, None, None, error=redact_exc(exc))


def _upsert_title(session: Session, kind: TitleKind, t: TmdbTitle) -> Title:
    """Get-or-create by (tmdb_id, kind) — never flips an existing row's kind."""

    title = session.scalar(
        select(Title).where(Title.tmdb_id == t.tmdb_id, Title.kind == kind)
    )
    if title is None:
        title = Title(tmdb_id=t.tmdb_id, kind=kind, title=t.title)
        session.add(title)
    apply_tmdb_details(session, title, t)
    session.flush()
    return title


def _blocked_in_db(session: Session, title_id: int) -> str | None:
    """Re-check owned/live/rejected inside the write transaction (TOCTOU guard)."""

    status = session.scalar(
        select(Candidate.status).where(
            Candidate.title_id == title_id, Candidate.status.in_(BLOCKING_STATUSES)
        )
    )
    if status is not None:
        return f"candidate already {status.value}"
    owned = session.scalar(select(OwnedFile.id).where(OwnedFile.title_id == title_id))
    if owned is not None:
        return "already owned"
    return None


def _persist(enriched: list[_Enriched], config: AppConfig, stats: DiscoveryStats) -> None:
    excluded = config.discovery.excluded_genres
    auto = config.features.auto_approve
    taste_cfg = config.taste

    # Lazy per-kind taste index over the owned library (content similarity —
    # sklearn import deferred so discovery without taste stays light).
    indexes: dict[TitleKind, Any] = {}

    def _taste_index(kind: TitleKind) -> Any:
        if kind not in indexes:
            if taste_cfg.enabled:
                from ..taste import build_index

                indexes[kind] = build_index(kind, min_library=taste_cfg.min_library)
            else:
                indexes[kind] = None
        return indexes[kind]

    with session_scope() as session:
        for item in enriched:
            if item.error or item.details is None:
                if item.error:
                    stats.errors.append(f"{item.disc.tmdb.title}: {item.error}")
                continue

            details = item.details
            imdb_rating = item.ratings.imdb_rating if item.ratings else None
            imdb_votes = item.ratings.imdb_votes if item.ratings else None

            title = _upsert_title(session, item.disc.kind, details)
            if imdb_rating is not None:
                title.imdb_rating = imdb_rating
            if imdb_votes is not None:
                title.imdb_votes = imdb_votes

            blocked = _blocked_in_db(session, title.id)
            if blocked is not None:
                stats.live_skipped += 1
                continue

            outcome = evaluate(
                imdb_rating=imdb_rating,
                imdb_votes=imdb_votes,
                tmdb_rating=details.tmdb_rating,
                tmdb_votes=details.tmdb_votes,
                genres=details.genres,
                thresholds=config.thresholds.for_kind(item.disc.kind.value),
                excluded_genres=excluded,
            )
            if not outcome.passed:
                stats.filtered += 1
                continue

            feats = extract_features(title)
            quality = score(
                imdb_rating,
                imdb_votes,
                details.popularity,
                tmdb_rating=details.tmdb_rating,
                tmdb_votes=details.tmdb_votes,
            )
            reason = f"{outcome.reason}; via {item.disc.source}"

            index = _taste_index(item.disc.kind)
            if index is not None:
                sim = index.similarity(feats, k=taste_cfg.neighbors)
                feats["taste"] = {"score": sim.score, "like": sim.like}
                quality = round(quality + taste_cfg.weight * 10 * sim.score, 3)
                if sim.like:
                    reason += f"; taste {sim.score:.2f} (like: {', '.join(sim.like[:3])})"

            session.add(
                Candidate(
                    title_id=title.id,
                    source=CandidateSource.discovery,
                    status=CandidateStatus.approved if auto else CandidateStatus.new,
                    reason=reason,
                    score=quality,
                    features=feats,
                    decided_at=utcnow() if auto else None,
                )
            )
            stats.created += 1


async def run_discovery(config: AppConfig) -> DiscoveryStats:
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
        sources = build_sources(config.discovery)
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
                    *(s.fetch(tmdb, config.discovery.max_per_source) for s in sources)
                )
                discovered = [d for lst in fetched_lists for d in lst]
                stats.fetched = len(discovered)

                # Dedup by (kind, tmdb_id), keeping the first source that surfaced it.
                seen: dict[_Key, Discovered] = {}
                for d in discovered:
                    seen.setdefault((d.kind, d.tmdb.tmdb_id), d)
                stats.deduped = len(seen)

                owned, live, rejected = _owned_live_rejected()
                to_consider: list[Discovered] = []
                for (kind, tid), d in seen.items():
                    if (kind, tid) in owned:
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
        raise RuntimeError(f"Discovery failed: {stats.errors}")
    return stats
