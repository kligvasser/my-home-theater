"""Discovery orchestration (plan §5.4): gather -> dedup -> enrich -> filter -> rank.

Writes ranked ``candidate`` rows with human-readable reasons, excluding titles you
already own or already have a live candidate for. Honors ``auto_approve``.
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
    Genre,
    JobRun,
    OwnedFile,
    RunStatus,
    Title,
    TitleKind,
)
from ..db.session import session_scope
from ..logging_setup import bind_run, clear_run, get_logger
from ..metadata.dto import OmdbRatings, TmdbTitle
from ..metadata.omdb import OMDbClient
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


@dataclass
class DiscoveryStats:
    sources: int = 0
    fetched: int = 0
    deduped: int = 0
    owned_skipped: int = 0
    live_skipped: int = 0
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


def _owned_and_live() -> tuple[set[int], set[int]]:
    """TMDb ids that are already owned, and that already have a live candidate."""

    with session_scope() as s:
        owned = {
            tid
            for (tid,) in s.execute(
                select(Title.tmdb_id).where(
                    Title.tmdb_id.is_not(None),
                    exists().where(OwnedFile.title_id == Title.id),
                )
            ).all()
        }
        live = {
            tid
            for (tid,) in s.execute(
                select(Title.tmdb_id).where(
                    Title.tmdb_id.is_not(None),
                    exists().where(
                        (Candidate.title_id == Title.id) & (Candidate.status.in_(LIVE_STATUSES))
                    ),
                )
            ).all()
        }
        return owned, live


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
            log.warning("discovery.enrich_failed", title=disc.tmdb.title, error=str(exc))
            return _Enriched(disc, None, None, error=str(exc))


def _get_or_create_genre(session: Session, name: str) -> Genre:
    genre: Genre | None = session.scalar(select(Genre).where(Genre.name == name))
    if genre is None:
        genre = Genre(name=name)
        session.add(genre)
        session.flush()
    return genre


def _upsert_title(session: Session, kind: TitleKind, t: TmdbTitle) -> Title:
    title = session.scalar(select(Title).where(Title.tmdb_id == t.tmdb_id))
    if title is None:
        title = Title(tmdb_id=t.tmdb_id, kind=kind, title=t.title)
        session.add(title)
    title.kind = kind
    title.title = t.title or title.title
    title.imdb_id = t.imdb_id or title.imdb_id
    title.tvdb_id = t.tvdb_id or title.tvdb_id
    title.year = t.year or title.year
    title.runtime = t.runtime
    title.tmdb_rating = t.tmdb_rating
    title.tmdb_votes = t.tmdb_votes
    title.popularity = t.popularity
    title.poster_url = t.poster_url
    title.overview = t.overview
    if t.genres:
        title.genres = [_get_or_create_genre(session, g) for g in t.genres]
    session.flush()
    return title


def _persist(enriched: list[_Enriched], config: AppConfig, stats: DiscoveryStats) -> None:
    thresholds = config.thresholds
    excluded = config.discovery.excluded_genres
    auto = config.features.auto_approve

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

            outcome = evaluate(
                imdb_rating=imdb_rating,
                imdb_votes=imdb_votes,
                tmdb_votes=details.tmdb_votes,
                genres=details.genres,
                thresholds=thresholds,
                excluded_genres=excluded,
            )
            if not outcome.passed:
                stats.filtered += 1
                continue

            reason = f"{outcome.reason}; via {item.disc.source}"
            session.add(
                Candidate(
                    title_id=title.id,
                    source=CandidateSource.discovery,
                    status=CandidateStatus.approved if auto else CandidateStatus.new,
                    reason=reason,
                    score=score(imdb_rating, imdb_votes, details.popularity),
                    decided_at=utcnow() if auto else None,
                )
            )
            stats.created += 1


async def run_discovery(config: AppConfig) -> DiscoveryStats:
    """Run discovery across configured sources. Records a ``discovery`` job_run."""

    secrets = config.secrets
    if secrets.tmdb_api_key is None:
        raise ValueError("TMDB_API_KEY is not set in .env; discovery needs it.")

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

                fetched_lists = await asyncio.gather(
                    *(s.fetch(tmdb, config.discovery.max_per_source) for s in sources)
                )
                discovered = [d for lst in fetched_lists for d in lst]
                stats.fetched = len(discovered)

                # Dedup by (kind, tmdb_id), keeping the first source that surfaced it.
                seen: dict[tuple[TitleKind, int], Discovered] = {}
                for d in discovered:
                    seen.setdefault((d.kind, d.tmdb.tmdb_id), d)
                stats.deduped = len(seen)

                owned, live = _owned_and_live()
                to_consider: list[Discovered] = []
                for d in seen.values():
                    if d.tmdb.tmdb_id in owned:
                        stats.owned_skipped += 1
                    elif d.tmdb.tmdb_id in live:
                        stats.live_skipped += 1
                    else:
                        to_consider.append(d)
                stats.considered = len(to_consider)

                enriched = await asyncio.gather(*(_enrich(d, tmdb, omdb, sem) for d in to_consider))
            _persist(list(enriched), config, stats)
        log.info("discovery.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(str(exc))
        log.error("discovery.failed", error=str(exc))
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
