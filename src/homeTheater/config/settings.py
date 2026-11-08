"""Typed configuration models.

Layering (see plan §5.1): defaults (these models) -> ``config.yaml`` (non-secret)
-> environment overrides -> ``.env`` (secrets). Secrets live in :class:`Secrets`
(loaded from the environment); everything else is plain, committable config.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class NASPaths(BaseModel):
    """SMB roots on the NAS. Read-only from this app (Radarr/Sonarr own writes)."""

    movies_root: str = Field(..., description="SMB path or share subpath for movies")
    tv_root: str = Field(..., description="SMB path or share subpath for TV shows")
    share: str | None = Field(None, description="SMB share name if paths are relative")
    # Address to dial for scans when SMB_HOST is a flaky ``.local`` name, e.g. the
    # NAS's IP. Catalog paths keep SMB_HOST either way: it only changes where the
    # socket goes, never OwnedFile.path. None -> resolve SMB_HOST at each walk.
    connect_host: str | None = None


# Models a dashboard override can reach. A typo'd key is rejected rather than
# dropped, so a setting can't look saved while nothing changed. These also read
# config.yaml, so an unknown key in those sections fails at startup.
_STRICT = ConfigDict(extra="forbid")


class KindThresholds(BaseModel):
    """Optional per-kind overrides; ``None`` falls back to the global value."""

    model_config = _STRICT

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

    model_config = _STRICT

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

    model_config = _STRICT

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
    train_interval_minutes: int = Field(1440, ge=0)  # retrain the preference model
    trash_interval_minutes: int = Field(1440, ge=0)  # purge expired NAS trash
    upgrades_interval_minutes: int = Field(1440, ge=0)  # look for better copies (F4)
    # Cancel a scheduled job that runs longer than this so a hung NAS/network
    # job can't hold the global lock forever and starve the others (e.g. backup).
    # 0 disables the timeout. Generous by default — legit big copies are slow.
    job_timeout_minutes: int = Field(180, ge=0)
    # On startup and after the host wakes, run any job whose last success is older
    # than its interval. The Mac sleeps through interval fires, so without this a
    # daily job can go days without running.
    catch_up: bool = True


class Scanner(BaseModel):
    """How much one scan may prune from a root before it refuses (a walk that
    "lost" that many files is more likely a NAS fault than real deletions).

    Refused when the prune exceeds ``max_prune_rows``, or exceeds both
    ``prune_floor_rows`` and ``max_prune_percent`` of the root's catalog rows. The
    floor is what lets a small library lose a file: 1 of 10 is 10%. Override once
    with ``home-theater scan --force-prune``.
    """

    max_prune_percent: float = Field(5.0, gt=0, le=100)
    max_prune_rows: int = Field(100, ge=1)
    prune_floor_rows: int = Field(5, ge=0)
    # Abandon (and fail) an SMB walk that takes longer than this. Kept under
    # schedule.job_timeout_minutes so a stuck walk also fails for manual scans,
    # which have no job timeout. 0 disables it.
    walk_timeout_minutes: int = Field(150, ge=0)


class Server(BaseModel):
    """Which ``Host`` headers the dashboard answers to (DNS-rebinding protection).

    The machine's own hostname is always added. ``HOME_THEATER_ALLOWED_HOSTS``
    (comma-separated) adds more without editing this file.
    """

    # Exact names or "*.suffix"; a lone "*" turns the check off.
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "::1", "*.local"]
    )
    # Also accept private/Tailscale IP literals (http://192.168.1.20:8000 from a
    # phone). A rebinding attack needs a DNS *name*, so IPs don't weaken the check.
    allow_private_ips: bool = True


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

    model_config = _STRICT

    trending: bool = True
    top_rated: bool = False
    watchlist: bool = True  # Trakt watchlist (needs keys + `home-theater trakt-auth`)
    new_seasons: bool = True  # new seasons of series you already own
    include_movies: bool = True
    include_series: bool = True
    trending_window: str = Field("week", pattern="^(day|week)$")
    max_per_source: int = Field(20, ge=1, le=100)
    excluded_genres: list[str] = Field(default_factory=list)


class SubtitleSync(BaseModel):
    """Timing check of subtitles chosen without a hash match (needs ffsubsync + ffmpeg
    on PATH; missing tools are a skip, not a failure)."""

    model_config = _STRICT

    enabled: bool = False
    interval_minutes: int = Field(360, ge=1)
    max_per_run: int = Field(5, ge=1)  # each one reads a whole film over the NAS
    timeout_minutes: int = Field(15, ge=1)  # hard limit for one file
    min_offset_seconds: float = Field(0.3, gt=0)  # below this it's already in time
    max_offset_seconds: float = Field(60.0, gt=0)  # past this the result is distrusted


class Subtitles(BaseModel):
    """Subtitle coverage/automation (plan §5.5).

    ``backend`` selects the fetcher:

    * ``bazarr`` (default) — trigger Bazarr, which owns provider search + placement.
    * ``native`` — search providers ourselves (OpenSubtitles, ktuvit) and write the
      ``.srt`` next to each owned file, driven by our own catalog coverage. No
      Bazarr/Radarr/Sonarr required.
    """

    model_config = _STRICT

    languages: list[str] = Field(default_factory=lambda: ["he", "en"])
    # Cap search/download work per sweep so a big backlog doesn't burn provider
    # quotas in one run (OpenSubtitles free tier is a few downloads/day).
    max_searches_per_sweep: int = Field(50, ge=1)

    backend: Literal["bazarr", "native"] = "bazarr"
    # native sources to query, best-first: "opensubtitles" (he+en, reliable),
    # "ktuvit" (Hebrew specialist, needs a ktuvit.me account).
    sources: list[str] = Field(default_factory=lambda: ["opensubtitles"])
    # Where owned media lives so we can write subs beside it. None -> derive the
    # NAS path over SMB (unreliable on some NAS); set a local/mounted path.
    library_base_dir: str | None = None
    hearing_impaired: bool = False
    # Sent to OpenSubtitles as required by their API terms.
    opensubtitles_user_agent: str = "my-home-theater v1"
    # opensubtitles.org XML-RPC requires a *registered* user agent; the temporary
    # one is heavily rate-limited. Register yours and set it here.
    opensubtitles_org_user_agent: str = "TemporaryUserAgent"
    request_timeout: float = Field(20.0, gt=0)
    sync: SubtitleSync = Field(default_factory=SubtitleSync)

    @property
    def primary(self) -> str:
        return self.languages[0] if self.languages else "he"


class Taste(BaseModel):
    """Content-based library similarity (plan §9 'personal insights').

    Unsupervised — works from the owned catalog alone, no approve/reject labels
    needed. Discovery blends the similarity into candidate scores and reasons.
    """

    model_config = _STRICT

    enabled: bool = True
    min_library: int = Field(8, ge=2, description="min enriched owned titles per kind")
    neighbors: int = Field(5, ge=1, description="kNN size for the similarity score")
    weight: float = Field(0.5, ge=0, description="score blend: + weight*10*similarity")
    max_clusters: int = Field(8, ge=2, description="upper bound for auto-k clustering")
    # Trained preference classifier (homeTheater.preferences); blended only
    # once a model exists (needs enough approve/reject labels to train).
    model_weight: float = Field(0.5, ge=0, description="score blend: + weight*10*p(like)")


class Organizer(BaseModel):
    """Target library layout, pushed to Radarr/Sonarr/Bazarr with one click.

    Radarr/Sonarr own renaming (their template syntax below); Bazarr places
    subtitles into ``subs_folder`` inside each movie/season folder. The result:

    * ``Movies/<Title (Year)>/<file>`` + ``Movies/<Title (Year)>/Subs/``
    * ``TV Shows/<Series>/Season 01/<episodes>`` + ``.../Season 01/Subs/``
    """

    movie_folder_format: str = "{Movie Title} ({Release Year})"
    movie_file_format: str = "{Movie Title} ({Release Year}) {Quality Full}"
    series_folder_format: str = "{Series Title}"
    season_folder_format: str = "Season {season:00}"
    episode_file_format: str = "{Series Title} - S{season:00}E{episode:00} - {Episode Title}"
    subs_folder: str = "Subs"


class Torrent(BaseModel):
    """Native torrent acquisition (used only when ``acquisition.backend == 'torrent'``).

    We search a set of indexers directly, pick a release, and hand the magnet to a
    torrent client (Transmission). This bypasses the Radarr/Sonarr stack. It is OFF
    by default and the arr path remains the recommended one — see the plan's
    source-agnostic doctrine (§12). Content sourcing and legality are your
    responsibility; keep ``features.dry_run`` on until you trust a full run.

    Site base URLs are config (not code) because these mirrors move domains often;
    swap them here without touching the source clients.
    """

    # Which indexers to query. Known: "piratebay" (apibay JSON API, reliable),
    # "1337x" (HTML scrape, needs FlareSolverr for Cloudflare), "rarbg"
    # (best-effort against a clone; the original site is defunct).
    enabled_sources: list[str] = Field(default_factory=lambda: ["piratebay"])
    min_seeders: int = Field(5, ge=0, description="drop releases below this seeder count")
    # Allowed release resolutions; None -> reuse thresholds.allowed_resolutions.
    resolutions: list[str] | None = None
    # Refuse to start a grab when the download dir has less than this much free
    # space (GB), so a full local disk fails fast with a clear message instead of
    # ENOSPC mid-download. 0 disables the check.
    min_free_gb: float = Field(5.0, ge=0)
    # Only grab real HD: reject cam/telesync/telecine/screener sources, SD
    # containers (avi/xvid/divx), and releases with no detectable HD resolution
    # (the usual shape of a fake/foreign-dub "WEB-DL" with no res in its name).
    require_hd: bool = True
    max_results_per_source: int = Field(30, ge=1, le=200)
    # Download target dirs handed to the client; None -> the client's own default.
    movie_download_dir: str | None = None
    series_download_dir: str | None = None
    # Optional FlareSolverr proxy (http://host:8191) for Cloudflare-walled sources
    # (1337x). Unset -> those sources fetch directly and skip on a challenge.
    flaresolverr_url: str | None = None
    # Swappable mirror base URLs.
    piratebay_api_url: str = "https://apibay.org"
    x1337_base_url: str = "https://1337x.st"
    rarbg_base_url: str = "https://en.rarbg-official.is"
    request_timeout: float = Field(20.0, gt=0)
    # A download with no client-side progress for this long is treated as failed
    # (dead magnet / removed by hand in the client). Mirrors the arr path's guard.
    stale_after_hours: int = Field(6, ge=1)

    # --- Library import (runs after a torrent completes) ---
    # Copy the finished movie into the NAS layout (Movies/<Title (Year)>/<file>).
    import_to_library: bool = True
    # Where the library lives. None -> write to the NAS over SMB (nas.* + SMB_*).
    # Set to a local/mounted path (e.g. a mounted SMB share) to copy locally.
    library_base_dir: str | None = None
    # After a successful import, remove the torrent + its local files from the
    # client (a true "move"). Default False keeps the local copy seeding.
    delete_local_after_import: bool = False
    # Stop seeding once a torrent finishes: 0.0 = stop at completion (don't waste
    # upload bandwidth — the app isn't a seedbox); negative = seed indefinitely.
    seed_ratio_limit: float = 0.0


class DownloadWindow(BaseModel):
    """A nightly time window during which the *scheduled* acquire job may grab.

    ``enabled: false`` means "no window" — the scheduler grabs approved candidates
    every interval (the original behaviour). When enabled, the scheduled job only
    grabs during ``[start_hour, end_hour)`` in local time (wraps past midnight,
    e.g. 22→6). A manual "grab now" (CLI or the dashboard) always bypasses it.
    """

    model_config = _STRICT

    enabled: bool = False
    start_hour: int = Field(2, ge=0, le=23)
    end_hour: int = Field(6, ge=0, le=23)

    def is_open(self, hour: int) -> bool:
        if not self.enabled or self.start_hour == self.end_hour:
            return True
        if self.start_hour < self.end_hour:
            return self.start_hour <= hour < self.end_hour
        return hour >= self.start_hour or hour < self.end_hour  # wraps midnight


class Acquisition(BaseModel):
    """How approved candidates are grabbed (plan §5.6).

    ``backend`` selects the pipeline:

    * ``arr`` (default) — hand the title to Radarr/Sonarr, which own release
      selection (a *quality profile*), the download client, and import.
    * ``torrent`` — search indexers ourselves and push the magnet to Transmission
      (see :class:`Torrent`). Self-contained; no arr stack required.
    """

    model_config = _STRICT

    backend: Literal["arr", "torrent"] = "arr"
    movie_quality_profile: str = "HD-1080p"
    series_quality_profile: str = "HD-1080p"
    movie_root_folder: str | None = None  # None -> use the arr's first root folder
    series_root_folder: str | None = None
    search_on_add: bool = True
    # Optional nightly window for the scheduled acquire job (dashboard-editable).
    window: DownloadWindow = Field(default_factory=DownloadWindow)


class Kodi(BaseModel):
    """Library refresh and watch history over Kodi's JSON-RPC. Credentials are the
    KODI_USER / KODI_PASS secrets."""

    model_config = _STRICT

    enabled: bool = False
    url: str | None = None  # e.g. http://kodi.local:8080/jsonrpc
    # Kodi path prefix -> catalog prefix. A path no prefix covers is reported.
    path_map: dict[str, str] = Field(default_factory=dict)
    # Kodi's lastplayed has no zone. None -> the zone of the host running this app.
    timezone: str | None = None
    scan_debounce_seconds: int = Field(900, ge=0)  # quiet time after the last import
    scan_poll_minutes: int = Field(5, ge=1)  # how often a wanted scan is looked for
    history_interval_minutes: int = Field(60, ge=1)
    request_timeout: float = Field(10.0, gt=0)


class TraktSync(BaseModel):
    """Two-way watched state with Trakt (needs TRAKT_* and ``home-theater trakt-auth``)."""

    model_config = _STRICT

    enabled: bool = False
    push: bool = False  # also send Kodi's watched state to Trakt
    interval_minutes: int = Field(360, ge=1)


class Trakt(BaseModel):
    model_config = _STRICT

    sync: TraktSync = Field(default_factory=TraktSync)


class Trash(BaseModel):
    """The NAS safety net: what the app deletes from the library is renamed into
    ``<torrent.library_base_dir>/.home-theater-trash/`` and purged after
    ``retention_days``. ``enabled: false`` goes back to deleting outright."""

    model_config = _STRICT

    enabled: bool = True
    retention_days: int = Field(30, ge=1)


class Upgrades(BaseModel):
    """Replace an owned movie with a clearly better release (movies only).

    Off by default, and with ``auto: false`` every replacement waits for a click on
    the Candidates page. Needs the torrent backend, ``torrent.library_base_dir`` and
    the trash: the old file always goes to the trash, after the new one is verified.
    """

    model_config = _STRICT

    enabled: bool = False
    auto: bool = False  # grab without asking
    min_seeders: int = Field(10, ge=1)  # raised to torrent.min_seeders if that's higher
    cooldown_days: int = Field(30, ge=1)  # per title, between searches and after a swap
    max_titles_per_run: int = Field(10, ge=1)  # each one is a tracker search


class Secrets(BaseSettings):
    """Secrets from environment / ``.env``. Never logged, never serialized."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object, info: ValidationInfo) -> object:
        """Treat an empty/whitespace env var (e.g. ``OMDB_API_KEY=``) as unset.

        Otherwise pydantic builds ``SecretStr("")`` and downstream ``is not None``
        checks think the provider is configured — then every call 401s. Unset means
        the field's default, not None: ``DASHBOARD_REQUIRE_AUTH=`` is a bool.
        """

        if isinstance(v, str) and v.strip() == "" and info.field_name is not None:
            return cls.model_fields[info.field_name].default
        return v

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

    # Native torrent download client (used when acquisition.backend == 'torrent').
    transmission_url: str | None = None  # e.g. http://localhost:9091/transmission/rpc
    transmission_user: str | None = None
    transmission_pass: SecretStr | None = None

    # Native subtitle providers (used when subtitles.backend == 'native').
    opensubtitles_api_key: SecretStr | None = None
    opensubtitles_username: str | None = None
    opensubtitles_password: SecretStr | None = None
    # Legacy opensubtitles.org XML-RPC (separate account/password from .com).
    opensubtitles_org_username: str | None = None
    opensubtitles_org_password: SecretStr | None = None
    ktuvit_email: str | None = None
    ktuvit_password: SecretStr | None = None

    # Watchlist
    trakt_client_id: str | None = None
    trakt_client_secret: SecretStr | None = None

    # Kodi web server login (used when kodi.enabled).
    kodi_user: str | None = None
    kodi_pass: SecretStr | None = None

    # Notifications
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Dashboard auth (required before any mutating endpoint is exposed)
    dashboard_token: SecretStr | None = None
    # Lock the ENTIRE dashboard (read pages + APIs) behind the token, for when it
    # is exposed beyond a trusted LAN. Off by default (LAN reads stay open); when
    # on, browsers get an HTTP Basic prompt (password = DASHBOARD_TOKEN).
    dashboard_require_auth: bool = False
    # Separate secret for Radarr/Sonarr webhook URLs (?token=...). Falls back to
    # dashboard_token, but a distinct value keeps the dashboard token out of arr
    # configs and access logs.
    webhook_token: SecretStr | None = None


