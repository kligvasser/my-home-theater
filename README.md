# my-home-theater

Automate a personal movie & TV library: cataloging, metadata/rating filtering,
subtitle coverage, source-agnostic acquisition, NAS organization, and an HTML
dashboard. Python-first, orchestrating a mature media-automation stack
(Radarr/Sonarr/Bazarr → Prowlarr/qBittorrent) rather than reinventing it.

See [`docs/my-home-theater-plan.md`](docs/my-home-theater-plan.md) for the full plan.

## Architecture (hybrid)

The app **drives Radarr/Sonarr/Bazarr** via their REST APIs; those own release
selection, the download client, import, renaming, and subtitle matching. This app
owns the parts that are genuinely custom: the **catalog**, **discovery + rating/
vote filtering**, the **scheduler**, and the **dashboard**. Radarr/Sonarr are the
single source of truth for "what do I own."

### Acquisition backends

`acquisition.backend` selects how approved candidates are grabbed:

- **`arr`** (default, recommended) — hand the title to Radarr/Sonarr; they own
  indexers (Prowlarr), the download client, import, and renaming.
- **`torrent`** — self-contained path with no arr stack: search indexers directly
  (The Pirate Bay via apibay, 1337x, RARBG-clone), push the chosen magnet to
  **Transmission**, then copy finished movies into the NAS library layout. See
  [`docs/torrent-backend.md`](docs/torrent-backend.md). Same `acquire`/`sync`
  commands and `dry_run` gate; movies-only import (series left in the download dir).

## Setup (conda)

```bash
conda env create -f environment.yaml
conda activate my-home-theater
pip install -e .            # editable install of the package + console script

cp config.example.yaml config.yaml   # edit paths/thresholds
cp .env.example .env                 # add secrets (API keys, SMB creds, token)
```

Generate a dashboard token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Run

```bash
home-theater                 # serve the dashboard/API (default)
home-theater scan            # scan the NAS -> owned catalog (Phase 1)
home-theater enrich          # backfill TMDb/IMDb metadata (Phase 2)
home-theater discover        # find candidates above your thresholds (Phase 4)
home-theater subtitles       # ask Bazarr to search for missing subs (Phase 5)
home-theater acquire         # queue approved candidates to Radarr/Sonarr (Phase 6)
home-theater sync            # advance in-flight download states (Phase 6)
home-theater reconcile       # reconcile Radarr/Sonarr owned items -> catalog (Phase 7)
home-theater backup          # write a timestamped SQLite backup (Phase 9)
# health:   http://localhost:8000/health
# readiness http://localhost:8000/ready
```

### Always-on (no Docker)

Run under conda and let the OS keep it alive:

- **macOS:** edit paths in [`deploy/com.homeTheater.app.plist`](deploy/com.homeTheater.app.plist),
  copy it to `~/Library/LaunchAgents/`, then `launchctl load` it.
- **Linux (mini-PC/NAS):** edit [`deploy/home-theater.service`](deploy/home-theater.service),
  copy to `/etc/systemd/system/`, then `systemctl enable --now home-theater`.

## Go-live checklist

Everything below is configuration, not code. Do it roughly in order.

1. **Install & configure**
   - `conda env create -f environment.yaml && conda activate my-home-theater && pip install -e .`
   - `cp config.example.yaml config.yaml` — set `nas.*` paths and `thresholds`.
   - `cp .env.example .env` — see the secrets checklist below.
   - `alembic upgrade head` (creates the schema from the migration baseline).

2. **Secrets in `.env`** (never committed)
   - `DASHBOARD_TOKEN` — generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
   - `TMDB_API_KEY`, `OMDB_API_KEY` — metadata + ratings.
   - `SMB_HOST` (IP beats flaky `.local`), `SMB_USER`, `SMB_PASS` — NAS scan.
   - `RADARR_URL`/`RADARR_API_KEY`, `SONARR_URL`/`SONARR_API_KEY`, `BAZARR_URL`/`BAZARR_API_KEY`.
   - Optional: `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` for alerts; `TRAKT_*` for a watchlist.
   - For the **torrent backend** only: `TRANSMISSION_URL`/`TRANSMISSION_USER`/`TRANSMISSION_PASS`.

3. **Prove the read path (safe, no writes/grabs)**
   - `home-theater scan` → `home-theater enrich`.
   - `home-theater` and open `http://localhost:8000/` — browse Library, and check
     **Status** (`/status`) shows your providers **up**.

4. **Discovery**
   - Tune `discovery` + `thresholds` in `config.yaml`; run `home-theater discover`.
   - Review the **Candidates** page. Approve via the token-gated API:
     `curl -X POST /api/candidates/<id>/approve -H "X-Auth-Token: $DASHBOARD_TOKEN"`.

