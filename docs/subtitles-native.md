# Native subtitle backend

An alternative to Bazarr: the app searches subtitle providers directly and writes
the `.srt` next to each owned media file — no Bazarr, Radarr, or Sonarr. Enable it
with `subtitles.backend: native` in `config.yaml`.

Like the torrent backend, this departs from the plan's "front everything through
the mature stack" doctrine; use it for a self-contained setup. Bazarr remains the
default. You are responsible for provider accounts and their terms of use.

## How it works

1. `subtitles` (CLI) / the scheduled sweep walks **our own catalog**: every owned
   file that lacks a target language (`subtitles.languages`) is a work item.
   Never-tried files go first, newest first, then the oldest retries. A valid
   subtitle already at the destination is adopted with no provider call, and a file
   that isn't on the NAS is skipped.
2. For each item it computes the file's OpenSubtitles `moviehash` and searches the
   enabled providers **in order**. The first provider that yields a valid subtitle
   wins, so ordering encodes preference (e.g. `ktuvit` before `opensubtitles` for
   Hebrew).
3. Results are ranked: a `moviehash` match first, then release overlap with the file
   (group, source, resolution, codec) plus the `hearing_impaired` preference, then
   provider popularity. Up to 3 per source are downloaded in that order until one
   passes `validate_subtitle` (real SRT/ASS cues, not an HTML page; gzip and zip
   unpacked under size caps, the right episode picked from a pack; decoded and
   stored as UTF-8). Subtitles you rejected are never chosen again.
4. It's written to `<media folder>/Subs/<media stem>.<lang>.srt` (or `.ass`/`.ssa`)
   through a uniquely named hidden temp file and an atomic replace, so a stale temp
   on the NAS can't block a later write. A `Subtitle` row is recorded and the file's
   `subtitle_langs` is updated, so the next sweep skips it.

A (file, language) that ends with nothing enters a cooldown in `subtitle_attempt`: 1
day, then 2, 4... up to 30, so an unmatchable title costs nothing until it expires.

`max_searches_per_sweep` caps the items that reach a provider in one run, including
those that find nothing. Adopted, absent and cooling-down items don't count.

A source is disabled for the rest of the sweep after 3 failures in a row, or at once
on an auth or quota error. The sweep stops if the library isn't mounted, and waits up
to 10 minutes between items while an import is copying. The run ends `success`,
`partial` (some work failed) or `failed`.

## Providers

| Source | Languages | Notes |
|---|---|---|
| `opensubtitles` | he, en, many | opensubtitles**.com** REST API. Search needs only the API key; **download needs username+password** and is **capped ≈ 20/day** (free tier). Query params are sent alphabetically sorted (the API 301s otherwise). |
| `opensubtitles_org` | he, en, many | opensubtitles**.org** legacy **XML-RPC** (separate account/password). **No daily download cap** — useful for bulk English. Deprecated + rate-limited, and needs a **registered `User-Agent`** (`subtitles.opensubtitles_org_user_agent`; the temporary one is throttled). Download links are gzip. |
| `ktuvit` | he | Hebrew specialist (ktuvit.me account). Movies **and** series per-episode. Login is a bespoke scheme — the client scrapes the site's rotating `encryptionSalt`, runs PBKDF2-HMAC-SHA1 → AES-CBC(password, iv-from-email) → SHA256 → base64. Login verified live. A failure raises rather than looking like "no subtitles": the sweep moves on to the next source and disables ktuvit after repeated failures. A title matches by imdb id, or by exact normalised name plus year; anything less returns nothing. The search field names are undocumented, so check the Hebrew hit rate after a real sweep. |

## Setup

1. Credentials in `.env`:
   ```
   OPENSUBTITLES_API_KEY=...
   OPENSUBTITLES_USERNAME=...
   OPENSUBTITLES_PASSWORD=...
   KTUVIT_EMAIL=...          # optional
   KTUVIT_PASSWORD=...
   ```
2. In `config.yaml`:
   ```yaml
   subtitles:
     backend: native
     # first hit wins -> put uncapped sources first
     sources: [ktuvit, opensubtitles_org, opensubtitles]
     library_base_dir: /Volumes/Elements_25A1-1   # write beside media via the mount
   ```
   Ordering is preference: `ktuvit` (Hebrew, uncapped) → `opensubtitles_org`
   (English, uncapped XML-RPC) → `opensubtitles` (.com REST, exact imdb match but
   20 downloads/day). For heavy `.org` use, register a User-Agent at
   opensubtitles.org and set `opensubtitles_org_user_agent`.
3. Run `home-theater subtitles`, or the token-gated `POST /api/subtitles/search`,
   which answers 202 once the sweep has started in the background (outcome on the
   Runs page), 409 if one is already running, and 503 if it isn't configured.
   Coverage on the `/subtitles` page reflects the newly-written subs.

## Writing subs to the NAS

Owned-file paths are SMB UNC from the scanner. Set `subtitles.library_base_dir` to
the **locally-mounted** share (`/Volumes/Elements_25A1-1`) so subs are written
through the OS mount — the same reliable path the torrent importer uses (the
direct `smbprotocol` write is unreliable on the WD MyCloud). Without it, files
with UNC paths are skipped with a clear "set subtitles.library_base_dir" error.

## Timing check

Off by default (`subtitles.sync`). Needs this backend and the optional `ffsubsync` and
`ffmpeg` commands on `PATH`; a launchd `PATH` has neither unless you add their folders.
Missing tools are a skip noted on Status, not a failure. Run by the `subtitle_sync` job
(`interval_minutes`) or `home-theater subtitle-sync`.

Every recorded subtitle is due, newest first, unless it's known to be a `moviehash`
match (that's only noted while the switch is on, so older ones get checked too). Up to
`max_per_run` run at a time (each reads a whole film over the NAS), each limited to
`timeout_minutes`, and a run stops if an import starts. ffsubsync works on a temporary
copy; its output must pass `validate_subtitle` and keep every cue, and the shift is
measured from the two files. Below `min_offset_seconds` it's already in time; past
`max_offset_seconds` the subtitle is left alone and retried after a cooldown. Otherwise
the original stays beside the subtitle as `<name>.orig` and the corrected file takes
its place.

The Subtitles page reviews each file: approve, revert to the original, or swap in
another provider result. A swapped-out subtitle is remembered per file and never chosen
again.

## Limitations

- No upgrade: once a language is present it's considered covered.
- Without `subtitles.sync`, timing rests on release and hash matching alone.
- ktuvit's name-and-year match returns nothing when several hits qualify or a year is
  missing, so a generic title falls through to the next source.
