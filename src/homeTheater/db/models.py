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
    Index,
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
    upgrade = "upgrade"  # a better copy of a movie you own (F4): never a training label


class CandidateStatus(enum.StrEnum):
    new = "new"
    approved = "approved"
    queued = "queued"
    downloading = "downloading"
    imported = "imported"
    rejected = "rejected"
    failed = "failed"
    # The owner stopped the work (Cancel, Unfollow). Not a dislike: never a training
    # label, unlike ``rejected``. Discovery still won't re-suggest it; following the
    # show again reopens its seasons.
    cancelled = "cancelled"


# No download work may go on for these. Only ``rejected`` is a label.
STOPPED_STATUSES = frozenset({CandidateStatus.rejected, CandidateStatus.cancelled})

# Download.state values still in the pipeline. "completed" is a torrent that finished
# downloading but hasn't imported yet, so it stays live until it has.
LIVE_DOWNLOAD_STATES = ("queued", "downloading", "importing", "completed")

# Download.error markers. They live in the error column so a parked download keeps its
# state: the sweep, cleanup and the dashboard all treat "completed" as "local data still
# needed", which is exactly right for a download waiting on a person.
#
# ``removed:``: ban record of a release thrown out as malware or junk (any state).
# ``stalled:``: failed row whose hash went dead; re-selected only as a last resort.
# ``held:``: completed, but a real video wasn't imported; local data is kept and every
#   sweep looks again.
# ``needs_attention:``: completed, import gave up (attempt cap, or a library conflict);
#   sweeps skip it until someone resolves it and calls retry_import.
# REMOVAL_PENDING (exact match, not a prefix): imported, torrent removal still to do.
MARKER_REMOVED = "removed:"
MARKER_STALLED = "stalled:"
MARKER_HELD = "held:"
MARKER_NEEDS_ATTENTION = "needs_attention:"
MARKER_REMOVAL_PENDING = "local copy removal pending"


class ProviderKind(enum.StrEnum):
    indexer = "indexer"
    subtitle = "subtitle"
    metadata = "metadata"
    library = "library"  # Radarr/Sonarr/Bazarr


class RunStatus(enum.StrEnum):
    running = "running"
    success = "success"
    partial = "partial"  # finished, but some of the work failed
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
    # Followed series: auto-grab new seasons/episodes as they air (series only).
    # Unlike "owned", a followed show is monitored even before you have any files.
    followed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
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
    # NULLs stay distinct, so rows without a handle (or unlinked ones) can coexist.
    __table_args__ = (
        Index("uq_download_candidate_external", "candidate_id", "external_id", unique=True),
    )

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


