# Upgrade runbook — November 2026 audit

Moves the live install from `main` to the `audit-fixes-2026-11` code: a bare process and
an un-stamped database become a launchd service on the Alembic head. Findings are in
[`audit-2026-11.md`](audit-2026-11.md).

Run everything from the repo root. `database.url` is relative, and `.env` and
`config.yaml` are read from the working directory. Commit the branch first: rollback
needs `git checkout main` to work.

## What changes on first start

- **Stricter startup.** An unknown key in `thresholds`, `features`, `discovery`,
  `subtitles`, `taste`, `acquisition`, `trash`, `upgrades`, `kodi`, `trakt` or `digest`
  now stops the app, as does `DASHBOARD_REQUIRE_AUTH=true` with no token.
- **Requests.** A `Host` outside `server.allowed_hosts` (plus this machine's name and
  private IPs) gets a 400. A CSP applies, and `/docs` is off unless `HOME_THEATER_DOCS`.
- **Catch-up.** With `schedule.enabled`, `running` rows from dead processes are marked
  failed, then every job past its interval fires at once, and jobs with no recorded
  history count as overdue. Expect a scan, a subtitle sweep, sync, backup and a train
  within minutes. Acquire respects the window but otherwise grabs for real. A trigger
  you press meanwhile may answer "already running".
- **First sync.** `queued`/`downloading` candidates with no live download are settled
  (`failed`, or `approved` with a backoff for a season). A 0% download older than
  `torrent.stale_after_hours` fails as `stalled:` and its torrent and data are removed;
  one with progress starts its clock now. Hand-paused torrents are exempt.
- **Imports.** Existing library files are no longer replaced. A `.part` from the old
  code has no sidecar, so it starts over.
- **Scans.** A large prune is refused and shows in the Inbox. Series identity comes from
  the show folder, but existing splits stay until `repair-titles`.
- **Model.** The old preference model counts as no model until `train` succeeds.
- **Old labels stay.** Past Cancel and Unfollow rows remain `rejected`, so they still
  train as dislikes; the migrations don't convert them.
- **On by default:** the NAS trash (`trash.enabled`, 30-day purge), Inbox, Repair,
  Storage, and Telegram failure alerts if a bot is in `.env`.

## Steps

1. **Check the config loads**, before stopping anything:
   ```bash
   .venv/bin/python -c "from homeTheater.config import get_config; get_config()"
   ```

2. **Full backup.** The name doesn't match the rolling backup pattern, so it's never
   pruned. Keep it until the checklist below passes.
   ```bash
   mkdir -p data/backups
   sqlite3 data/home_theater.db ".backup data/backups/pre-audit-2026-11.db"
   sqlite3 data/backups/pre-audit-2026-11.db "PRAGMA integrity_check;"   # ok
   cp -p config.yaml data/backups/config.pre-audit-2026-11.yaml
   cp -p data/preference_model.joblib data/backups/preference_model.pre-audit-2026-11.joblib
   ```

3. **Stop the running app.** It's a bare process, not under launchd. Make sure Activity
   shows no import copying, then:
   ```bash
   lsof -nP -iTCP:8000 -sTCP:LISTEN     # note the pid
   kill <pid>
   lsof -nP -iTCP:8000 -sTCP:LISTEN     # nothing
   ```
   If a job ran after step 2, repeat the `.backup` line now.

4. **Create the index the live DB is missing:**
   ```bash
   sqlite3 data/home_theater.db "CREATE INDEX IF NOT EXISTS ix_title_followed ON title (followed);"
   ```

5. **Stamp, then upgrade.** The live DB has no `alembic_version` table.
   ```bash
   .venv/bin/alembic heads                  # c3e9a7d15f42 (head)
   .venv/bin/alembic stamp f4a1c2b9d3e6
   .venv/bin/alembic upgrade head
   .venv/bin/alembic current                # c3e9a7d15f42 (head)
   ```
   Each migration checks what exists first, so tables made earlier by `create_all` are
   fine. Duplicate subtitle and download rows are unlinked or cancelled, never deleted.

6. **Grant Full Disk Access first,** because the install starts the service at once and
   a first run without it can put a whole subtitle backlog into cooldown:
   ```bash
   .venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))'
   ```
   System Settings > Privacy & Security > Full Disk Access, press +, Cmd-Shift-G, and add
   that path: the real binary, not the `.venv` symlink. Recreating the venv on a new
   Python means granting again.

7. **Optional, for the timing check (F6):** the plist `PATH` is
   `/usr/bin:/bin:/usr/sbin:/sbin`, which has neither tool. Add `ffsubsync`'s folder and
   `/opt/homebrew/bin` (`ffmpeg`) to `PATH` in `deploy/com.homeTheater.app.plist`. The
   install copies that file, so don't edit the installed copy.

8. **Install and start:**
   ```bash
   deploy/install-launchd.sh --dry-run   # read what it will install
   deploy/install-launchd.sh             # refuses if port 8000 is still in use
   launchctl print gui/$(id -u)/com.homeTheater.app | head -30
   ```
   Logs are in `~/Library/Logs/home-theater`. If you granted access after installing:
   `launchctl kickstart -k gui/$(id -u)/com.homeTheater.app`.

## Verification checklist

Use the dashboard for steps 1–4: the service runs them, under its own privacy grants,
where a Terminal run wouldn't prove them.

