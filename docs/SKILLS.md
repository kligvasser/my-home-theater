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
kept alive by launchd/systemd.

## Module map (`src/homeTheater/`)

- `config/` — layered config. `settings.py` (pydantic models + `Secrets` from
  `.env`), `loader.py` (`get_config`, cached), `runtime.py` (`effective_config` =
  file config + DB overrides; only a whitelist of sections is overridable).
- `db/` — `models.py` (ORM), `session.py` (`session_scope()` context manager,
  SQLite WAL, `check_same_thread=False`).
- `scanner/` — read-only NAS walk (SMB or local) → `owned_file` rows via `guessit`.
- `metadata/` — TMDb + OMDb clients with a TTL cache.
- `discovery/` — gather → dedup → enrich → filter → rank → `candidate` rows.
- `acquisition/` — grab approved candidates. `service.py` dispatches by
  `acquisition.backend`; `arr.py` (Radarr/Sonarr) and `torrent/` (native).
- `subtitles/` — `service.py` dispatches by `subtitles.backend`; `bazarr.py` and
  `native/` (OpenSubtitles + ktuvit).
- `pipeline.py` — assembles the Activity view's per-candidate `ExecutionState`.
- `api/` — FastAPI routers; `web.py` renders the Jinja pages.
- `dashboard/queries.py` — all read-only dashboard queries (return dataclasses,
  never detached ORM objects).
- `health/checks.py` — backend-aware service probes for the Status page.
- `scheduler/` — APScheduler jobs, gated by `schedule.enabled`.

## Backends are seams, not forks

Both pluggable concerns dispatch on a config field at the top of the shared
service function, so callers (CLI, API, scheduler) never change:

- **Acquisition** (`acquisition.backend: arr | torrent`): `queue_candidate` /
  `sync_downloads` in `acquisition/service.py` delegate to `torrent/service.py`
  when `torrent`. The torrent path: `TorrentSource` search (apibay/1337x/rarbg) →
  `select.py` ranking → `TransmissionClient.add_magnet` → on completion, copy into
  the NAS via a `LibraryTarget`, register an `OwnedFile`, fetch its subtitles.
- **Subtitles** (`subtitles.backend: bazarr | native`): `sweep_subtitles` in
  `subtitles/service.py` delegates to `native/service.py` when `native`.
  `SubtitleSource`s are tried in the configured order; first hit wins.

When adding a source/client, implement the `Protocol` (`TorrentSource`,
`DownloadClient`, `LibraryTarget`, `SubtitleSource`) — never special-case a site
in the service layer.

## State machines

- **Candidate** (`CandidateStatus`): `new → approved → queued → downloading →
  imported`, plus `rejected` / `failed`. `rejected` is a training label — never
  silently resurrect it. Discovery skips titles that are owned OR have a candidate
  in any non-terminal state incl. `imported` (else an owned title re-appears as a
  fresh candidate).
- **Download** (`Download.state`, torrent backend): `queued → downloading →
  importing → imported`, plus `completed` (bytes down, import pending retry),
  `failed`, `cancelled`. `sync_downloads` re-polls `queued/downloading/importing/
  completed`, so an interrupted import retries next sweep.
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
  before deleting/overwriting NAS files.
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
- **SMB mount reliability (WD MyCloud).** The `smbprotocol` write path is
  unreliable on this NAS (rejects deletes with `STATUS_INVALID_PARAMETER`; guest
  writes don't survive a new session). **Write through the macOS SMB mount**
  (`library_base_dir: /Volumes/<share>` → `LocalLibraryTarget`). Large sustained
  copies can drop the guest mount; sync imports one file at a time and stops after
  the first failure so a mount drop doesn't cascade. Health checks the mount via
  the (cancellable) `mount` table, never `stat()` (a wedged mount hangs forever).
- **ktuvit login** isn't a plain hash: scrape the rotating `encryptionSalt` from
  the homepage, then `PBKDF2-HMAC-SHA1(salt, email, 3000, 16)` → AES-CBC encrypt
  the password (IV = email chars as hex bytes, 16-padded) → SHA256 → base64.
  Service requests are **nested objects** `{"request": {...}}`, not stringified.
  Series search needs `WithSubsOnly: false` or it returns nothing. Success = the
  `Login` cookie is set (an error body must not read as success).
- **OpenSubtitles.com** REST: query params must be **alphabetically sorted +
  lower-cased** or it 301-redirects. **Download needs a logged-in bearer token**
  (username+password), and the free tier is ~a few/day — surfaced on Status. Uses
  `.com` credentials (shared with `.org`).
- **OpenSubtitles.org** is the legacy XML-RPC API (uncapped, separate account,
  needs a *registered* User-Agent). Serialized with stdlib `xmlrpc.client` over
  async `httpx`; 3-letter lang codes (`heb`/`eng`); returns `False` (not `[]`) on
  no match; download links are gzip.
- **Transmission RPC** returns 409 with `X-Transmission-Session-Id` on the first
  call — echo it thereafter. A grab with no seeders sits at status 4 ("downloading")
  at 0% forever, so it's time-boxed to `failed` past the grace window.
- **Scraped magnets** (1337x/rarbg) arrive HTML-entity-encoded — `html.unescape`
  them or `&amp;` garbles tracker params.

## Verifying a change

Don't trust green tests alone for backend work — drive the real flow. Boot the app
(`.venv/bin/home-theater`, or a scripted `uvicorn` on a spare port) and hit the
endpoint, or call the service directly from a `.venv/bin/python -c` snippet against
the live DB/Transmission/NAS. Most bugs this project hit (TCC, mount drops, ktuvit
crypto, `.org` params) were invisible to unit tests and only showed up live.