class DownloadTracking(Base):
    """What the sweep has seen of one download.

    A table of its own, not columns on ``download``: ``create_all`` makes a missing
    table but can't alter one, so this works on a database that hasn't run the
    migration yet.
    """

    __tablename__ = "download_tracking"

    download_id: Mapped[int] = mapped_column(
        ForeignKey("download.id", ondelete="CASCADE"), primary_key=True
    )
    last_progress: Mapped[float] = mapped_column(Float, default=0.0)
    last_progress_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Separates "no progress" from "nobody was looking" (app down, host asleep).
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    import_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_import_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GrabRetry(Base):
    """Backoff for a season whose last download died: the scheduled acquire leaves it
    alone until ``next_attempt_at``. Same reason for being a table as above."""

    __tablename__ = "grab_retry"

    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), primary_key=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Subtitle(Base, TimestampMixin):
    __tablename__ = "subtitle"
    # NULLs stay distinct, so rows the scanner unlinked (owned_file_id=NULL) coexist.
    __table_args__ = (Index("uq_subtitle_file_lang", "owned_file_id", "lang", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    title_id: Mapped[int] = mapped_column(ForeignKey("title.id"), index=True)
    owned_file_id: Mapped[int | None] = mapped_column(ForeignKey("owned_file.id"))
    lang: Mapped[str] = mapped_column(String(8))
    provider: Mapped[str | None] = mapped_column(String(64))
    path: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str | None] = mapped_column(String(32))


class SubtitleAttempt(Base):
    """Negative cache for the native sweep: one failed (file, language) pair.

    A row exists only while the pair is unsatisfied; success deletes it.
    ``ondelete=CASCADE`` because the scanner bulk-deletes pruned ``owned_file``
    rows with foreign keys enforced.
    """

    __tablename__ = "subtitle_attempt"
    __table_args__ = (
        UniqueConstraint("owned_file_id", "lang", name="uq_subtitle_attempt_file_lang"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    owned_file_id: Mapped[int] = mapped_column(ForeignKey("owned_file.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(String(8))
    last_tried: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_outcome: Mapped[str | None] = mapped_column(String(32))


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


class WatchState(Base):
    """What Kodi and Trakt agree has been watched, one row per owned file.

    ``source`` names whoever supplied the newest ``last_played_at``. A missing row
    means nobody reported a play; the storage planner reads this table too.
    """

    __tablename__ = "watch_state"

    owned_file_id: Mapped[int] = mapped_column(
        ForeignKey("owned_file.id", ondelete="CASCADE"), primary_key=True
    )
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    last_played_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(16))  # kodi | trakt
    # The last_played_at Trakt is known to hold, so a push is never sent twice.
    trakt_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SubtitleSync(Base):
    """Timing state of the subtitle chosen for one (file, language) pair."""

    __tablename__ = "subtitle_sync"
    __table_args__ = (UniqueConstraint("owned_file_id", "lang", name="uq_subtitle_sync_file_lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owned_file_id: Mapped[int] = mapped_column(ForeignKey("owned_file.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(16), default="pending")
    source: Mapped[str | None] = mapped_column(String(64))
    release: Mapped[str | None] = mapped_column(String(512))
    result_key: Mapped[str | None] = mapped_column(String(64))
    hash_match: Mapped[bool] = mapped_column(Boolean, default=False)
    offset_seconds: Mapped[float | None] = mapped_column(Float)
    # The backup left beside the subtitle, and what it was.
    original_path: Mapped[str | None] = mapped_column(String(2048))
    original_source: Mapped[str | None] = mapped_column(String(64))
    original_release: Mapped[str | None] = mapped_column(String(512))
    original_key: Mapped[str | None] = mapped_column(String(64))
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[str | None] = mapped_column(Text)


class SubtitleReject(Base):
    """A subtitle the owner threw out for one file: never choose it again.

    ``result_key`` catches it in search results, before a download is spent;
    ``content_sha256`` catches it under a new id, and covers subtitles chosen before
    their key was recorded.
    """

    __tablename__ = "subtitle_reject"
    __table_args__ = (Index("ix_subtitle_reject_file_lang", "owned_file_id", "lang"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owned_file_id: Mapped[int] = mapped_column(ForeignKey("owned_file.id", ondelete="CASCADE"))
    lang: Mapped[str] = mapped_column(String(8))
    result_key: Mapped[str | None] = mapped_column(String(64))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(512))
    rejected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TrashItem(Base):
    """One file the app moved to the NAS trash, and how to put it back (F1).

    Both paths are relative, so a row can never name anything outside the library:
    ``original_path`` to ``torrent.library_base_dir``, ``trash_path`` to the trash root.
    """

    __tablename__ = "trash_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    original_path: Mapped[str] = mapped_column(String(2048))
    trash_path: Mapped[str] = mapped_column(String(2048))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    # pending (rename not confirmed) | trashed | restored | purged
    state: Mapped[str] = mapped_column(String(16), index=True)
    trashed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UpgradeCheck(Base):
    """When a movie was last looked at for a better copy: the per-title cooldown (F4)."""

    __tablename__ = "upgrade_check"

    title_id: Mapped[int] = mapped_column(
        ForeignKey("title.id", ondelete="CASCADE"), primary_key=True
    )
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