- [ ] **Status:** `nas-mount` is up ("mounted at ..."), which means its write probe
      passed. A stale job on Status or `/health` can be catch-up still running.
- [ ] **One scan succeeds** (Activity, Rescan; result on Runs). A refused prune goes to
      the Inbox: accept it only if those files really are gone.
- [ ] **One subtitle sweep, then the ktuvit hit rate.** ktuvit's search field names are
      unverified. A total format change raises, but a wrong year field would quietly
      find nothing.
      ```bash
      sqlite3 data/home_theater.db "SELECT stats FROM job_run WHERE kind='subtitle' ORDER BY id DESC LIMIT 1;"
      sqlite3 data/home_theater.db "SELECT provider, COUNT(*) FROM subtitle WHERE lang='he' AND created_at > datetime('now','-2 hours') GROUP BY provider;"
      ```
      No `ktuvit` rows, with `disabled_sources` empty and many misses, means it matched
      nothing: compare a title that exists on ktuvit.me, and fix `_NAME_FIELDS`,
      `_YEAR_FIELDS` or `_IMDB_FIELDS` in `subtitles/native/ktuvit.py`. Misses are now in
      cooldown, so clear them afterwards: `DELETE FROM subtitle_attempt WHERE lang='he';`.
- [ ] **One small import:** grab one small movie and watch Activity reach Imported. The
      file is on the NAS, and its local copy is gone if `delete_local_after_import`.
- [ ] `.venv/bin/home-theater train`. "Not enough labels" is fine; the old model stays
      ignored until there are 20 decisions, 5 per class.
- [ ] `.venv/bin/home-theater enrich --force` retries titles missing a TMDb id or rating
      under the new search. It doesn't re-check titles that are already matched; fix
      those on the Repair page.
- [ ] `.venv/bin/home-theater repair-titles`, read the dry run, then `--apply`. Expect
      The Killing and Severance each folded into one title. `--apply` takes a backup
      and exits 2 if a scan or enrich is running. "check by hand" lines were not
      changed; merge those on the Repair page.

## Then

```bash
chmod 600 .env
.venv/bin/pre-commit install
```

Set `nas.mount_dir: /Volumes/<share>` in `config.yaml` before relying on scheduled scans.
Under launchd the service can't open its own LAN connections: macOS Local Network
privacy denies them with `[Errno 65] No route to host` and never prompts, or lists, a
command-line tool. Reads through the Finder mount aren't affected, and the mount walk
gives the catalog's exact UNC paths (checked: 1361 of 1361 on 2026-11-08), so nothing
is re-keyed. It refuses any volume that isn't this user's `smbfs` mount of `nas.share`
from `SMB_HOST` / `nas.connect_host`. Restart with
`launchctl kickstart -k gui/$(id -u)/com.homeTheater.app`.

## Off-by-default features

Set these in `config.yaml` and restart: the config is cached, optional jobs register at
startup, and none of these keys can be set from the Settings page. All need
`schedule.enabled` to run on a schedule. Tests mock every service, and no live run is
recorded for any of them.

| Feature | Enable | Also needs | Unverified |
|---|---|---|---|
| F2 Morning digest | `digest.enabled: true`, `digest.hour` | `TELEGRAM_*` | Real delivery and Telegram's HTML formatting |
| F4 Quality upgrades | `upgrades.enabled: true`; leave `auto: false` | torrent backend, `library_base_dir`, trash on | Source-tier reading on real filenames, and a real swap on the NAS. Run `home-theater upgrades` and review proposals first; it never downloads |
| F6 Subtitle timing | `subtitles.sync.enabled: true` | native backend, `ffsubsync` and `ffmpeg` on `PATH` (step 7) | Never run with real `ffsubsync`, which isn't installed yet, nor whether it installs on this Python. Missing tools are a skip noted on Status. Try `home-theater subtitle-sync` from a shell first |
| F7 Kodi | `kodi.enabled: true`, `kodi.url`, `kodi.path_map`, `kodi.timezone` | `KODI_USER`/`KODI_PASS`, Kodi's web server on | Never run against real Kodi. `path_map` values are untested, and unmatched paths are reported on Watched |
| F8 Trakt sync | `trakt.sync.enabled: true`; leave `push: false` | `TRAKT_*`, `home-theater trakt-auth` | Never run against real Trakt. `push` writes to your Trakt history, so check the pull on Watched first |

## Rollback

Old code can't read `cancelled` candidates or `partial` runs (enum validation fails on
load), so going back means restoring the database too, not just the code. Anything
recorded since the backup is lost.

1. `.venv/bin/home-theater trash list`, and `restore` what you want back. The old code
   doesn't know the trash: files there stay on the NAS, and nothing purges them.
2. `deploy/install-launchd.sh --uninstall`, while the script still exists.
3. Restore, removing the WAL so it can't replay onto the old file:
   ```bash
   rm -f data/home_theater.db-wal data/home_theater.db-shm
   cp -p data/backups/pre-audit-2026-11.db data/home_theater.db
   cp -p data/backups/preference_model.pre-audit-2026-11.joblib data/preference_model.joblib
   cp -p data/backups/config.pre-audit-2026-11.yaml config.yaml
   ```
4. `git checkout main`, and start it the old way.

Media and subtitles imported since stay on the NAS, and the next scan catalogs them.
