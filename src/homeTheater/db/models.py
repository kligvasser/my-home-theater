"""ORM models (plan §4).

Identity rule: every owned file and candidate resolves to a TMDb id (and IMDb id
when available); "do I own this?" is an id membership test, not fuzzy matching.
Genres are a proper join table (not a comma string) so the dashboard breakdown
and filtering are cheap.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


def _enum(enum_cls: type) -> SAEnum:
    """Store enums as portable VARCHAR (values == names for our StrEnums) so DB
    round-trips return real enum members, not bare strings."""

    return SAEnum(enum_cls, native_enum=False, validate_strings=True)


class TitleKind(enum.StrEnum):
    movie = "movie"
    series = "series"


class CandidateSource(enum.StrEnum):
    discovery = "discovery"
    watchlist = "watchlist"
    manual = "manual"


class CandidateStatus(enum.StrEnum):
    new = "new"
    approved = "approved"
    queued = "queued"
    downloading = "downloading"
    imported = "imported"
    rejected = "rejected"
    failed = "failed"


class ProviderKind(enum.StrEnum):
    indexer = "indexer"
    subtitle = "subtitle"
    metadata = "metadata"
    library = "library"  # Radarr/Sonarr/Bazarr


class RunStatus(enum.StrEnum):
    running = "running"
    success = "success"
    failed = "failed"


class Title(Base, TimestampMixin):
    __tablename__ = "title"
    # TMDb movie ids and TV ids are independent sequences (tv/1396 != movie/1396),
    # so uniqueness — and every lookup — must be scoped by kind.
    __table_args__ = (UniqueConstraint("tmdb_id", "kind", name="uq_title_tmdb_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String(16), index=True, unique=True)
    tvdb_id: Mapped[int | None] = mapped_column(Integer, index=True)  # Sonarr keys on this
    kind: Mapped[TitleKind] = mapped_column(_enum(TitleKind))
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer)
    runtime: Mapped[int | None] = mapped_column(Integer)
    imdb_rating: Mapped[float | None] = mapped_column(Float)
    imdb_votes: Mapped[int | None] = mapped_column(Integer)
    tmdb_rating: Mapped[float | None] = mapped_column(Float)
    tmdb_votes: Mapped[int | None] = mapped_column(Integer)
    popularity: Mapped[float | None] = mapped_column(Float)
    poster_url: Mapped[str | None] = mapped_column(String(1024))
    overview: Mapped[str | None] = mapped_column(Text)

    # --- taste/ML features (plan: characterize the library, train a preference
    # model). Populated by metadata enrichment; snapshotted onto candidates at
    # decision time (Candidate.features) so labels keep their era's feature values.
    original_language: Mapped[str | None] = mapped_column(String(8))
    origin_countries: Mapped[list[str] | None] = mapped_column(JSON)
    release_date: Mapped[str | None] = mapped_column(String(10))  # ISO yyyy-mm-dd
    certification: Mapped[str | None] = mapped_column(String(16))  # US content rating
    keywords: Mapped[list[str] | None] = mapped_column(JSON)  # TMDb keywords
    cast_top: Mapped[list[str] | None] = mapped_column(JSON)  # top-billed cast names
    directors: Mapped[list[str] | None] = mapped_column(JSON)  # directors / creators
    collection_tmdb_id: Mapped[int | None] = mapped_column(Integer)  # franchise
    collection_name: Mapped[str | None] = mapped_column(String(256))
    seasons_count: Mapped[int | None] = mapped_column(Integer)  # series only
    episodes_count: Mapped[int | None] = mapped_column(Integer)  # series only
    series_status: Mapped[str | None] = mapped_column(String(32))  # Ended/Returning…
    # Radarr/Sonarr report a file for this title (set by reconcile_library) —
    # counts as "owned" for discovery even before the NAS scanner sees the file.
    arr_has_file: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last enrichment attempt: keeps titles the providers can't resolve (or that
    # legitimately have no IMDb rating) from being re-enqueued on every run.
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    genres: Mapped[list[Genre]] = relationship(secondary="title_genre", back_populates="titles")
    owned_files: Mapped[list[OwnedFile]] = relationship(
        back_populates="title", cascade="all, delete-orphan"
    )
    candidates: Mapped[list[Candidate]] = relationship(back_populates="title")


class Genre(Base):
    __tablename__ = "genre"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    titles: Mapped[list[Title]] = relationship(secondary="title_genre", back_populates="genres")


class TitleGenre(Base):
    __tablename__ = "title_genre"

    title_id: Mapped[int] = mapped_column(ForeignKey("title.id"), primary_key=True)
    genre_id: Mapped[int] = mapped_column(ForeignKey("genre.id"), primary_key=True)


class OwnedFile(Base, TimestampMixin):
    __tablename__ = "owned_file"
    __table_args__ = (UniqueConstraint("path", name="uq_owned_file_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id"), index=True)
    path: Mapped[str] = mapped_column(String(2048))
    kind: Mapped[TitleKind] = mapped_column(_enum(TitleKind))
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
    episode_end: Mapped[int | None] = mapped_column(Integer)  # multi-episode files
    resolution: Mapped[str | None] = mapped_column(String(16))
    codec: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    container: Mapped[str | None] = mapped_column(String(16))
    subtitle_langs: Mapped[list[str] | None] = mapped_column(JSON)
    file_hash: Mapped[str | None] = mapped_column(String(128))

    title: Mapped[Title] = relationship(back_populates="owned_files")


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidate"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id"), index=True)
    # Season-scoped candidate (series only): "grab season N of a series you own".
    # NULL means the whole title (movies, new series). Dedup/rejection invariants
    # apply per (title, season) so rejecting S3 doesn't bury next year's S4.
    season: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[CandidateSource] = mapped_column(_enum(CandidateSource))
    status: Mapped[CandidateStatus] = mapped_column(
        _enum(CandidateStatus), default=CandidateStatus.new
    )
    reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    # Feature snapshot at creation time (see homeTheater.features): training data
    # for the preference model — approve/reject/import decisions are the labels.
    features: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    title: Mapped[Title] = relationship(back_populates="candidates")
    downloads: Mapped[list[Download]] = relationship(back_populates="candidate")


class Download(Base, TimestampMixin):
    __tablename__ = "download"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"), index=True)
    # For the hybrid, "handle" is the Radarr/Sonarr id we track through their API.
    external_id: Mapped[str | None] = mapped_column(String(128))
    release: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str | None] = mapped_column(String(32))
    progress: Mapped[float | None] = mapped_column(Float)
    save_path: Mapped[str | None] = mapped_column(String(2048))
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    candidate: Mapped[Candidate] = relationship(back_populates="downloads")


class Subtitle(Base, TimestampMixin):
    __tablename__ = "subtitle"

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id"), index=True)
    owned_file_id: Mapped[int | None] = mapped_column(ForeignKey("owned_file.id"))
    lang: Mapped[str] = mapped_column(String(8))
    provider: Mapped[str | None] = mapped_column(String(64))
    path: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str | None] = mapped_column(String(32))


class ProviderSetting(Base, TimestampMixin):
    __tablename__ = "provider_setting"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ProviderKind] = mapped_column(_enum(ProviderKind))
    name: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class JobRun(Base):
    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # scan|discovery|subtitle|...
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.running)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    log_ref: Mapped[str | None] = mapped_column(String(256))


class Setting(Base, TimestampMixin):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)


class MetadataCache(Base):
    """Persistent cache of external provider responses (TTL enforced in code).

    Keyed by ``(provider, cache_key)`` so a re-run reuses TMDb/OMDb payloads and
    respects rate limits (OMDb's 1k/day especially — plan §3).
    """

    __tablename__ = "metadata_cache"
    __table_args__ = (UniqueConstraint("provider", "cache_key", name="uq_cache_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    cache_key: Mapped[str] = mapped_column(String(512), index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
