"""Candidate review actions (approve / reject / manual add).

These mutate state, so the API gates them behind the dashboard token. The status
transitions are guarded: once a candidate is queued/downloading/imported, the
dashboard buttons can't silently contradict what Radarr/Sonarr are doing.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import Candidate, CandidateSource, CandidateStatus, TitleKind
from ..db.session import session_scope
from ..errors import InvalidTransitionError, NotConfiguredError
from ..features import extract_features
from ..metadata.omdb import OMDbClient
from ..metadata.tmdb import TMDbClient
from .service import _upsert_title

# from-status -> statuses a dashboard action may set
_ALLOWED: dict[CandidateStatus, tuple[CandidateStatus, ...]] = {
    CandidateStatus.new: (CandidateStatus.approved, CandidateStatus.rejected),
    CandidateStatus.approved: (CandidateStatus.rejected,),
    CandidateStatus.failed: (CandidateStatus.approved, CandidateStatus.rejected),
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
        return True


def approve(candidate_id: int) -> bool:
    """Mark a candidate approved (the acquire sweep hands it to Radarr/Sonarr)."""

    return _set_status(candidate_id, CandidateStatus.approved)


def reject(candidate_id: int) -> bool:
    return _set_status(candidate_id, CandidateStatus.rejected)


async def add_manual(config: AppConfig, tmdb_id: int, kind: TitleKind) -> int:
    """Manually add a candidate by TMDb id: fetch details, upsert title, queue it.

    Returns the new candidate id. Raises if the title already has a live candidate
    or the TMDb id doesn't exist for that kind.
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
            ratings = await omdb.by_imdb_id(details.imdb_id)

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
