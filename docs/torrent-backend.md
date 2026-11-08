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
2. `select.py` drops under-seeded, wrong-resolution and wrong-title releases (the
   whole release title must match, for movies and seasons alike), and sizes outside
   300 MiB–150 GiB for a movie, 40 MiB–40 GiB for an episode, 200 MiB–500 GiB for a
   pack; an unreported size passes. It ranks the rest: hashes that went dead last,
   then the surest year, resolution preference (your `thresholds.allowed_resolutions`,
   or `torrent.resolutions`) and seeder count.
3. In dry-run it logs the intended grab and stops. A grab is refused unless the
   download disk keeps `torrent.min_free_gb` after this release and everything still
   downloading. Otherwise it hands the magnet to Transmission and records a `Download`
   row keyed by the torrent infohash.
4. `sync` polls Transmission and advances each download. When a **movie**
   finishes it is copied into the NAS library layout
   (`Movies/<Title (Year)>/<Title (Year)>.<ext>`), and only then does the
   candidate flip to `imported`. A failed copy leaves the download in `completed`
   and retries with a growing gap (see below) — the candidate is not advanced until
   the file is in place.

A download with no progress for `torrent.stale_after_hours` fails as `stalled:` and
leaves Transmission, at any percentage. Time queued in Transmission and long gaps
between sweeps (host asleep) don't count, and a hand-paused torrent is exempt.
A stalled hash stays eligible but ranks last. A movie then shows as `failed`; a season
returns to `approved` and is re-grabbed after 1 h, doubling to 24 h, for 5 tries.

## Library import

After a torrent completes, `sync` finds the primary video file (the file itself
for a single-file torrent, else the largest non-sample media file in the folder) and
copies it into the library. Writes go to `<name>.part`, then atomically rename into
place. A copy counts only after a read-back: the size, plus sampled chunks (head,
tail, three in the middle, or the whole of a small file) compared with the source, read
again from the NAS at the final path with the mount still there. A file already at the
destination has to pass the same check.

An existing library file is never overwritten. A different file there parks the import
as `needs_attention:` with both copies kept. A torrent with a real video that didn't
import (say, an episode with no parsable name) is `held:`: what did import is cataloged,
local data stays, and every sweep looks again.

A failed import retries after 15 minutes, doubling to 6 hours. After 5 failures of the
item itself it parks as `needs_attention:` until someone fixes it and presses **Retry
import**, which downloads nothing. NAS faults (link errors, full or read-only share)
don't count against the item; they back off the whole sweep instead.

| Config (`torrent:`) | Default | Effect |
|---|---|---|
| `import_to_library` | `true` | Copy finished movies into the NAS layout. |
| `library_base_dir` | `null` | `null` → write to the NAS over SMB (`nas.*` + `SMB_*` creds). Set a local/mounted path to copy there instead. |
| `delete_local_after_import` | `false` | `true` → remove the torrent + its local files after a successful import (a true "move"). `false` keeps the local copy **seeding**. |
| `seed_ratio_limit` | `0.0` | Stop seeding once a torrent completes (`0.0`), so it doesn't keep uploading before the post-import removal runs. Set per torrent with `torrent-set` after the add. Negative = seed indefinitely. |

The scanner never writes to the NAS. Besides this import, the subtitle backend writes
into `Subs/`, the health probe writes a temp file, and cleanup, upgrades and the trash
rename files.

Copies **resume** through the mount (`library_base_dir` set): if the SMB link drops
mid-transfer (WD MyCloud does this on sustained multi-GB writes, surfacing as `[Errno
5] Input/output error`), the partial `.part` is kept and the next sweep appends from
where it stopped, so a large file completes across several sweeps. It resumes only when
the `.part.meta` sidecar proves the same source (size, mtime, infohash) and the last
bytes match; anything else starts over. The direct SMB target never resumes. A
transport fault also backs the whole sweep off so a faulting share isn't hammered
file-by-file.

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

### Malware / fake-file guard

Some "releases" are executables with a clean-looking listing name (e.g.
`Movie 2026 1080p WEB-DL .exe`, or an `.exe` inside an otherwise normal folder).
Three layers block them:

1. **Selection** drops releases whose *name* is executable-styled.
2. **Grab screening** — right after a magnet is added, the backend waits for the
   torrent's metadata (the file list, which transfers before any content) and
   removes it immediately if it contains an executable or no media at all, so
   the payload is never downloaded. The infohash is banned from re-selection.
3. **Sync** re-scans every in-flight torrent's file list as a backstop and
   quarantines anything the screen missed before it can be imported.

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
stops importing for that run and picks up on a later sweep (so one failure doesn't
cascade). If big copies repeatedly break the mount, prefer an authenticated SMB mount
over guest.

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
finished download is removed from Transmission (and its local copy deleted) after
`imported` commits, so it no longer uploads. A crash in between leaves a `local copy
removal pending` marker the next sweep finishes, and removal waits while the mount is
gone. A `held:` or `needs_attention:` download keeps its local data.

## Season grabs

**Following a series** (the ★ Follow button on a series card or search result)
monitors it and **auto-grabs** new seasons/episodes as they air — even a show you
own nothing of yet backfills every aired season. It works by marking the title
`followed`; the new-seasons discovery source then emits *approved* season
candidates for followed shows (owned shows still emit plain suggestions), which
the acquire job grabs and the season top-up keeps current.

