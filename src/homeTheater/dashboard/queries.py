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
    # One meter per configured subtitle language (coverage == coverages[0]).
    coverages: list[Coverage] = field(default_factory=list)


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


# Library sort options + directions are defined next to the sort logic below
# (TITLE_SORTS / TITLE_DIRS), keyed off _SORT_KEYS.


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
    release_date: str | None = None  # ISO yyyy-mm-dd (finer than year)


def get_stats(sub_lang: str = DEFAULT_SUB_LANG, sub_langs: list[str] | None = None) -> LibraryStats:
    langs = sub_langs or [sub_lang]
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
            select(func.round(func.avg(Title.imdb_rating), 2)).where(Title.imdb_rating.is_not(None))
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

        stats.coverages = _coverages(s, langs)
        stats.coverage = stats.coverages[0]
        return stats


def _coverages(session: Session, langs: list[str]) -> list[Coverage]:
    """% of owned titles that have at least one sidecar in each language.

    ``subtitle_langs`` is a JSON list, so membership is computed in Python — fine
    for a home-scale library and avoids DB-specific JSON operators.
    """

    rows = session.execute(select(OwnedFile.title_id, OwnedFile.subtitle_langs)).all()
    owned: set[int] = set()
    covered: dict[str, set[int]] = {lang: set() for lang in langs}
    for title_id, file_langs in rows:
        owned.add(title_id)
        for lang in langs:
            if file_langs and lang in file_langs:
                covered[lang].add(title_id)
    return [Coverage(lang=lang, covered=len(covered[lang]), total=len(owned)) for lang in langs]


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


_RES_RANK = {"2160p": 4, "4k": 4, "1080p": 3, "720p": 2, "576p": 1, "480p": 1}


def _res_rank(resolutions: list[str]) -> int | None:
    ranks = [_RES_RANK.get(r.lower(), 0) for r in resolutions]
    return max(ranks) if ranks else None


# Every library column is sortable. Value ``None`` (missing rating, no files, …)
# always sorts last, regardless of direction. Second element = default direction
# when a column is first clicked (True = descending).
_SORT_KEYS: dict[str, tuple[Any, bool]] = {
    "title": (lambda r: r.title.lower(), False),
    "year": (lambda r: r.year, True),
    "kind": (lambda r: r.kind, False),
    "rating": (lambda r: r.imdb_rating, True),  # IMDb column
    "votes": (lambda r: r.imdb_votes, True),
    "genres": (lambda r: (", ".join(r.genres).lower() or None), False),
    "added": (lambda r: r.id, True),  # id is a monotonic proxy for insert order
    "files": (lambda r: r.owned_count, True),
    "res": (lambda r: _res_rank(r.resolutions), True),
    "subs": (lambda r: (1 if r.has_sub else 0), True),
}
TITLE_SORTS = tuple(_SORT_KEYS)
TITLE_DIRS = ("asc", "desc")


def default_dir(sort: str) -> str:
    """The natural first-click direction for a column (desc for numeric/date)."""

    return "desc" if _SORT_KEYS.get(sort, _SORT_KEYS["added"])[1] else "asc"


def _sorted_rows(rows: list[TitleRow], sort: str, direction: str | None) -> list[TitleRow]:
    keyfn, default_desc = _SORT_KEYS.get(sort, _SORT_KEYS["added"])
    desc = default_desc if direction not in TITLE_DIRS else (direction == "desc")
    present = [r for r in rows if keyfn(r) is not None]
    missing = [r for r in rows if keyfn(r) is None]  # always last
    present.sort(key=keyfn, reverse=desc)
    return present + missing


def list_titles(
    q: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sub_lang: str = DEFAULT_SUB_LANG,
    sort: str | None = "added",
    direction: str | None = None,
) -> tuple[list[TitleRow], int]:
    """Filtered, fully-sortable, paginated title list.

    Sorting is done in Python (the catalog is small) so computed columns — files,
    resolution, subtitle coverage, genres — sort as naturally as the DB columns.
    """

    page = max(page, 1)
    with session_scope() as s:
        stmt = select(Title).options(selectinload(Title.genres), selectinload(Title.owned_files))
        if q:
            stmt = stmt.where(Title.title.ilike(f"%{q}%"))
        if kind in (TitleKind.movie, TitleKind.series):
            stmt = stmt.where(Title.kind == kind)
        rows = [_title_row(t, sub_lang) for t in s.scalars(stmt).all()]

    rows = _sorted_rows(rows, sort or "added", direction)
    total = len(rows)
    start = (page - 1) * page_size
    return rows[start : start + page_size], total


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


CANDIDATE_PAGE_SIZE = 60

# Candidate queue sorts. All sort best-first (descending); ``None`` sorts last.
# Sorted in Python (bounded queue) so JSON-derived taste ranks alongside DB fields.
_CANDIDATE_SORT_KEYS: dict[str, Any] = {
    "score": lambda r: r.score,
    "taste": lambda r: r.taste_score,
    "rating": lambda r: r.imdb_rating,  # IMDb score
    "votes": lambda r: r.imdb_votes,
    "year": lambda r: r.year,
    "release": lambda r: r.release_date,  # ISO date -> chronological
    "added": lambda r: r.id,
}
CANDIDATE_SORTS = tuple(_CANDIDATE_SORT_KEYS)


def list_candidates(
    status: str | None = "new",
    kind: str | None = None,
    sort: str = "score",
    page: int = 1,
    page_size: int = CANDIDATE_PAGE_SIZE,
) -> tuple[list[CandidateRow], int]:
    """Ranked candidate queue (defaults to the pending ``new`` queue).

    Returns ``(page_rows, total_matching)``. Every column sorts best-first with
    missing values last; sorting is in Python since the queue is small.
    """

    page = max(page, 1)
    with session_scope() as s:
        stmt = select(Candidate, Title).join(Title, Title.id == Candidate.title_id)
        if status:
            stmt = stmt.where(Candidate.status == status)
        if kind in (TitleKind.movie, TitleKind.series):
            stmt = stmt.where(Title.kind == kind)
        rows: list[CandidateRow] = []
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
                    release_date=title.release_date,
                )
            )

    keyfn = _CANDIDATE_SORT_KEYS.get(sort, _CANDIDATE_SORT_KEYS["score"])
    present = [r for r in rows if keyfn(r) is not None]
    missing = [r for r in rows if keyfn(r) is None]  # always last
    present.sort(key=keyfn, reverse=True)
    ordered = present + missing
    total = len(ordered)
    start = (page - 1) * page_size
    return ordered[start : start + page_size], total


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
