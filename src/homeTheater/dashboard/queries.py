"""Read-only catalog queries for the dashboard (plan §5.8, §9).

Everything returns plain dataclasses built inside a session so templates never
touch detached ORM objects or trigger lazy loads after the session closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    Candidate,
    CandidateStatus,
    Genre,
    JobRun,
    OwnedFile,
    Title,
    TitleGenre,
    TitleKind,
)
from ..db.session import session_scope

DEFAULT_SUB_LANG = "he"
PAGE_SIZE = 40


@dataclass(frozen=True, slots=True)
class Coverage:
    lang: str
    covered: int
    total: int

    @property
    def pct(self) -> float:
        return round(100.0 * self.covered / self.total, 1) if self.total else 0.0


@dataclass
class LibraryStats:
    total_titles: int = 0
    movies: int = 0
    series: int = 0
    files: int = 0
    total_size_bytes: int = 0
    resolutions: list[tuple[str, int]] = field(default_factory=list)
    genres: list[tuple[str, int]] = field(default_factory=list)
    decades: list[tuple[int, int]] = field(default_factory=list)
    ratings: list[tuple[float, int]] = field(default_factory=list)  # 0.5-wide buckets
    languages: list[tuple[str, int]] = field(default_factory=list)
    avg_imdb: float | None = None
    coverage: Coverage = field(default_factory=lambda: Coverage(DEFAULT_SUB_LANG, 0, 0))


@dataclass(frozen=True, slots=True)
class TitleRow:
    id: int
    title: str
    year: int | None
    kind: str
    imdb_rating: float | None
    imdb_votes: int | None
    poster_url: str | None
    genres: list[str]
    owned_count: int
    resolutions: list[str]
    has_sub: bool
    overview: str | None = None
    added_at: str | None = None  # ISO date the catalog first saw it


# Library sort options: query-param value -> ORDER BY. "added" first: the
# dashboard's default is "what landed recently".
TITLE_SORTS = ("added", "rating", "title", "year")


@dataclass(frozen=True, slots=True)
class RunRow:
    id: int
    kind: str
    status: str
    started_at: str | None
    finished_at: str | None
    stats: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class CandidateRow:
    id: int
    title: str
    year: int | None
    kind: str
    status: str
    source: str
    reason: str | None
    score: float | None
    imdb_rating: float | None
    imdb_votes: int | None
    poster_url: str | None
    overview: str | None = None
    taste_score: float | None = None
    taste_like: list[str] = field(default_factory=list)


def get_stats(sub_lang: str = DEFAULT_SUB_LANG) -> LibraryStats:
    with session_scope() as s:
        stats = LibraryStats()
        stats.movies = s.scalar(select(func.count()).where(Title.kind == TitleKind.movie)) or 0
        stats.series = s.scalar(select(func.count()).where(Title.kind == TitleKind.series)) or 0
        stats.total_titles = stats.movies + stats.series
        stats.files = s.scalar(select(func.count()).select_from(OwnedFile)) or 0
        stats.total_size_bytes = (
            s.scalar(select(func.coalesce(func.sum(OwnedFile.size_bytes), 0))) or 0
        )

        stats.resolutions = [
            (res, cnt)
            for res, cnt in s.execute(
                select(OwnedFile.resolution, func.count())
                .where(OwnedFile.resolution.is_not(None))
                .group_by(OwnedFile.resolution)
                .order_by(func.count().desc())
            ).all()
        ]

        stats.genres = [
            (name, cnt)
            for name, cnt in s.execute(
                select(Genre.name, func.count(func.distinct(TitleGenre.title_id)))
                .join(TitleGenre, TitleGenre.genre_id == Genre.id)
                .group_by(Genre.name)
                .order_by(func.count(func.distinct(TitleGenre.title_id)).desc())
            ).all()
        ]

        # year - (year % 10) floors to the decade with exact integer math
        # (integer division isn't portable across SQLite/Postgres).
        decade = Title.year - (Title.year % 10)
        stats.decades = [
            (int(dec), cnt)
            for dec, cnt in s.execute(
                select(decade.label("decade"), func.count())
                .where(Title.year.is_not(None))
                .group_by("decade")
                .order_by("decade")
            ).all()
        ]

        # IMDb rating profile in 0.5-wide buckets (round(x*2)/2 is portable).
        bucket = (func.round(Title.imdb_rating * 2) / 2).label("bucket")
        stats.ratings = [
            (float(b), cnt)
            for b, cnt in s.execute(
                select(bucket, func.count())
                .where(Title.imdb_rating.is_not(None))
                .group_by("bucket")
                .order_by("bucket")
            ).all()
        ]
        stats.avg_imdb = s.scalar(
            select(func.round(func.avg(Title.imdb_rating), 2)).where(
                Title.imdb_rating.is_not(None)
            )
        )

        stats.languages = [
            (lang, cnt)
            for lang, cnt in s.execute(
                select(Title.original_language, func.count())
                .where(Title.original_language.is_not(None))
                .group_by(Title.original_language)
                .order_by(func.count().desc())
                .limit(10)
            ).all()
        ]

        stats.coverage = _coverage(s, sub_lang)
        return stats


def _coverage(session: Session, lang: str) -> Coverage:
    """% of owned titles that have at least one sidecar in ``lang``.

    ``subtitle_langs`` is a JSON list, so membership is computed in Python — fine
    for a home-scale library and avoids DB-specific JSON operators.
    """

    rows = session.execute(select(OwnedFile.title_id, OwnedFile.subtitle_langs)).all()
    owned: set[int] = set()
    covered: set[int] = set()
    for title_id, langs in rows:
        owned.add(title_id)
        if langs and lang in langs:
            covered.add(title_id)
    return Coverage(lang=lang, covered=len(covered), total=len(owned))


def _title_row(t: Title, sub_lang: str) -> TitleRow:
    resolutions = sorted({f.resolution for f in t.owned_files if f.resolution})
    has_sub = any(f.subtitle_langs and sub_lang in f.subtitle_langs for f in t.owned_files)
    return TitleRow(
        id=t.id,
        title=t.title,
        year=t.year,
        kind=str(t.kind),
        imdb_rating=t.imdb_rating,
        imdb_votes=t.imdb_votes,
        poster_url=t.poster_url,
        genres=[g.name for g in t.genres],
        owned_count=len(t.owned_files),
        resolutions=resolutions,
        has_sub=has_sub,
        overview=t.overview,
        added_at=t.created_at.date().isoformat() if t.created_at else None,
    )


def _title_order(sort: str | None) -> tuple[Any, ...]:
    if sort == "title":
        return (Title.title,)
    if sort == "year":
        return (Title.year.is_(None), Title.year.desc(), Title.title)
    if sort == "rating":
        return (Title.imdb_rating.is_(None), Title.imdb_rating.desc(), Title.title)
    # default: most recently added first
    return (Title.created_at.desc(), Title.id.desc())


def list_titles(
    q: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sub_lang: str = DEFAULT_SUB_LANG,
    sort: str | None = "added",
) -> tuple[list[TitleRow], int]:
    page = max(page, 1)
    with session_scope() as s:
        stmt = select(Title).options(selectinload(Title.genres), selectinload(Title.owned_files))
        count_stmt = select(func.count()).select_from(Title)
        if q:
            like = f"%{q}%"
            stmt = stmt.where(Title.title.ilike(like))
            count_stmt = count_stmt.where(Title.title.ilike(like))
        if kind in (TitleKind.movie, TitleKind.series):
            stmt = stmt.where(Title.kind == kind)
            count_stmt = count_stmt.where(Title.kind == kind)

        total = s.scalar(count_stmt) or 0
        stmt = stmt.order_by(*_title_order(sort)).limit(page_size).offset((page - 1) * page_size)
        rows = [_title_row(t, sub_lang) for t in s.scalars(stmt).all()]
        return rows, total


def recent_titles(limit: int = 12, sub_lang: str = DEFAULT_SUB_LANG) -> list[TitleRow]:
    """Most recently added *owned* titles — the dashboard's poster wall."""

    with session_scope() as s:
        stmt = (
            select(Title)
            .options(selectinload(Title.genres), selectinload(Title.owned_files))
            .where(Title.owned_files.any())
            .order_by(Title.created_at.desc(), Title.id.desc())
            .limit(limit)
        )
        return [_title_row(t, sub_lang) for t in s.scalars(stmt).all()]


