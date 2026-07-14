"""Metadata enrichment (plan §5.3).

Backfills TMDb/IMDb ids, ratings/votes, genres, and taste features onto the
titles the scanner created. Network fetches run concurrently (bounded by a
semaphore); DB reads and writes happen in short transactions around the I/O so
we never hold a transaction open across network calls.

Persistence is per-title: one bad row (e.g. two scanner titles resolving to the
same TMDb id) merges or fails alone instead of rolling back the whole batch.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import Candidate, Genre, JobRun, OwnedFile, RunStatus, Subtitle, Title, TitleKind
from ..db.session import session_scope
from ..errors import NotConfiguredError, redact, redact_exc
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
    merged: int = 0
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


def _load_pending(retry_days: int, force: bool = False) -> list[_Snapshot]:
    """Titles missing a TMDb id or an IMDb rating, not attempted recently.

    ``last_enriched_at`` keeps unresolvable titles (bad filenames, titles OMDb
    doesn't know) from being re-enqueued on every single run. ``force`` drops
    that time guard — the escape hatch for "I just configured a new provider
    (e.g. OMDb) and want the still-incomplete titles backfilled now".
    """

    where = [or_(Title.tmdb_id.is_(None), Title.imdb_rating.is_(None))]
    if not force:
        cutoff = utcnow() - timedelta(days=max(retry_days, 1))
        where.append(or_(Title.last_enriched_at.is_(None), Title.last_enriched_at < cutoff))
    with session_scope() as session:
        rows = session.scalars(select(Title).where(*where)).all()
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
                # A ratings failure (bad key, quota, outage) must not discard the
                # TMDb details + features we just fetched — ratings stay None.
                try:
                    ratings = await omdb.by_imdb_id(imdb_id)
                except Exception as exc:
                    log.warning("enrich.omdb_failed", title=snap.title, error=redact_exc(exc))
            return _Result(snapshot=snap, tmdb=details, ratings=ratings)
        except Exception as exc:  # keep enriching the rest
            log.warning("enrich.title_failed", title=snap.title, error=redact_exc(exc))
            return _Result(snapshot=snap, error=redact_exc(exc))


def _get_or_create_genre(session: Session, name: str) -> Genre:
    genre: Genre | None = session.scalar(select(Genre).where(Genre.name == name))
    if genre is None:
        genre = Genre(name=name)
        session.add(genre)
        session.flush()
    return genre


def apply_tmdb_details(session: Session, title: Title, t: TmdbTitle) -> None:
    """Copy TMDb details (including taste/ML features) onto a Title row.

    Never sets ``tmdb_id``/``kind`` (identity is the caller's concern) and never
    clobbers an existing value with None. The imdb_id unique index is guarded:
    a conflicting id on another row is logged and skipped, not crashed on.
    """

    title.title = t.title or title.title
    if t.imdb_id and t.imdb_id != title.imdb_id:
        conflict = session.scalar(
            select(Title.id).where(Title.imdb_id == t.imdb_id, Title.id != title.id)
        )
        if conflict is None:
            title.imdb_id = t.imdb_id
        else:
            log.warning(
                "metadata.imdb_id_conflict", title=title.title, imdb_id=t.imdb_id, other=conflict
            )
    title.tvdb_id = t.tvdb_id or title.tvdb_id
    title.year = title.year or t.year
    if t.runtime is not None:
        title.runtime = t.runtime
    if t.tmdb_rating is not None:
        title.tmdb_rating = t.tmdb_rating
    if t.tmdb_votes is not None:
        title.tmdb_votes = t.tmdb_votes
    if t.popularity is not None:
        title.popularity = t.popularity
    title.poster_url = t.poster_url or title.poster_url
    title.overview = t.overview or title.overview
    title.original_language = t.original_language or title.original_language
    if t.origin_countries:
        title.origin_countries = t.origin_countries
    title.release_date = t.release_date or title.release_date
    title.certification = t.certification or title.certification
    if t.keywords:
        title.keywords = t.keywords
    if t.cast_top:
        title.cast_top = t.cast_top
    if t.directors:
        title.directors = t.directors
    if t.collection_tmdb_id is not None:
        title.collection_tmdb_id = t.collection_tmdb_id
        title.collection_name = t.collection_name
    if t.seasons_count is not None:
        title.seasons_count = t.seasons_count
    if t.episodes_count is not None:
        title.episodes_count = t.episodes_count
    title.series_status = t.series_status or title.series_status
    if t.genres:
        title.genres = [_get_or_create_genre(session, g) for g in t.genres]


def _merge_title(session: Session, dup: Title, canonical: Title) -> None:
    """Re-point a duplicate title's children to the canonical row and delete it."""

    for model in (OwnedFile, Candidate, Subtitle):
        session.execute(update(model).where(model.title_id == dup.id).values(title_id=canonical.id))
    session.expire(dup)
    session.delete(dup)


def _apply_ratings(title: Title, ratings: OmdbRatings | None, stats: EnrichStats) -> None:
    if ratings is not None and (ratings.imdb_rating is not None or ratings.imdb_votes is not None):
        title.imdb_rating = ratings.imdb_rating
        title.imdb_votes = ratings.imdb_votes
        stats.ratings_updated += 1


def _persist_one(res: _Result, stats: EnrichStats) -> None:
    """Apply one enrichment result in its own transaction."""

    with session_scope() as session:
        title = session.get(Title, res.snapshot.id)
        if title is None:
            return
        if res.error:
            stats.errors.append(f"{res.snapshot.title}: {redact(res.error)}")
            return
        title.last_enriched_at = utcnow()

        if res.tmdb is None:
            stats.unmatched += 1
            return

        t = res.tmdb
        # Two catalog rows resolving to the same (tmdb_id, kind) are the same
        # work (e.g. case-variant filenames): merge instead of violating the
        # unique index and sinking the run.
        canonical = session.scalar(
            select(Title).where(
                Title.tmdb_id == t.tmdb_id, Title.kind == title.kind, Title.id != title.id
            )
        )
        if canonical is not None:
            _merge_title(session, dup=title, canonical=canonical)
            apply_tmdb_details(session, canonical, t)
            _apply_ratings(canonical, res.ratings, stats)
            canonical.last_enriched_at = utcnow()
            stats.merged += 1
            log.info("enrich.merged_duplicate", title=t.title, tmdb_id=t.tmdb_id)
            return

        if title.tmdb_id is None and t.tmdb_id:
            stats.ids_resolved += 1
        title.tmdb_id = t.tmdb_id
        apply_tmdb_details(session, title, t)
        stats.details_updated += 1
        _apply_ratings(title, res.ratings, stats)


def _persist(results: list[_Result], stats: EnrichStats) -> None:
    for res in results:
        try:
            _persist_one(res, stats)
        except Exception as exc:  # isolate: one bad row must not sink the batch
            log.warning("enrich.persist_failed", title=res.snapshot.title, error=redact_exc(exc))
            stats.errors.append(f"{res.snapshot.title}: {redact_exc(exc)}")


async def enrich_catalog(config: AppConfig, *, force: bool = False) -> EnrichStats:
    """Enrich all pending titles. Returns run statistics; records a ``job_run``.

    ``force`` re-attempts every title still missing data, ignoring the
    ``last_enriched_at`` retry guard (use after adding a provider like OMDb).
    """

    secrets = config.secrets
    if secrets.tmdb_api_key is None:
        raise NotConfiguredError("TMDB_API_KEY is not set in .env; metadata enrichment needs it.")

    with session_scope() as session:
        run = JobRun(kind="metadata", started_at=utcnow(), status=RunStatus.running)
        session.add(run)
        session.flush()
        run_id = run.id

    bind_run(run_id=run_id, job="metadata")
    stats = EnrichStats()
    status = RunStatus.success
    try:
        pending = _load_pending(retry_days=config.metadata.cache_days, force=force)
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
        stats.errors.append(redact_exc(exc))
        log.error("enrich.failed", error=redact_exc(exc))
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
