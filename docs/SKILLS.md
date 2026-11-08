# Working on my-home-theater

A practical guide for anyone (human or agent) making changes here: how the system
is shaped, the conventions to follow, and the platform gotchas that cost real time
to rediscover. Read this before touching acquisition, subtitles, or the dashboard.

## What it is

A personal movie/TV automation app. It owns the genuinely custom parts — the
**catalog**, **discovery** (rating/vote filter + a learned taste model), the
**scheduler**, and an **HTML dashboard** — and delegates the commodity parts
(acquisition, subtitles) to pluggable backends. SQLite + SQLAlchemy 2.0, FastAPI +
Jinja, `httpx` for all outbound calls. No Docker; runs under a `.venv` or conda,
kept alive by launchd (`deploy/install-launchd.sh`) or systemd.

## Module map (`src/homeTheater/`)

- `config/` — layered config. `settings.py` (pydantic models + `Secrets` from
  `.env`), `loader.py` (`get_config`, cached), `runtime.py` (`effective_config` =
  file config + DB overrides; only an allow-list is overridable). Sections a
  dashboard override can reach use `extra="forbid"`, so an unknown key fails at
  startup. `get_config` is cached: `config.yaml` changes need a restart.
- `db/` — `models.py` (ORM), `session.py` (`session_scope()` context manager,
  SQLite WAL, `check_same_thread=False`).
- `scanner/` — read-only NAS walk (SMB or local) → `owned_file` rows via `guessit`.
  A series file takes its title and year from its top-level show folder
  (`parse.series_identity`), and only season/episode from the filename; a known path
  keeps its title link. A walk error fails the run, and a large prune is refused
  (`scanner.*`) until `scan --force-prune` or the Inbox accepts it. `repair.py` folds
  old filename splits back into one title (`repair-titles`, the Repair page).
