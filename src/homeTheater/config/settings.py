"""Typed configuration models.

Layering (see plan §5.1): defaults (these models) -> ``config.yaml`` (non-secret)
-> environment overrides -> ``.env`` (secrets). Secrets live in :class:`Secrets`
(loaded from the environment); everything else is plain, committable config.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class NASPaths(BaseModel):
    """SMB roots on the NAS. Read-only from this app (Radarr/Sonarr own writes)."""

    movies_root: str = Field(..., description="SMB path or share subpath for movies")
    tv_root: str = Field(..., description="SMB path or share subpath for TV shows")
    share: str | None = Field(None, description="SMB share name if paths are relative")


class Thresholds(BaseModel):
    """Discovery filters: 'high rank with enough views'."""

    min_imdb_rating: float = Field(7.0, ge=0, le=10)
    min_imdb_votes: int = Field(25_000, ge=0)
    min_tmdb_votes: int = Field(500, ge=0)
    allowed_resolutions: list[str] = Field(default_factory=lambda: ["1080p", "2160p"])


class FeatureFlags(BaseModel):
    """Safety switches. Defaults are the *safe* values."""

    dry_run: bool = True
    auto_approve: bool = False


class Schedule(BaseModel):
    """APScheduler intervals, in minutes. 0 disables a job."""

    scan_interval_minutes: int = Field(360, ge=0)
    discovery_interval_minutes: int = Field(720, ge=0)
    subtitle_interval_minutes: int = Field(720, ge=0)
    import_reconcile_interval_minutes: int = Field(15, ge=0)


class Database(BaseModel):
    url: str = Field("sqlite:///data/home_theater.db")
    echo: bool = False


class Metadata(BaseModel):
    """Metadata enrichment behaviour (plan §5.3)."""

    language: str = Field("en-US", description="TMDb language for details")
    cache_days: int = Field(14, ge=0, description="TTL for cached provider responses")
    max_concurrency: int = Field(8, ge=1, description="parallel provider fetches")


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


class AppConfig(BaseModel):
    """The fully-resolved application configuration."""

    nas: NASPaths
    thresholds: Thresholds = Field(default_factory=Thresholds)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    schedule: Schedule = Field(default_factory=Schedule)
    database: Database = Field(default_factory=Database)
    metadata: Metadata = Field(default_factory=Metadata)
    quality_profile: str = Field("HD-1080p", description="Radarr/Sonarr profile name")
    enabled_providers: list[str] = Field(default_factory=list)
    secrets: Secrets = Field(default_factory=Secrets, repr=False)

    @model_validator(mode="after")
    def _validate(self) -> AppConfig:
        if self.thresholds.min_imdb_rating > 10:
            raise ValueError("min_imdb_rating must be <= 10")
        return self