class Digest(BaseModel):
    """The daily Telegram summary. Runs under the scheduler, so it also needs
    ``schedule.enabled`` and a Telegram bot in ``.env``."""

    model_config = _STRICT

    enabled: bool = False
    hour: int = Field(8, ge=0, le=23)  # local time


class AppConfig(BaseModel):
    """The fully-resolved application configuration."""

    nas: NASPaths
    thresholds: Thresholds = Field(default_factory=Thresholds)
    features: FeatureFlags = Field(default_factory=FeatureFlags)
    schedule: Schedule = Field(default_factory=Schedule)
    server: Server = Field(default_factory=Server)
    scanner: Scanner = Field(default_factory=Scanner)
    database: Database = Field(default_factory=Database)
    metadata: Metadata = Field(default_factory=Metadata)
    discovery: Discovery = Field(default_factory=Discovery)
    subtitles: Subtitles = Field(default_factory=Subtitles)
    taste: Taste = Field(default_factory=Taste)
    organizer: Organizer = Field(default_factory=Organizer)
    acquisition: Acquisition = Field(default_factory=Acquisition)
    torrent: Torrent = Field(default_factory=Torrent)
    trash: Trash = Field(default_factory=Trash)
    upgrades: Upgrades = Field(default_factory=Upgrades)
    kodi: Kodi = Field(default_factory=Kodi)
    trakt: Trakt = Field(default_factory=Trakt)
    digest: Digest = Field(default_factory=Digest)
    enabled_providers: list[str] = Field(default_factory=list)
    secrets: Secrets = Field(default_factory=Secrets, repr=False)
