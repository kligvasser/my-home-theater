# Native torrent acquisition backend

An alternative to the Radarr/Sonarr path: the app searches torrent indexers
itself and pushes the chosen magnet to a Transmission download client. Enable it
by setting `acquisition.backend: torrent` in `config.yaml`.

> **Note on the design doctrine.** The plan (`docs/my-home-theater-plan.md` §12)
> deliberately keeps acquisition source-agnostic and routes it through Prowlarr.
> This backend intentionally departs from that: it talks to specific sites. It
> exists for a self-contained, no-arr-stack setup. The arr backend remains the
> default and the recommended path. You are responsible for your sources and
> their legality; keep `features.dry_run: true` until you've watched a clean run.

## How it works

1. `queue`/`acquire` takes each approved candidate, builds a search query from its
   title (and year, for movies), and searches every enabled source concurrently.
2. `select.py` drops under-seeded and wrong-resolution releases, then ranks the
   rest by resolution preference (your `thresholds.allowed_resolutions`, or
   `torrent.resolutions`) and seeder count, and picks one.
3. In dry-run it logs the intended grab and stops. Otherwise it hands the magnet
   to Transmission and records a `Download` row keyed by the torrent infohash.
4. `sync` polls Transmission and advances each download. When a **movie**
   finishes it is copied into the NAS library layout
   (`Movies/<Title (Year)>/<Title (Year)>.<ext>`), and only then does the
   candidate flip to `imported`. A failed copy leaves the download in
   `completed` and is retried on the next sweep — the candidate is not advanced
   until the file is in place.

## Library import

After a torrent completes, `sync` finds the primary video file (the file itself
for a single-file torrent, else the largest non-sample media file in the folder),
copies it into the library, and verifies the copy by size before it counts as
imported. Writes go to a `.part` sidecar and are atomically renamed into place.

| Config (`torrent:`) | Default | Effect |
|---|---|---|
| `import_to_library` | `true` | Copy finished movies into the NAS layout. |
| `library_base_dir` | `null` | `null` → write to the NAS over SMB (`nas.*` + `SMB_*` creds). Set a local/mounted path to copy there instead. |
| `delete_local_after_import` | `false` | `true` → remove the torrent + its local files after a successful import (a true "move"). `false` keeps the local copy **seeding**. |

**This is the app's only write path to the NAS** (the scanner is read-only).
Guest/password-less shares are often read-only for writes — if imports fail with
a permission error, set `SMB_USER`/`SMB_PASS` in `.env` to an account that can
write, or point `library_base_dir` at a locally-mounted copy of the share.

### Prefer the SMB mount on WD MyCloud (this deployment)

The direct `smbprotocol` write path (`library_base_dir: null`) proved unreliable
on the WD MyCloud EX2 Ultra: the NAS rejects `smbclient` deletes
(`STATUS_INVALID_PARAMETER`) and guest-session writes don't survive into a new
connection. The fix — already set in `config.yaml` — is to import through the
macOS SMB mount, the same access Kodi/Finder use:

```yaml
torrent:
  library_base_dir: /Volumes/Elements_25A1-1
```

`LocalLibraryTarget` then copies via the OS mount (durable write, atomic rename,
working delete — verified end-to-end). Ensure the share is mounted
(`/Volumes/Elements_25A1-1`) before a sync runs.

Nothing about the CLI, scheduler, dashboard, or `dry_run` gate changes; only the
grab/poll mechanics differ. `home-theater acquire` and `home-theater sync` work
for whichever backend is selected.

## Sources

| Source      | Mechanism                         | Reliability | Notes |
|-------------|-----------------------------------|-------------|-------|
| `piratebay` | apibay.org JSON API               | High        | No scraping, no Cloudflare — the anchor. |
| `1337x`     | HTML scrape (search + detail page)| Medium      | Behind Cloudflare → needs FlareSolverr. |
| `rarbg`     | HTML scrape of a clone            | Low / experimental | Original RARBG is defunct; clones are unstable. |