def list_missing_subtitles(lang: str = DEFAULT_SUB_LANG, limit: int = 500) -> list[TitleRow]:
    """Owned titles that lack a ``lang`` sidecar on every one of their files."""

    with session_scope() as s:
        titles = s.scalars(
            select(Title).options(selectinload(Title.genres), selectinload(Title.owned_files))
        ).all()
        rows = []
        for t in titles:
            owned = t.owned_files
            if not owned:
                continue
            has_lang = any(f.subtitle_langs and lang in f.subtitle_langs for f in owned)
            if has_lang:
                continue
            rows.append(
                TitleRow(
                    id=t.id,
                    title=t.title,
                    year=t.year,
                    kind=str(t.kind),
                    imdb_rating=t.imdb_rating,
                    imdb_votes=t.imdb_votes,
                    poster_url=t.poster_url,
                    genres=[g.name for g in t.genres],
                    owned_count=len(owned),
                    resolutions=sorted({f.resolution for f in owned if f.resolution}),
                    has_sub=False,
                )
            )
            if len(rows) >= limit:
                break
        return rows


def list_candidates(status: str | None = "new", limit: int = 100) -> list[CandidateRow]:
    """Ranked candidate queue. Defaults to the pending (``new``) queue."""

    with session_scope() as s:
        stmt = (
            select(Candidate, Title)
            .join(Title, Title.id == Candidate.title_id)
            .order_by(Candidate.score.is_(None), Candidate.score.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(Candidate.status == status)
        rows = []
        for cand, title in s.execute(stmt).all():
            taste = (cand.features or {}).get("taste") or {}
            rows.append(
                CandidateRow(
                    id=cand.id,
                    title=title.title,
                    year=title.year,
                    kind=str(title.kind),
                    status=str(cand.status),
                    source=str(cand.source),
                    reason=cand.reason,
                    score=cand.score,
                    imdb_rating=title.imdb_rating,
                    imdb_votes=title.imdb_votes,
                    poster_url=title.poster_url,
                    overview=title.overview,
                    taste_score=taste.get("score"),
                    taste_like=list(taste.get("like") or [])[:4],
                )
            )
        return rows


def candidate_counts() -> dict[str, int]:
    """Count candidates by status (for dashboard badges)."""

    with session_scope() as s:
        return {
            str(status): (s.scalar(select(func.count()).where(Candidate.status == status)) or 0)
            for status in CandidateStatus
        }


def recent_runs(limit: int = 25) -> list[RunRow]:
    with session_scope() as s:
        runs = s.scalars(select(JobRun).order_by(JobRun.started_at.desc()).limit(limit)).all()
        return [
            RunRow(
                id=r.id,
                kind=r.kind,
                status=str(r.status),
                started_at=r.started_at.isoformat() if r.started_at else None,
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                stats=r.stats,
            )
            for r in runs
        ]