- `metadata/` — TMDb + OMDb clients with a TTL cache. TMDb search widens from the exact
  year to ±1 to none, and `names.py` accepts a result only if a name matches (empty
  answers aren't cached). `service.merge_titles` folds duplicates: flags are ORed and,
  with `fill_metadata`, only gaps are filled.
- `discovery/` — gather → dedup → enrich → filter → rank → `candidate` rows.
- `acquisition/` — grab approved candidates. `service.py` dispatches by
  `acquisition.backend`; `arr.py` (Radarr/Sonarr) and `torrent/` (native).
- `subtitles/` — `service.py` dispatches by `subtitles.backend`; `bazarr.py` and
  `native/` (OpenSubtitles + ktuvit; `payload.py` is the single validation gate,
  `ranking.py` orders results). `runs.py` is shared `job_run` bookkeeping, `choices.py`
  records what was chosen and rejected, `sync.py` is the ffsubsync timing check,
  `review.py` is approve/swap/revert.
- `pipeline.py` — assembles the Activity view's per-candidate `ExecutionState`.
- `api/` — FastAPI routers; `web.py` renders the core Jinja pages, and `inbox`,
  `repair`, `storage`, `trash`, `watch` own their features. `security.py` is the `Host`
  allowlist and security headers (CSP: no inline script or `<style>`).
- `web/static/` — `app.js`, plus `ops.js` for the Inbox and Repair pages and nav badge.
- `dashboard/queries.py` — all read-only dashboard queries (return dataclasses,
  never detached ORM objects).
- `health/checks.py` — backend-aware service probes for the Status page. `nas-mount`
  reads the `mount` table, then runs a write probe in a subprocess that can be killed.
- `scheduler/` — APScheduler jobs, gated by `schedule.enabled`. `JOB_RESOURCES` in
  `jobs.py` gives each job a per-resource `asyncio.Lock`: `sync`, `acquire`, `reconcile`
  and `trash` share `downloads`, `subtitle` and `subtitle_sync` share `subtitles`, the
  two Kodi jobs share `kodi`, and an unlisted job shares `default`. Each job has a
  wall-clock deadline (`schedule.job_timeout_minutes`) so a hung NAS job can't hold its
  resource. `catchup.py` runs overdue jobs at startup and after a wake, judged from
  `job_run`, reaps `running` rows of dead processes, and feeds per-job freshness.
  The `train` job retrains the model daily and skips while there are too few labels.
- `notifications/` — Telegram, or the log without a bot. `alerts.py` alerts on a job's
  first failure and sends one recovery message, with state in the `setting` table.
- `digest.py` — daily Telegram summary (F2, off by default).
- `inbox.py` — what's waiting on the owner, and the fix for each (F3).
- `storage.py` — read-only NAS forecast and removal suggestions (F9).
- `kodi.py` — debounced library scan after imports, watch history (F7, off by default).
- `watch.py` — `watch_state`: merges Kodi and Trakt plays, pushes to Trakt if enabled,
  archive suggestions. `trakt.py` is the client: device-flow auth, watchlist, watched
  history and history push (F8, off by default).
- `reconcile/` — apply Radarr/Sonarr import webhooks/polls to the catalog
  (season-aware: an S3 import completes the S3 candidate, not any live sibling).
- `cleanup.py` — dry-run-first janitor: send NAS extras to the trash and remove
  stuck torrents (verified imports only).
- `trash.py` — every NAS delete: rename into `.home-theater-trash`, restore, purge.
- `upgrades.py` — propose clearly better copies of owned movies (candidates with
  `source=upgrade`); the swap itself is in `importer.retire_replaced_file`.
- `locks.py` — cross-process advisory `flock` (`job_lock`), so a manual or dashboard
  run can't race the scheduled one. Names: `scan`, `enrich`, `discovery`, `acquire`,
  `sync` (also cleanup and trash), `subtitles`, `subtitle-sync`, `upgrades`, `digest`,
  `kodi-sync`, `trakt-sync`; catalog repair takes `scan` + `enrich`. Separately,
  importers hold `import-copy` shared (`import_active`) as a hint, never a gate:
  scheduled scans wait up to 15 minutes then skip, subtitle sweeps wait between items,
  and the timing check and Kodi scan hold off, because the NAS throws EIO when work
  overlaps an import.

## Backends are seams, not forks

Both pluggable concerns dispatch on a config field at the top of the shared
service function, so callers (CLI, API, scheduler) never change:

- **Acquisition** (`acquisition.backend: arr | torrent`): `queue_candidate` /
  `sync_downloads` in `acquisition/service.py` delegate to `torrent/service.py`
  when `torrent`. The torrent path: `TorrentSource` search (apibay/1337x/rarbg) →
  `select.py` ranking (season/episode-verified; executable-named releases and
  quarantined infohashes dropped) → post-grab metadata screen (remove lone-exe /
  no-media fakes before they download) → `TransmissionClient.add_magnet` → on
  completion, copy into the NAS via a `LibraryTarget` (`.part` resumed only when its
  `.part.meta` sidecar matches, read back before it counts, EIO backoff, an existing
  file never overwritten), register an `OwnedFile`, fetch its subtitles.
  Series: a whole-series candidate expands into one candidate per aired season;
  each grabs the season pack or, for an airing season, tops up episodes as they
  air (against `features.season_episodes`, completing once the show has ended).
  A **followed** series (`Title.followed`) auto-grabs new seasons via discovery.
- **Subtitles** (`subtitles.backend: bazarr | native`): `sweep_subtitles` in
  `subtitles/service.py` delegates to `native/service.py` when `native`.
  `SubtitleSource`s are tried in the configured order; the first valid subtitle wins.

When adding a source/client, implement the `Protocol` (`TorrentSource`,
`DownloadClient`, `LibraryTarget`, `SubtitleSource`) — never special-case a site
in the service layer.

## State machines

- **Candidate** (`CandidateStatus`): `new → approved → queued → downloading →
  imported`, plus `rejected` / `failed` / `cancelled`. `rejected` is a training label —
  never silently resurrect it. `cancelled` means the owner stopped the work (Cancel,
  Unfollow): no label, discovery won't re-suggest it, and following the show reopens
  its seasons. Discovery skips titles that are owned OR have a candidate in any live
  state incl. `imported` (else an owned title re-appears as a fresh candidate), but
  reuses a `failed` row after a backoff (`retry_due`: 1, 2, 4... days, at most 14). A
  season that loses its last download returns to `approved` with a `GrabRetry` backoff
  (1 h doubling to 24 h, 5 tries), then `failed`; a movie goes straight to `failed`.
  Sync settles `queued`/`downloading` candidates with no live download. `source=upgrade`
  candidates are never labels. Candidates are season-scoped for series (`Candidate.season`):
  the owned/live/rejected invariants apply per `(title, season)`, so rejecting S3
  never buries a future S4, and a season top-up returns the candidate to
  `approved` while episodes are still missing.
- **Download** (`Download.state`, torrent backend): `queued → downloading →
  importing → imported`, plus `completed` (bytes down, import pending retry),
  `failed`, `cancelled`. `sync_downloads` re-polls `queued/downloading/importing/
  completed` (`LIVE_DOWNLOAD_STATES`). A failed import stays `completed` and retries
  with a growing gap (15 min doubling to 6 h, 5 tries, NAS faults not counted).
- **Download markers** live in `Download.error` (constants in `db/models.py`), so a
  parked row keeps its state. `removed:` is the ban record of a release thrown out as
  malware or junk, in any state. `stalled:` is a `failed` row whose hash went dead;
  it's re-selected only as a last resort. `held:` is `completed` with a real video left
  unimported: local data stays and every sweep looks again. `needs_attention:` is
  `completed` after the import gave up (attempt cap or library conflict): sweeps skip
  it until someone fixes it and uses Retry import (`retry_import`). `local copy removal
  pending` is an exact match on an `imported` row whose torrent removal is still to do.
- **Activity view** shows only mid-flight candidates (`_ACTIVE`); once `imported`
  they drop off. Subtitle backfill is best-effort and reported on Library/Subtitles
  — a title may legitimately never get a Hebrew sub.

## Conventions

- **Config vs secrets.** Non-secret config in `config.yaml` (committable via
  `config.example.yaml`); secrets in `.env` only. Passwords with `$`/`#`/`!` must
  be **single-quoted** in `.env` or dotenv mangles them.
- **DB.** Always `with session_scope() as s:`. Blocking DB in an `async def` route
  must go through `asyncio.to_thread` (see `pipeline.activity`, `status_page`) —
  don't stall the event loop. Reassign JSON list columns (e.g. `subtitle_langs`) to
  a new list so SQLAlchemy marks them dirty.
- **Errors.** `redact_exc(exc)` before logging/storing/displaying any exception
  (httpx messages embed credential-bearing URLs). `NotConfiguredError` for missing
  creds (callers skip/503), `InvalidTransitionError` for illegal state changes.
  Never let one bad item sink a whole sweep — catch per-item, keep going.
- **Safety.** `features.dry_run` gates every grab. Default to non-destructive; look
  before deleting/overwriting NAS files. A NAS delete goes through
  `trash.move_to_trash`, never `os.remove`; only temp files and 0-byte leftovers are
  removed directly.
- **Formatting.** The formatter is **black** (line-length 100) + **ruff** + **mypy
  strict-ish**. Format only files you touch — the repo has some pre-existing
  black/ruff-format drift; a tree-wide `ruff format` creates churn.
- **Tests.** `pytest` with `respx` mocking all HTTP; each test resets the config
  cache + DB engine (`_reset()`), writes a tmp `config.yaml`, seeds via
  `session_scope`. No test hits the network.

## Platform gotchas (the expensive ones)

- **macOS TCC / protected folders.** A terminal/launchd process is **denied read
  access** to `~/Downloads`, `~/Desktop`, `~/Documents`. Transmission can write
  there, but the app can't read it → imports fail with a *misleading* empty-folder
  error. Fix: set `torrent.movie_download_dir` to a plain folder (e.g.
  `~/HomeTheaterDownloads`). `find_primary_video` now raises a clear permission
  error instead of "no media file". Existing torrents can be relocated via
  Transmission's `torrent-set-location`.
- **launchd.** Install with `deploy/install-launchd.sh` (`--dry-run` prints the plist,
  `--uninstall` removes it). It starts the service straight away and refuses while
  anything holds the port. TCC judges the real interpreter, so Full Disk Access goes to
  `realpath(.venv/bin/python)`, not the symlink, and again after recreating the venv on
  a new Python. `home-theater-launchd.sh` waits for the mount, holds `caffeinate -i`,
  rotates logs (`~/Library/Logs/home-theater`) and execs the app. The plist `PATH` is
  system folders only, so optional tools (`ffsubsync`, `ffmpeg`) need their folders added.
- **SMB mount reliability (WD MyCloud).** The `smbprotocol` write path is
  unreliable on this NAS (rejects deletes with `STATUS_INVALID_PARAMETER`; guest
  writes don't survive a new session). **Write through the macOS SMB mount**
  (`library_base_dir: /Volumes/<share>` → `LocalLibraryTarget`). Large sustained
  copies can drop the guest mount; sync imports one file at a time and stops for the
  sweep on a NAS fault so a mount drop doesn't cascade. Health checks the mount via
  the (cancellable) `mount` table and a killable write probe, never `stat()` in
  process (a wedged mount hangs forever). An empty folder under `/Volumes` isn't the
  share, so it's never probed. `nas.connect_host` pins the scanner's socket to an IP
  when `.local` resolution is flaky; catalog paths keep `SMB_HOST` either way.
- **Scanning under launchd** goes through the mount (`nas.mount_dir`): macOS Local
  Network privacy silently denies a launchd tool's own SMB sockets (`Errno 65`). The
  walk runs in a killable `python -I -S` child with an empty environment, skips
  symlinks and nested volumes, checks the volume is this user's smbfs mount of the
  share (`statfs`), and maps names back to smbclient's form in `nas_paths.py`: NFC,
  and Services-for-Mac code points (a trailing dot is `\uf029` over SMB). Everything
  that turns a UNC path into a mount path goes through `nas_paths`.
- **ktuvit login** isn't a plain hash: scrape the rotating `encryptionSalt` from
  the homepage, then `PBKDF2-HMAC-SHA1(salt, email, 3000, 16)` → AES-CBC encrypt
  the password (IV = email chars as hex bytes, 16-padded) → SHA256 → base64.
  Service requests are **nested objects** `{"request": {...}}`, not stringified.
  Series search needs `WithSubsOnly: false` or it returns nothing. Success = the
  `Login` cookie is set (an error body must not read as success). Search result field
  names (`_NAME_FIELDS` etc.) are undocumented guesses: a total format change raises,
  but a wrong year field would just find nothing, so watch the Hebrew hit rate.
- **OpenSubtitles.com** REST: query params must be **alphabetically sorted +
  lower-cased** or it 301-redirects. **Download needs a logged-in bearer token**
  (username+password), and the free tier is ~a few/day — surfaced on Status. `.org`
  has its own `OPENSUBTITLES_ORG_*` login.
- **OpenSubtitles.org** is the legacy XML-RPC API (uncapped, separate account,
  needs a *registered* User-Agent). Serialized with stdlib `xmlrpc.client` over
  async `httpx`; 3-letter lang codes (`heb`/`eng`); returns `False` (not `[]`) on
  no match; download links are gzip.
- **Transmission RPC** returns 409 with `X-Transmission-Session-Id` on the first
  call — echo it thereafter. A grab with no seeders sits at status 4 ("downloading")
  forever, so a torrent with no progress at any percentage fails after
  `torrent.stale_after_hours` of watched time (`stalled:`) and leaves the client. Time
  queued in Transmission, hand-paused torrents, and long gaps between sweeps (host
  asleep) don't count. `torrent-add` ignores seed limits; they need a `torrent-set`.
- **Scraped magnets** (1337x/rarbg) arrive HTML-entity-encoded — `html.unescape`
  them or `&amp;` garbles tracker params.

## Verifying a change

Don't trust green tests alone for backend work — drive the real flow. Boot the app
(`.venv/bin/home-theater`, or a scripted `uvicorn` on a spare port) and hit the
endpoint, or call the service directly from a `.venv/bin/python -c` snippet against
the live DB/Transmission/NAS. Most bugs this project hit (TCC, mount drops, ktuvit
crypto, `.org` params) were invisible to unit tests and only showed up live.
