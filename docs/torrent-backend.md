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

## Limitations

- **Series import is not done here.** A completed series torrent is marked
  imported but its files are **left in the download dir** — per-episode placement
  into `TV Shows/<Series>/Season NN/` isn't modelled. Only movies are copied into
  the library. For per-episode management, use the arr backend.
- Import is a straight copy with the target name; it does not fetch subtitles or
  push naming templates (that's Bazarr/the arr stack). Run the existing subtitle
  sweep afterwards if you want subs.
