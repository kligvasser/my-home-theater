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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin


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

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True, unique=True)
    imdb_id: Mapped[str | None] = mapped_column(String(16), index=True, unique=True)
    kind: Mapped[TitleKind] = mapped_column(String(8))
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
    kind: Mapped[TitleKind] = mapped_column(String(8))
    season: Mapped[int | None] = mapped_column(Integer)
    episode: Mapped[int | None] = mapped_column(Integer)
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
    source: Mapped[CandidateSource] = mapped_column(String(16))
    status: Mapped[CandidateStatus] = mapped_column(String(16), default=CandidateStatus.new)
    reason: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
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
    kind: Mapped[ProviderKind] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class JobRun(Base):
    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # scan|discovery|subtitle|...
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[RunStatus] = mapped_column(String(16), default=RunStatus.running)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    log_ref: Mapped[str | None] = mapped_column(String(256))


class Setting(Base, TimestampMixin):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