5. **Acquisition — stays in dry-run until you trust it**
   - With `features.dry_run: true` (default), `home-theater acquire` only logs
     "would add …" — nothing is grabbed. Confirm the intent looks right.
   - Set matching Radarr/Sonarr **quality profile names** in `acquisition.*`.
   - Flip `features.dry_run: false` **only** after validating a real add with a
     legal / public-domain release. Then `acquire` → `sync`.

6. **Import reconciliation (webhooks)**
   - In Radarr/Sonarr, add a **Webhook** connection (On Import) to
     `http://<host>:8000/api/webhooks/radarr?token=$DASHBOARD_TOKEN` (and `/sonarr`).
   - Or poll: `home-theater reconcile`.

7. **Subtitles** — configure providers **inside Bazarr**; then
   `home-theater subtitles` (or the token-gated `POST /api/subtitles/search`).

8. **Unattended + durable**
   - Set `schedule.enabled: true` (tune intervals; `0` disables a job). The daily
     `backup` job runs automatically; `home-theater backup` runs one on demand.
   - Install the launchd/systemd unit (see *Always-on* above) so it survives reboots.

## Database migrations (Alembic)

```bash
alembic upgrade head                            # apply schema (production)
alembic revision --autogenerate -m "message"    # after model changes (SQLite batch mode)
```

Dev/test also has `init_db()` which creates tables directly.

## Develop

```bash
pytest            # unit + smoke tests (no external services hit)
ruff check .
mypy
pre-commit install
```

## Status

Phases 0–2 are in place:
- **Phase 0** — layered config, SQLAlchemy models + session (SQLite WAL),
  structured logging, FastAPI health/readiness, dashboard-auth dependency, conda
  env, launchd/systemd deploy templates, Alembic.
- **Phase 1** — read-only NAS scanner (SMB + local/fake filesystem), guessit
  parsing, subtitle sidecar detection, idempotent upserts, `home-theater scan`.
- **Phase 2** — TMDb + OMDb clients with a TTL cache, concurrent enrichment that
  backfills ids/ratings/votes/genres, `home-theater enrich`.
- **Phase 3** — read-only dashboard: Jinja2 pages (`/`, `/library`, `/runs`) with
  library stats, resolution/genre/decade breakdowns, Hebrew subtitle coverage, and
  search; plus a JSON API (`/api/stats`, `/api/titles`, `/api/runs`).
- **Phase 4** — discovery: TMDb trending/top-rated sources, threshold filter +
  rating×log(votes) scoring, dedup vs. owned/live candidates, review/auto modes,
  `home-theater discover`, a candidate-queue page, and a token-gated
  approve/reject/manual API (`/api/candidates`).
- **Phase 5** — subtitles: thin Bazarr client (read wanted, trigger search-missing),
  catalog-based coverage + missing-list, `/subtitles` page, `home-theater subtitles`,
  and a token-gated `POST /api/subtitles/search`.
- **Phase 6** — acquisition: Radarr/Sonarr `LibraryAutomation` clients, dry-run-gated
  `queue`/`sync` of approved candidates (they own Prowlarr/qBittorrent/import),
  `home-theater acquire`/`sync`, and a token-gated `POST /api/candidates/<id>/queue`.
- **Phase 7** — import reconciliation: idempotent `reconcile_import` from Radarr/Sonarr
  import webhooks (`POST /api/webhooks/{radarr,sonarr}?token=…`) that links the owned
  file and flips the candidate to `imported`, plus a `reconcile_library` poll and
  `home-theater reconcile`.
- **Phase 8** — scheduling + notifications: APScheduler periodic jobs
  (scan/discovery/subtitle/sync/reconcile) behind a global concurrency guard, started
  from `serve` when `schedule.enabled`; Telegram/log notifier for new candidates,
  imports, and job failures.
- **Phase 9** — hardening: provider health checks + `/status` page +
  `/api/{providers,status}`, SQLite online backup (`home-theater backup` + daily job),
  and a real Alembic initial migration baseline.

All nine phases are in place. Use `alembic upgrade head` in production; `init_db()`
covers dev/test.

**Native torrent backend (optional, `acquisition.backend: torrent`).** An
alternative to the arr stack: indexer clients (apibay/1337x/rarbg) behind a
`TorrentSource` seam, seeder/resolution release selection, a Transmission RPC
download client, and a NAS importer that copies finished movies into
`Movies/<Title (Year)>/`. No DB migration (reuses `download`). Details +
per-deployment notes in [`docs/torrent-backend.md`](docs/torrent-backend.md).