Approving a **series** (from trending, the watchlist, or the search box) means the
whole series: at grab time it is expanded into one candidate per aired season,
so a show never arrives as a single stray episode. Each season candidate then
follows the rules below; seasons that haven't started airing wait for the
"new season" discovery source to surface them later.

A season-scoped candidate (discovery's "new season of a series you own") grabs
the season pack when one exists — releases are **season-verified** with guessit and
must carry the show's name, so a `Title Season N` search can't grab another show or
another season's pack. While a season is still airing there is no pack, so the backend
grabs the available episodes one release each and the candidate returns to `approved`
when they finish; the next acquire run tops up newly-aired episodes (coverage is what
actually reached the library, against the season's announced episode count,
`features.season_episodes`, and episodes already on the NAS are never re-downloaded)
until the season completes. With a known count it probes every episode up to it. A
finished season left with a hole where a download failed ends `failed`, so Retry shows;
an ended show that merely falls short of its announced count completes.

A completed series torrent (single episode or a whole pack) is imported the same
way movies are: each media file is copied into `TV Shows/<Series>/Season NN/`
(keeping its release filename — that's what the scanner parses), cataloged as an
owned file with season/episode, and its subtitles fetched (native backend). An
existing series folder is reused even when its name is styled differently
(`Colin.From.Accounts` vs `Colin From Accounts`), so a show never splits across
two folders.

## Cleanup

`home-theater cleanup` (dry run) / `--apply`, or the 🧹 **Cleanup** panel on the
Status page, finds and removes two kinds of junk safely:

- **NAS extras** — samples, trailers and featurettes in the TV library, on positive
  evidence only (an extras folder, or the word in the filename, never a file with
  an episode number). Found by walking the mounted library.
- **Stuck torrents** — completed torrents Transmission still holds whose every
  download is imported with its NAS copy verified. Anything else is reported as
  unmanaged and never removed.

Nothing is removed without an explicit `--apply` / **Apply** press, and NAS extras go
to the trash, not away.

## Trash

Everything the app removes from the library is renamed into
`<library_base_dir>/.home-theater-trash/<date>/<original relative path>`: same volume,
so instant, and never a copy. If the rename can't happen the file stays where it is and
the failure is reported; a file is never deleted instead. The scanner and the extras
finder skip the folder (it's a dot-directory).

| Config | Default | Effect |
|---|---|---|
| `trash.enabled` | `true` | `false` makes cleanup delete outright, and blocks upgrades. |
| `trash.retention_days` | `30` | Purged this long after trashing (minimum 1). |
| `schedule.trash_interval_minutes` | `1440` | How often the purge job runs. |

`home-theater trash list | restore <id> | purge [--dry-run]`, the Trash section on the
Status page, or the token-gated `GET /api/trash`, `POST /api/trash/{id}/restore` and
`POST /api/trash/purge`. Restore refuses to overwrite (409) and the file re-enters
the catalog on the next scan. Restore and purge take the `sync` lock, so they wait for
imports. Purge works only from database records, re-checks that each one resolves inside
the trash root, unlinks without following symlinks, and leaves unrecorded files alone.

Temp files (`.part`, sidecars) and a 0-byte leftover at an import's destination hold
nothing worth keeping and are removed directly. The trash needs
`torrent.library_base_dir`; the SMB-only target has no trash, and no upgrades.

Local data is a separate check: the import reads sampled chunks back from the NAS (A2)
before the torrent and its local copy can be removed.

## Quality upgrades

Movies only, off by default (`upgrades.enabled`). Needs the torrent backend,
`library_base_dir` and the trash. Run by the `upgrades` job
(`schedule.upgrades_interval_minutes`) or `home-theater upgrades`, which only propose.

A proposal is a normal candidate with `source=upgrade` and a reason, and appears at the
top of the Candidates page. With `upgrades.auto: false` nothing downloads until someone
approves it. `auto: true` approves it, still subject to `features.dry_run` and the
download window. Rejecting one isn't a dislike: upgrades are never training labels.

A release qualifies when it's provably better on source tier (CAM/TS < TV/DVD < WEB <
BluRay < Remux) or resolution, and no worse on the other. Same tier and resolution
never qualifies. It needs `upgrades.min_seeders`, a year in its name, and no dub,
hardcoded-subtitle, upscale or 3D tag. A title is searched once per
`upgrades.cooldown_days`, at most `upgrades.max_titles_per_run` per run, and only with a
single owned file of known quality. The catalog has no source column, so the owned
tier comes from the release it was imported from, then the file and folder names. With
neither, only a resolution jump can qualify.

The grab takes exactly the proposed release, never a fresh search. The new file lands as
`Movies/<Title (Year)>/<Title (Year)> [1080p BluRay].<ext>`, so it can't collide with
the old one, and is read back there. Only then does the old file go to the trash, and
only if it's still the size that was approved. Any failure leaves the original in
place: a failed copy retries like any import, and a failed swap keeps both files and
parks for **Retry import**. Subtitles for the old filename stay in `Subs/`; the new
file gets its own from the usual fetch.

## Limitations

- Import is a straight copy with the target name; it does not push naming
  templates (that's Bazarr/the arr stack).
