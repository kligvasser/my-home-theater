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
2. For each item it computes the file's OpenSubtitles `moviehash` (best-match
   signal) and searches the enabled providers **in order** — the first provider
   with a hit wins, so ordering encodes preference (e.g. `ktuvit` before
   `opensubtitles` for Hebrew).
3. The top result is downloaded and written to
   `<media folder>/Subs/<media stem>.<lang>.srt` (atomic `.part` rename). A
   `Subtitle` row is recorded and the file's `subtitle_langs` is updated, so the
   next sweep skips it.

Capped at `max_searches_per_sweep` downloads per run to respect provider quotas.

## Providers

| Source | Languages | Notes |
|---|---|---|
| `opensubtitles` | he, en, many | REST API (opensubtitles**.com** login — the account is shared with .org). Search needs only the API key; **download needs username+password** (free tier ≈ a few/day). Reliable anchor. Query params are sent alphabetically sorted (the API 301s otherwise). |
| `ktuvit` | he | Hebrew specialist (ktuvit.me account). **Movies only** for now (series return nothing). Login is a bespoke scheme — the client scrapes the site's rotating `encryptionSalt`, runs PBKDF2-HMAC-SHA1 → AES-CBC(password, iv-from-email) → SHA256 → base64. Verified working; if ktuvit changes that scheme it degrades to empty results and OpenSubtitles covers Hebrew. |

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
     sources: [ktuvit, opensubtitles]   # Hebrew from ktuvit first, else OpenSubtitles
     library_base_dir: /Volumes/Elements_25A1-1   # write beside media via the mount
   ```
3. Run `home-theater subtitles` (or the token-gated `POST /api/subtitles/search`).
   Coverage on the `/subtitles` page reflects the newly-written subs.

## Writing subs to the NAS

Owned-file paths are SMB UNC from the scanner. Set `subtitles.library_base_dir` to
the **locally-mounted** share (`/Volumes/Elements_25A1-1`) so subs are written
through the OS mount — the same reliable path the torrent importer uses (the
direct `smbprotocol` write is unreliable on the WD MyCloud). Without it, files
with UNC paths are skipped with a clear "set subtitles.library_base_dir" error.

## Limitations

- **ktuvit is movies-only** (per-episode series selection isn't implemented);
  OpenSubtitles handles episodes via the series imdb id + season/episode.
- No re-sync/upgrade: once a language is present it's considered covered.
- No subtitle-to-audio sync verification beyond release/hash matching.