Keep `piratebay` enabled even when you add the others.

### Cloudflare / FlareSolverr

1337x returns a JS challenge to a plain request. Run FlareSolverr (the same proxy
Prowlarr uses) and point the config at it:

```
docker run -d -p 8191:8191 ghcr.io/flaresolverr/flaresolverr
```
```yaml
torrent:
  flaresolverr_url: http://localhost:8191
```

Without it, `1337x`/`rarbg` are skipped whenever they hit a challenge (logged,
never fatal).

## Setup

1. Install Transmission and enable its Web/RPC interface. Set credentials in
   `.env` (`TRANSMISSION_URL/USER/PASS`); the URL defaults to
   `http://localhost:9091/transmission/rpc`.
2. In `config.yaml`, set `acquisition.backend: torrent` and configure the
   `torrent:` block (see `config.example.yaml`).
3. Keep `features.dry_run: true`, approve a legal test title, run
   `home-theater acquire`, and confirm the logged `acquire.dry_run` picks a sane
   release.
4. Flip `dry_run: false` and run `acquire` then `sync`; watch the candidate move
   `queued → downloading → imported`.

## Download location on macOS (important)

Point `torrent.movie_download_dir` / `series_download_dir` at a plain folder like
`/Users/<you>/HomeTheaterDownloads` — **not** `~/Downloads`, `~/Desktop` or
`~/Documents`. Those are privacy-protected (TCC): Transmission can write there, but
this app (terminal/launchd) is **denied read access**, so import fails with
"permission denied reading …". If you have torrents stuck in a protected folder,
the app can move them out via Transmission (`torrent-set-location`) — restart the
item, or set the download dir and re-grab.

Large imports copy over SMB one at a time; if the mount drops mid-copy the sync
stops importing for that run and retries the rest next sweep (so one failure
doesn't cascade). If big copies repeatedly break the mount, prefer an
authenticated SMB mount over guest.

## Watching the pipeline (dashboard)

The **Activity** page (`/activity`) shows every in-flight candidate as a live
stepper — **Grabbed → Downloading → Imported to NAS → Subtitles** — with live
progress, seeders, speed and ETA polled from Transmission, plus per-language
subtitle coverage. A compact stepper also appears on each in-flight candidate
card. The page exposes manual stage triggers: **Grab approved now**, **Advance
downloads** (sync), **Rescan NAS**, and **Fetch subtitles**.

**When to grab.** `acquisition.window` is an optional nightly window (editable on
the Settings page) that the *scheduled* acquire job respects; **Grab now** on a
candidate and **Grab approved now** on Activity always bypass it. Approve leaves a
candidate "scheduled" (grabbed in the next window); "Grab now" is immediate.

**Stop seeding after import.** With `torrent.delete_local_after_import: true`, a
movie is removed from Transmission (and its local copy deleted) once it's copied
to the NAS, so it no longer uploads.

## Season grabs

A season-scoped candidate (discovery's "new season of a series you own") grabs
the season pack when one exists — releases are **season-verified** with guessit,
so a `Title Season N` search can't grab another season's pack. While a season is
still airing there is no pack, so the backend grabs the available episodes one
release each and the candidate returns to `approved` when they finish; the next
acquire run tops up newly-aired episodes (coverage tracked against the season's
announced episode count, `features.season_episodes`) until the season completes.

A completed series torrent (single episode or a whole pack) is imported the same
way movies are: each media file is copied into `TV Shows/<Series>/Season NN/`
(keeping its release filename — that's what the scanner parses), cataloged as an
owned file with season/episode, and its subtitles fetched (native backend). An
existing series folder is reused even when its name is styled differently
(`Colin.From.Accounts` vs `Colin From Accounts`), so a show never splits across
two folders.

## Limitations

- Import is a straight copy with the target name; it does not push naming
  templates (that's Bazarr/the arr stack).
