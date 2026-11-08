"""Candidate review actions (approve / reject / manual add).

These mutate state, so the API gates them behind the dashboard token. The status
transitions are guarded: once a candidate is queued/downloading/imported, the
dashboard buttons can't silently contradict what Radarr/Sonarr are doing.
"""

from __future__ import annotations

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import (
    Candidate,
    CandidateSource,
    CandidateStatus,
    GrabRetry,
    Title,
    TitleKind,
)
from ..db.session import session_scope
from ..errors import (
    InvalidTransitionError,
    NotConfiguredError,
    TransientProviderError,
    redact_exc,
)
from ..features import extract_features
from ..logging_setup import get_logger
from ..metadata.omdb import OMDbClient
from ..metadata.tmdb import TMDbClient
from .service import _upsert_title

log = get_logger(__name__)

# from-status -> statuses a dashboard action may set
_ALLOWED: dict[CandidateStatus, tuple[CandidateStatus, ...]] = {
    CandidateStatus.new: (CandidateStatus.approved, CandidateStatus.rejected),
    CandidateStatus.approved: (CandidateStatus.rejected,),
    CandidateStatus.failed: (CandidateStatus.approved, CandidateStatus.rejected),
    CandidateStatus.cancelled: (CandidateStatus.approved, CandidateStatus.rejected),
}


def _set_status(candidate_id: int, status: CandidateStatus) -> bool:
    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is None:
            return False
        if cand.status == status:  # idempotent re-click
            return True
        if status not in _ALLOWED.get(cand.status, ()):
            raise InvalidTransitionError(
                f"candidate {candidate_id} is {cand.status.value}; "
                f"cannot change it to {status.value} from the dashboard"
            )
        cand.status = status
        cand.decided_at = utcnow()
        if status is CandidateStatus.approved:
            # A person saying "go" overrides any retry backoff.
            s.execute(delete(GrabRetry).where(GrabRetry.candidate_id == candidate_id))
        return True


def approve(candidate_id: int) -> bool:
    """Mark a candidate approved (the acquire sweep hands it to Radarr/Sonarr)."""

    return _set_status(candidate_id, CandidateStatus.approved)


def reject(candidate_id: int) -> bool:
    return _set_status(candidate_id, CandidateStatus.rejected)


async def add_manual(
    config: AppConfig, tmdb_id: int, kind: TitleKind, *, reuse_existing: bool = False
) -> int:
    """Manually add a candidate by TMDb id: fetch details, upsert title, queue it.

    Returns the new candidate id. Raises if the TMDb id doesn't exist for that
    kind, or (unless ``reuse_existing``) if the title already has a live
    candidate — with ``reuse_existing`` the existing candidate's id is returned
    instead, so a one-click "grab from search" is idempotent.
    """

    secrets = config.secrets
    if secrets.tmdb_api_key is None:
        raise NotConfiguredError("TMDB_API_KEY is not set in .env.")

    async with httpx.AsyncClient(timeout=15.0) as http:
        tmdb = TMDbClient(
            secrets.tmdb_api_key.get_secret_value(),
            http,
            language=config.metadata.language,
            cache_days=config.metadata.cache_days,
        )
        try:
            details = await tmdb.details(tmdb_id, kind)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"TMDb has no {kind.value} with id {tmdb_id}") from exc
            raise
        ratings = None
        if secrets.omdb_api_key is not None and details.imdb_id:
            omdb = OMDbClient(
                secrets.omdb_api_key.get_secret_value(), http, cache_days=config.metadata.cache_days
            )
            # Ratings are a bonus here: an OMDb outage or spent quota mustn't lose the add.
            try:
                ratings = await omdb.by_imdb_id(details.imdb_id)
            except (TransientProviderError, httpx.HTTPError) as exc:
                log.warning("manual_add.omdb_skipped", error=redact_exc(exc))

    with session_scope() as session:
        title = _upsert_title(session, kind, details)
        if ratings is not None:
            if ratings.imdb_rating is not None:
                title.imdb_rating = ratings.imdb_rating
            if ratings.imdb_votes is not None:
                title.imdb_votes = ratings.imdb_votes

        existing = session.scalar(
            select(Candidate).where(
                Candidate.title_id == title.id,
                Candidate.status.in_(
                    (
                        CandidateStatus.new,
                        CandidateStatus.approved,
                        CandidateStatus.queued,
                        CandidateStatus.downloading,
                    )
                ),
            )
        )
        if existing is not None:
            if reuse_existing:
                return existing.id
            raise ValueError(f"'{title.title}' already has a live candidate.")

        cand = Candidate(
            title_id=title.id,
            source=CandidateSource.manual,
            status=CandidateStatus.new,
            reason="manually added",
            features=extract_features(title),
        )
        session.add(cand)
        session.flush()
        return cand.id


def _reopen_cancelled(session: Session, title_id: int) -> None:
    """Following says "I want this show": what an unfollow or cancel stopped is wanted
    again. Rejected candidates stay rejected; that's a different statement."""

    for cand in session.scalars(
        select(Candidate).where(
            Candidate.title_id == title_id, Candidate.status == CandidateStatus.cancelled
        )
    ).all():
        cand.status = CandidateStatus.approved
        cand.decided_at = utcnow()


async def follow_series(config: AppConfig, tmdb_id: int, kind: TitleKind = TitleKind.series) -> int:
    """Mark a series as followed: upsert its Title from TMDb and set ``followed``.

    Following auto-grabs new seasons/episodes as they air (the new-seasons
    discovery source treats followed shows as auto-grab), and reopens seasons
    cancelled earlier, which discovery alone would leave blocked. Returns the title id.
    Only series can be followed — movies don't have new episodes.
    """

    if kind is not TitleKind.series:
        raise ValueError("Only series can be followed.")
    secrets = config.secrets
    if secrets.tmdb_api_key is None:
        raise NotConfiguredError("TMDB_API_KEY is not set in .env.")

    async with httpx.AsyncClient(timeout=15.0) as http:
        tmdb = TMDbClient(
            secrets.tmdb_api_key.get_secret_value(),
            http,
            language=config.metadata.language,
            cache_days=config.metadata.cache_days,
        )
        try:
            details = await tmdb.details(tmdb_id, kind)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(f"TMDb has no series with id {tmdb_id}") from exc
            raise

    with session_scope() as session:
        title = _upsert_title(session, kind, details)
        title.followed = True
        session.flush()
        _reopen_cancelled(session, title.id)
        return title.id


def set_followed(tmdb_id: int, kind: TitleKind, followed: bool) -> bool:
    """Toggle ``followed`` on an existing title (by tmdb id + kind). Returns True
    if a title was found and updated."""

    with session_scope() as session:
        title = session.scalar(select(Title).where(Title.tmdb_id == tmdb_id, Title.kind == kind))
        if title is None:
            return False
        title.followed = followed
        if followed:
            _reopen_cancelled(session, title.id)
        return True
