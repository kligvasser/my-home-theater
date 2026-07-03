"""Read-only catalog queries for the dashboard (plan §5.8, §9).

Everything returns plain dataclasses built inside a session so templates never
touch detached ORM objects or trigger lazy loads after the session closes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import Genre, JobRun, OwnedFile, Title, TitleGenre, TitleKind
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


@dataclass(frozen=True, slots=True)
class RunRow:
    id: int
    kind: str
    status: str
    started_at: str | None
    finished_at: str | None
    stats: dict[str, Any] | None


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


def list_titles(
    q: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = PAGE_SIZE,
    sub_lang: str = DEFAULT_SUB_LANG,
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
        stmt = (
            stmt.order_by(Title.imdb_rating.is_(None), Title.imdb_rating.desc(), Title.title)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        rows = []
        for t in s.scalars(stmt).all():
            resolutions = sorted({f.resolution for f in t.owned_files if f.resolution})
            has_sub = any(f.subtitle_langs and sub_lang in f.subtitle_langs for f in t.owned_files)
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
                    owned_count=len(t.owned_files),
                    resolutions=resolutions,
                    has_sub=has_sub,
                )
            )
        return rows, total


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
