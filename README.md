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
# health:   http://localhost:8000/health
# readiness http://localhost:8000/ready
```

### Always-on (no Docker)

Run under conda and let the OS keep it alive:

- **macOS:** edit paths in [`deploy/com.homeTheater.app.plist`](deploy/com.homeTheater.app.plist),
  copy it to `~/Library/LaunchAgents/`, then `launchctl load` it.
- **Linux (mini-PC/NAS):** edit [`deploy/home-theater.service`](deploy/home-theater.service),
  copy to `/etc/systemd/system/`, then `systemctl enable --now home-theater`.

## Database migrations (Alembic)

```bash
alembic revision --autogenerate -m "message"   # SQLite uses batch mode
alembic upgrade head
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

Subsequent phases (reconcile, scheduling) follow the plan.
