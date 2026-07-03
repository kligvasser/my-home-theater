"""Metadata enrichment (plan §5.3).

Backfills TMDb/IMDb ids, ratings/votes, genres, and other details onto the titles
the scanner created. Network fetches run concurrently (bounded by a semaphore);
DB reads and writes happen in short transactions around the I/O so we never hold a
transaction open across network calls.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import Genre, JobRun, RunStatus, Title, TitleKind
from ..db.session import session_scope
from ..logging_setup import bind_run, clear_run, get_logger
from .dto import OmdbRatings, TmdbTitle
from .omdb import OMDbClient
from .tmdb import TMDbClient

log = get_logger(__name__)


@dataclass
class EnrichStats:
    titles_considered: int = 0
    ids_resolved: int = 0
    details_updated: int = 0
    ratings_updated: int = 0
    unmatched: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Snapshot:
    id: int
    title: str
    year: int | None
    kind: TitleKind
    tmdb_id: int | None
    imdb_id: str | None


@dataclass
class _Result:
    snapshot: _Snapshot
    tmdb: TmdbTitle | None = None
    ratings: OmdbRatings | None = None
    error: str | None = None


def _load_pending() -> list[_Snapshot]:
    """Titles missing a TMDb id or an IMDb rating are candidates for enrichment."""

    with session_scope() as session:
        rows = session.scalars(
            select(Title).where(or_(Title.tmdb_id.is_(None), Title.imdb_rating.is_(None)))
        ).all()
        return [_Snapshot(t.id, t.title, t.year, t.kind, t.tmdb_id, t.imdb_id) for t in rows]


async def _enrich_one(
    snap: _Snapshot,
    tmdb: TMDbClient,
    omdb: OMDbClient | None,
    sem: asyncio.Semaphore,
) -> _Result:
    async with sem:
        try:
            tmdb_id = snap.tmdb_id or await tmdb.search(snap.title, snap.year, snap.kind)
            details = await tmdb.details(tmdb_id, snap.kind) if tmdb_id else None
            imdb_id = (details.imdb_id if details else None) or snap.imdb_id

            ratings: OmdbRatings | None = None
            if omdb is not None and imdb_id:
                ratings = await omdb.by_imdb_id(imdb_id)
            return _Result(snapshot=snap, tmdb=details, ratings=ratings)
        except Exception as exc:  # keep enriching the rest
            log.warning("enrich.title_failed", title=snap.title, error=str(exc))
            return _Result(snapshot=snap, error=str(exc))


def _get_or_create_genre(session: Session, name: str) -> Genre:
    genre: Genre | None = session.scalar(select(Genre).where(Genre.name == name))
    if genre is None:
        genre = Genre(name=name)
        session.add(genre)
        session.flush()
    return genre


def _persist(results: list[_Result], stats: EnrichStats) -> None:
    with session_scope() as session:
        for res in results:
            if res.error:
                stats.errors.append(f"{res.snapshot.title}: {res.error}")
                continue
            title = session.get(Title, res.snapshot.id)
            if title is None:
                continue

            if res.tmdb is not None:
                t = res.tmdb
                if title.tmdb_id is None and t.tmdb_id:
                    stats.ids_resolved += 1
                title.tmdb_id = t.tmdb_id
                title.imdb_id = t.imdb_id or title.imdb_id
                title.year = title.year or t.year
                title.runtime = t.runtime
                title.tmdb_rating = t.tmdb_rating
                title.tmdb_votes = t.tmdb_votes
                title.popularity = t.popularity
                title.poster_url = t.poster_url
                title.overview = t.overview
                if t.genres:
                    title.genres = [_get_or_create_genre(session, g) for g in t.genres]
                stats.details_updated += 1
            else:
                stats.unmatched += 1

            if res.ratings is not None and (
                res.ratings.imdb_rating is not None or res.ratings.imdb_votes is not None
            ):
                title.imdb_rating = res.ratings.imdb_rating
                title.imdb_votes = res.ratings.imdb_votes
                stats.ratings_updated += 1


async def enrich_catalog(config: AppConfig) -> EnrichStats:
    """Enrich all pending titles. Returns run statistics; records a ``job_run``."""

    secrets = config.secrets
    if secrets.tmdb_api_key is None:
        raise ValueError("TMDB_API_KEY is not set in .env; metadata enrichment needs it.")

    with session_scope() as session:
        run = JobRun(kind="metadata", started_at=utcnow(), status=RunStatus.running)
        session.add(run)
        session.flush()
        run_id = run.id

    bind_run(run_id=run_id, job="metadata")
    stats = EnrichStats()
    status = RunStatus.success
    try:
        pending = _load_pending()
        stats.titles_considered = len(pending)
        log.info("enrich.start", pending=len(pending))

        if pending:
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
                    log.warning("enrich.no_omdb", detail="OMDB_API_KEY unset; skipping ratings")
                results = await asyncio.gather(*(_enrich_one(s, tmdb, omdb, sem) for s in pending))
            _persist(list(results), stats)
        log.info("enrich.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(str(exc))
        log.error("enrich.failed", error=str(exc))
    finally:
        with session_scope() as session:
            job_run = session.get(JobRun, run_id)
            if job_run is not None:
                job_run.finished_at = utcnow()
                job_run.status = status
                job_run.stats = stats.as_dict()
        clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Enrichment failed: {stats.errors}")
    return stats
