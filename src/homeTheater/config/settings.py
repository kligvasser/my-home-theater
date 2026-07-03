"""Typed configuration models.

Layering (see plan §5.1): defaults (these models) -> ``config.yaml`` (non-secret)
-> environment overrides -> ``.env`` (secrets). Secrets live in :class:`Secrets`
(loaded from the environment); everything else is plain, committable config.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class NASPaths(BaseModel):
    """SMB roots on the NAS. Read-only from this app (Radarr/Sonarr own writes)."""

    movies_root: str = Field(..., description="SMB path or share subpath for movies")
    tv_root: str = Field(..., description="SMB path or share subpath for TV shows")
    share: str | None = Field(None, description="SMB share name if paths are relative")


class KindThresholds(BaseModel):
    """Optional per-kind overrides; ``None`` falls back to the global value."""

    min_imdb_rating: float | None = Field(None, ge=0, le=10)
    min_imdb_votes: int | None = Field(None, ge=0)
    min_tmdb_votes: int | None = Field(None, ge=0)


class ResolvedThresholds(BaseModel):
    """Thresholds flattened for one title kind — what the filter actually applies."""

    model_config = {"frozen": True}

    min_imdb_rating: float
    min_imdb_votes: int
    min_tmdb_votes: int
    tmdb_fallback: bool


class Thresholds(BaseModel):
    """Discovery filters: 'high rank with enough views'.

    Movies and series have very different vote economics (a hit film gets 10-50x
    the IMDb votes of an equally-loved series), so each kind can override the
    global bars via ``movie:`` / ``series:``.
    """

    min_imdb_rating: float = Field(7.0, ge=0, le=10)
    min_imdb_votes: int = Field(25_000, ge=0)
    min_tmdb_votes: int = Field(500, ge=0)
    # When IMDb data is missing (no OMDb key, or a title OMDb doesn't know yet),
    # fall back to TMDb rating/votes instead of rejecting everything.
    tmdb_fallback: bool = True
    allowed_resolutions: list[str] = Field(default_factory=lambda: ["1080p", "2160p"])
    movie: KindThresholds = Field(default_factory=KindThresholds)
    series: KindThresholds = Field(
        default_factory=lambda: KindThresholds(min_imdb_votes=5_000, min_tmdb_votes=200)
    )

    def for_kind(self, kind: str) -> ResolvedThresholds:
        over = self.movie if kind == "movie" else self.series
        return ResolvedThresholds(
            min_imdb_rating=(
                over.min_imdb_rating if over.min_imdb_rating is not None else self.min_imdb_rating
            ),
            min_imdb_votes=(
                over.min_imdb_votes if over.min_imdb_votes is not None else self.min_imdb_votes
            ),
            min_tmdb_votes=(
                over.min_tmdb_votes if over.min_tmdb_votes is not None else self.min_tmdb_votes
            ),
            tmdb_fallback=self.tmdb_fallback,
        )


class FeatureFlags(BaseModel):
    """Safety switches. Defaults are the *safe* values."""

    dry_run: bool = True
    auto_approve: bool = False


class Schedule(BaseModel):
    """APScheduler intervals, in minutes. 0 disables a job."""

    enabled: bool = False  # opt-in; `serve` only starts jobs when true
    scan_interval_minutes: int = Field(360, ge=0)
    discovery_interval_minutes: int = Field(720, ge=0)
    subtitle_interval_minutes: int = Field(720, ge=0)
    sync_interval_minutes: int = Field(10, ge=0)
    acquire_interval_minutes: int = Field(30, ge=0)  # queue approved candidates
    import_reconcile_interval_minutes: int = Field(60, ge=0)
    backup_interval_minutes: int = Field(1440, ge=0)  # daily DB backup


class Database(BaseModel):
    url: str = Field("sqlite:///data/home_theater.db")
    echo: bool = False


class Metadata(BaseModel):
    """Metadata enrichment behaviour (plan §5.3)."""

    language: str = Field("en-US", description="TMDb language for details")
    cache_days: int = Field(14, ge=0, description="TTL for cached provider responses")
    max_concurrency: int = Field(8, ge=1, description="parallel provider fetches")


class Discovery(BaseModel):
    """Candidate discovery behaviour (plan §5.4)."""

    trending: bool = True
    top_rated: bool = False
    include_movies: bool = True
    include_series: bool = True
    trending_window: str = Field("week", pattern="^(day|week)$")
    max_per_source: int = Field(20, ge=1, le=100)
    excluded_genres: list[str] = Field(default_factory=list)


class Subtitles(BaseModel):
    """Subtitle coverage/automation (plan §5.5). Bazarr does the fetching."""

    languages: list[str] = Field(default_factory=lambda: ["he"])
    # Cap Bazarr search-missing triggers per sweep so a big backlog doesn't burn
    # provider quotas in one scheduled run.
    max_searches_per_sweep: int = Field(50, ge=1)

    @property
    def primary(self) -> str:
        return self.languages[0] if self.languages else "he"


class Acquisition(BaseModel):
    """How approved candidates are handed to Radarr/Sonarr (plan §5.6).

    Release selection is a Radarr/Sonarr *quality profile* configured once in those
    apps; we just pick the profile name, root folder, and whether to search on add.
    """

    movie_quality_profile: str = "HD-1080p"
    series_quality_profile: str = "HD-1080p"
    movie_root_folder: str | None = None  # None -> use the arr's first root folder
    series_root_folder: str | None = None
    search_on_add: bool = True


class Secrets(BaseSettings):
    """Secrets from environment / ``.env``. Never logged, never serialized."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # Metadata
    tmdb_api_key: SecretStr | None = None
    omdb_api_key: SecretStr | None = None

    # NAS / SMB
    smb_user: str | None = None
    smb_pass: SecretStr | None = None
    smb_host: str | None = None  # IP fallback for flaky .local mDNS

    # Media-automation stack (we drive these; they own Prowlarr/qBittorrent)
    radarr_url: str | None = None
    radarr_api_key: SecretStr | None = None
    sonarr_url: str | None = None
    sonarr_api_key: SecretStr | None = None
    bazarr_url: str | None = None
    bazarr_api_key: SecretStr | None = None

    # Watchlist
    trakt_client_id: str | None = None
    trakt_client_secret: SecretStr | None = None

    # Notifications
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Dashboard auth (required before any mutating endpoint is exposed)
    dashboard_token: SecretStr | None = None
    # Separate secret for Radarr/Sonarr webhook URLs (?token=...). Falls back to
    # dashboard_token, but a distinct value keeps the dashboard token out of arr
    # configs and access logs.
    webhook_token: SecretStr | None = None


class AppConfig(BaseModel):
    """The fully-resolved application configuration."""

    nas: NASPaths
    thresholds: Thresholds = Field(default_factory=Thresholds)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    schedule: Schedule = Field(default_factory=Schedule)
    database: Database = Field(default_factory=Database)
    metadata: Metadata = Field(default_factory=Metadata)
    discovery: Discovery = Field(default_factory=Discovery)
    subtitles: Subtitles = Field(default_factory=Subtitles)
    acquisition: Acquisition = Field(default_factory=Acquisition)
    enabled_providers: list[str] = Field(default_factory=list)
    secrets: Secrets = Field(default_factory=Secrets, repr=False)
