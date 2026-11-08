"""Import a completed torrent's media into the NAS library layout.

This is the app's first *write* path to the NAS. It copies the finished file
into ``Movies/<Title (Year)>/<Title (Year)>.<ext>`` (the layout the arr stack
would otherwise produce) and reads it back before it counts as done.

Two targets implement the same :class:`LibraryTarget` seam:

* :class:`SMBLibraryTarget` — writes to the NAS share over SMB (the default).
* :class:`LocalLibraryTarget` — writes to a local/mounted directory; also what
  the tests use, so import logic is exercised without a live NAS.

Copies land at a ``.part`` file and are atomically renamed into place, so a
half-written file is never mistaken for a real one (mirrors the scanner's
verify-after-move rule, plan §12).

Three rules protect data, because a successful import authorises deleting the
local copy:

* A ``.part`` is only resumed when its ``.part.meta`` sidecar proves it came from
  the same source; a size match alone can splice two releases together.
* A copy counts only after a read-back of sampled chunks matches the source and
  the mount is still there. A file already in place has to pass the same check.
* An existing library file is never overwritten; see :func:`_may_replace`. A quality
  upgrade lands under its own name beside the old file, is verified there, and only
  then does the old one go to the trash (:func:`retire_replaced_file`).

Series go to ``TV Shows/<Series>/Season NN/<release filename>`` — the layout the
scanner already reads. Episode files keep their release names (that's what the
library's existing episodes look like, and the scanner parses S/E from them);
an existing series folder is reused even when its name is styled differently
("Colin.From.Accounts" vs "Colin From Accounts"), so one show never splits
across two folders.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from ...config import AppConfig
from ...errors import NotConfiguredError, redact_exc
from ...logging_setup import get_logger
from ...scanner.parse import is_media_file

log = get_logger(__name__)

# Called during a copy with (bytes_copied, total_bytes) so the dashboard can show
# NAS-import progress. May be None.
ProgressCb = Callable[[int, int], None] | None

# Set by whoever gave up waiting for a copy; the copy stops at its next chunk.
CancelEvent = threading.Event | None

# Characters illegal in SMB/Windows and most NAS filesystems.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# A "sample" clip is only junk when it's also tiny; a real 4 GB file that merely
# has "sample" in its name is kept.
_SAMPLE_MAX_BYTES = 300 * 1024 * 1024
_COPY_CHUNK = 4 * 1024 * 1024  # 4 MiB — throughput + progress granularity
# Read-back check: head, tail and this many evenly spaced middle chunks, so a
# multi-GB file costs a few MiB of reads. A file too small to sample is compared whole.
_VERIFY_CHUNK = 1024 * 1024
_VERIFY_MIDDLE_CHUNKS = 3
# macOS only: ask for reads from the NAS, not the pages this process just wrote.
_F_NOCACHE: int | None = getattr(fcntl, "F_NOCACHE", None)


class ImportError_(RuntimeError):
    """The completed torrent could not be imported (no media file, copy failed)."""


class NasUnavailableError(ImportError_):
    """The NAS can't take writes right now: the SMB session faulted (EIO, connection
    reset, stale handle) or the share is full or read-only. The transfer isn't
    wrong, so the whole sweep should back off and retry later rather than hammer
    the share file by file."""


class ImportStoppedError(ImportError_):
    """The copy was told to stop. Its ``.part`` stays, to resume on a later sweep."""


class LibraryConflictError(ImportError_):
    """A different file already sits at the destination. It is never overwritten:
    the import is refused, the local copy stays, and a person decides."""


# errno values that mean "the SMB link faulted", not "this file is bad".
_TRANSPORT_ERRNOS = frozenset(
    {
        errno.EIO,
        errno.ENOTCONN,
        errno.ESTALE,
        errno.ETIMEDOUT,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.ENODEV,
    }
)


# errno values that mean every later import would fail the same way.
_CAPACITY_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EROFS})


def _classify_oserror(exc: OSError, dest: str) -> ImportError_:
    """Wrap a copy/rename OSError: faults that would hit every file become
    NasUnavailableError so the sweep stops; anything else is a per-file error."""

    if exc.errno in _CAPACITY_ERRNOS:
        return NasUnavailableError(
            f"NAS can't accept {os.path.basename(dest)!r} ({exc.strerror}); "
            "backing off until the share has space and is writable."
        )
    if exc.errno in _TRANSPORT_ERRNOS:
        return NasUnavailableError(
            f"NAS link faulted copying {os.path.basename(dest)!r} "
            f"({exc.strerror}); backing off to retry next sweep."
        )
    return ImportError_(f"copy to {dest!r} failed: {exc.strerror}")


def _report(on_progress: ProgressCb, done: int, total: int) -> None:
    """Progress is cosmetic: a failing callback (a locked DB) must not abort a copy."""

    if on_progress is None:
        return
    try:
        on_progress(done, total)
    except Exception as exc:
        log.warning("import.progress_failed", detail=redact_exc(exc))


def _check_cancelled(cancel: CancelEvent) -> None:
    if cancel is not None and cancel.is_set():
        raise ImportStoppedError("import stopped before it finished")


def _copy_stream(
    fsrc: Any,
    fdst: Any,
    total: int,
    on_progress: ProgressCb,
    *,
    base: int = 0,
    cancel: CancelEvent = None,
) -> int:
    """Copy fsrc -> fdst in chunks, reporting progress. Returns bytes written.

    ``base`` is bytes already present from a resumed copy, so progress is
    reported against the whole file, not just this run's remainder. ``cancel`` is
    checked before each chunk, so a stop request lands within one chunk.
    """

    written = base
    while True:
        _check_cancelled(cancel)
        chunk = fsrc.read(_COPY_CHUNK)
        if not chunk:
            break
        fdst.write(chunk)
        written += len(chunk)
        _report(on_progress, written, total)
    return written


def _sample_ranges(size: int) -> list[tuple[int, int]]:
    """(offset, length) pairs to compare: head, middles and tail, or the whole file."""

    chunks = _VERIFY_MIDDLE_CHUNKS + 2
    if size <= _VERIFY_CHUNK * chunks:
        return [(0, size)]
    step = (size - _VERIFY_CHUNK) / (chunks - 1)
    return [(int(step * i), _VERIFY_CHUNK) for i in range(chunks)]


def _same_ranges(fsrc: Any, fcopy: Any, ranges: list[tuple[int, int]]) -> bool:
    for offset, length in ranges:
        fsrc.seek(offset)
        fcopy.seek(offset)
        left = length
        while left > 0:
            want = min(left, _COPY_CHUNK)
            data = fsrc.read(want)
            if not data or data != fcopy.read(want):
                return False
            left -= len(data)
    return True


def _bytes_match(src: str, copy: str, ranges: list[tuple[int, int]]) -> bool:
    with open(src, "rb") as fsrc, open(copy, "rb") as fcopy:
        if _F_NOCACHE is not None:
            with contextlib.suppress(OSError):  # best-effort: not every filesystem honours it
                fcntl.fcntl(fcopy.fileno(), _F_NOCACHE, 1)
        return _same_ranges(fsrc, fcopy, ranges)


def _copy_matches(src: str, copy: str, size: int) -> bool:
    """Whether ``copy`` has ``size`` bytes and its sampled chunks equal ``src``."""

    return os.path.getsize(copy) == size and _bytes_match(src, copy, _sample_ranges(size))


def _part_matches(src: str, part: str, ranges: list[tuple[int, int]]) -> bool:
    """:func:`_bytes_match` for a leftover ``.part``. One the NAS refuses to re-open
    is simply not reusable, but a link fault still has to back the sweep off."""

    try:
        return _bytes_match(src, part, ranges)
    except OSError as exc:
        classified = _classify_oserror(exc, part)
        if isinstance(classified, NasUnavailableError):
            raise classified from exc
        return False


def _flush_to_disk(fdst: Any) -> None:
    fdst.flush()
    try:
        os.fsync(fdst.fileno())
    except OSError as exc:
        # Some network filesystems don't implement fsync. That must not block every
        # import, and the read-back still runs.
        if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
            raise
        log.warning("import.fsync_unsupported", errno=exc.errno)


def _sidecar_path(tmp: str) -> str:
    return tmp + ".meta"


def _source_stamp(src_stat: os.stat_result, infohash: str | None) -> dict[str, Any]:
    """What a ``.part`` must have been copied from for it to be resumed."""

    return {
        "size": src_stat.st_size,
        "mtime_ns": src_stat.st_mtime_ns,
        "infohash": infohash.lower() if infohash else None,
    }


def _read_sidecar(tmp: str) -> Any:
    """The stamp beside ``tmp``, or None if it's missing or unreadable."""

    try:
        with open(_sidecar_path(tmp), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _write_sidecar(tmp: str, stamp: dict[str, Any]) -> None:
    with open(_sidecar_path(tmp), "w", encoding="utf-8") as fh:
        json.dump(stamp, fh)
        _flush_to_disk(fh)


def _drop_sidecar(tmp: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(_sidecar_path(tmp))


@dataclass(frozen=True, slots=True)
class SmbMount:
    """Enough to re-mount a dropped ``/Volumes/<share>`` SMB mount (WD MyCloud and
    other guest shares drop the mount during sustained multi-GB copies)."""

    host: str
    share: str
    username: str | None
    password: str | None

    @property
    def url(self) -> str:
        user = quote(self.username or "guest", safe="")
        auth = f"{user}:{quote(self.password, safe='')}" if self.password else user
        return f"smb://{auth}@{self.host}/{self.share}"


def ensure_mounted(base_dir: str, mount: SmbMount | None) -> None:
    """Guard a ``/Volumes/<share>`` library path before writing to it.

    A dropped SMB mount leaves the path either gone or a bare root-owned stub, so
    a copy fails with a cryptic ``Permission denied: '/Volumes/…'``. For such paths
    we verify the mount and, given credentials, try one ``mount volume`` re-mount so
    a dropped share self-heals instead of failing the whole sync run. Plain local
    dirs (not under ``/Volumes``) are left alone — ``makedirs`` handles those.
    """

    if not base_dir.startswith("/Volumes/"):
        return
    if os.path.ismount(base_dir):
        return
    if mount is None:
        raise NasUnavailableError(
            f"NAS share not mounted at {base_dir!r} — remount it "
            "(Finder → Go → Connect to Server) and retry."
        )
    log.warning("import.mount_dropped", base_dir=base_dir)
    try:
        # The script goes in on stdin, not argv: mount.url embeds the password and
        # every process's arguments are visible to `ps`. For the same reason, never
        # surface the raw command or exception.
        proc = subprocess.run(
            ["osascript", "-"],
            input=f'mount volume "{mount.url}"'.encode(),
            capture_output=True,
            timeout=30,
        )
    except Exception:
        raise NasUnavailableError(
            f"NAS share not mounted at {base_dir!r}; auto-remount timed out. "
            "Remount it manually and retry."
        ) from None
    if proc.returncode != 0 or not os.path.ismount(base_dir):
        raise NasUnavailableError(
            f"NAS share not mounted at {base_dir!r}; auto-remount failed. "
            "Remount it manually and retry."
        )
    log.info("import.remounted", base_dir=base_dir)


def _mount_gone(base_dir: str | None) -> bool:
    if not base_dir or not base_dir.startswith("/Volumes/"):
        return False
    return not os.path.ismount(base_dir)


def mount_lost(config: AppConfig) -> bool:
    """True when the configured ``/Volumes/<share>`` library mount is gone — the
    one failure mode where continuing to import would only cascade."""

    return _mount_gone(config.torrent.library_base_dir)


class LibraryTarget(Protocol):
    def import_file(
        self,
        local_src: str,
        rel_dir: str,
        filename: str,
        on_progress: ProgressCb = None,
        *,
        infohash: str | None = None,
        cancel: CancelEvent = None,
    ) -> str:
        """Copy ``local_src`` into ``rel_dir/filename`` under the library root,
        creating directories and returning the final path. Returning at all
        means the copy was read back and matched, which is what authorises
        deleting the local data; raises :class:`LibraryConflictError` rather
        than replace a different file already there. ``infohash`` ties a resumed
        copy to its torrent. Calls ``on_progress(copied, total)`` during the copy
        when provided, and stops with :class:`ImportStoppedError` once ``cancel`` is
        set."""
        ...

    def ready(self) -> None:
        """Raise if the library can't be reached right now. Folder listings don't:
        they read as empty, which would look like "no such series folder yet"."""
        ...

    def list_dir(self, rel_dir: str) -> list[str]:
        """Entry names under ``rel_dir`` (empty when it doesn't exist) — used to
        reuse an existing series folder whose name is styled differently."""
        ...


def _existing_size(path: str) -> int | None:
    """Size of ``path``, or None only when it truly doesn't exist: any other stat
    failure propagates, so a flaky share can't read as "nothing to protect here"."""

    try:
        return os.path.getsize(path)
    except FileNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class ReplacementGrant:
    """An approved upgrade's permission to supersede one exact library file: the
    file at ``path``, as long as it's still the ``size`` the owner saw. ``tag`` names
    the new file so it can sit beside the old one."""

    path: str
    size: int
    tag: str


def _may_replace(existing_size: int, grant: ReplacementGrant | None = None) -> bool:
    """Whether an existing library file may give way to an import.

    An empty leftover (a raced 0-byte file) holds nothing worth keeping. Anything
    else takes a grant for exactly this file, which only :func:`retire_replaced_file`
    passes: an overwrite can't keep the old file until the new one is proven.
    """

    return existing_size == 0 or (grant is not None and existing_size == grant.size)


def _guard_existing(dest: str, existing_size: int | None, new_size: int) -> None:
    if existing_size is None or _may_replace(existing_size):
        return
    raise LibraryConflictError(
        f"refusing to replace {os.path.basename(dest)!r}: the library file is "
        f"{existing_size} bytes, this release is {new_size}. Both copies are kept; "
        "resolve it by hand."
    )


def _conflict_same_size(dest: str) -> LibraryConflictError:
    return LibraryConflictError(
        f"{os.path.basename(dest)!r} is already in the library at the same size but "
        "its contents differ from this release. Both copies are kept; resolve it by hand."
    )


def _finalize_local(tmp: str, dest: str) -> None:
    """Atomically move a verified ``tmp`` onto ``dest``.

    ``os.replace`` overwrites atomically on POSIX, but some NAS firmware (WD
    MyCloud) returns EBUSY/EINVAL when ``dest`` is an existing file it still
    holds a stale SMB handle on. Fall back to remove-then-rename; if the NAS
    still won't release it, raise a clear, retryable error (the handle clears
    on its own, and the next sweep resumes from the intact ``tmp``).
    """

    try:
        os.replace(tmp, dest)
        return
    except OSError:
        pass
    try:
        existing = _existing_size(dest)
        if existing is not None:
            _guard_existing(dest, existing, os.path.getsize(tmp))  # may have appeared since
            os.remove(dest)
        os.rename(tmp, dest)
    except OSError as exc:
        classified = _classify_oserror(exc, dest)
        if isinstance(classified, NasUnavailableError):
            raise classified from None
        raise ImportError_(
            f"NAS is holding {dest!r} busy ({exc.strerror}); leaving {os.path.basename(tmp)} "
            "in place to finish on a later sweep — if it persists, remount the share."
        ) from None


def _sanitize(name: str) -> str:
    cleaned = _ILLEGAL.sub("", name).strip().rstrip(". ")
    return cleaned or "Untitled"


def _walk_videos(content_path: str) -> list[tuple[str, int]]:
    """All non-sample media files under a completed torrent, with sizes.

    Raises :class:`ImportError_` if the folder can't be read — on macOS,
    ``~/Downloads``/``~/Desktop``/``~/Documents`` are privacy-protected (TCC) and
    a terminal/launchd process is denied listing them, which would otherwise look
    like an empty folder ("no media file found"). Also raises if only part of the
    folder could be read: the callers decide what is safe to delete from this list.
    """

    if os.path.isfile(content_path):
        if not is_media_file(os.path.basename(content_path)):
            return []
        try:
            return [(content_path, os.path.getsize(content_path))]
        except OSError:
            return []
    walk_errors: list[OSError] = []
    found: list[tuple[str, int]] = []
    for dirpath, _dirnames, filenames in os.walk(content_path, onerror=walk_errors.append):
        for name in filenames:
            if not is_media_file(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                walk_errors.append(exc)
                continue
            if "sample" in name.lower() and size < _SAMPLE_MAX_BYTES:
                continue
            found.append((full, size))
    if not found and any(isinstance(e, PermissionError) for e in walk_errors):
        raise ImportError_(
            f"permission denied reading {content_path!r} — on macOS, grant the app "
            "Full Disk Access, or set torrent.movie_download_dir to a folder outside "
            "~/Downloads, ~/Desktop and ~/Documents (which are privacy-protected)."
        )
    if found and walk_errors:
        raise ImportError_(f"could not read everything under {content_path!r}")
    return found


def find_primary_video(content_path: str) -> str | None:
    """The main video file for a completed torrent: the file itself if the torrent
    is a single file, else the largest non-sample media file in its folder."""

    videos = _walk_videos(content_path)
    if not videos:
        return None
    return max(videos, key=lambda v: v[1])[0]


def find_unimported_videos(
    content_path: str, imported: Collection[str] | None = None
) -> list[tuple[str, int]]:
    """Videos in a completed torrent that an import left behind and that look like
    real content, as (path, size). While any remain, the local data must stay.

    ``imported`` holds the source paths that were copied, defaulting to the primary
    video (what a movie import copies). A video counts as real content if it's over
    the sample cap, or at least half the size of the smallest file imported, so a
    low-bitrate episode with an unparsable name isn't mistaken for a featurette.
    """

    videos = _walk_videos(content_path)
    if imported is None:
        imported = [max(videos, key=lambda v: v[1])[0]] if videos else []
    done = set(imported)
    sizes = [size for path, size in videos if path in done]
    if not sizes:
        return []
    floor = min(_SAMPLE_MAX_BYTES, min(sizes) // 2)
    return [(path, size) for path, size in videos if path not in done and size >= floor]


def _movie_dir_and_file(
    title: str, year: int | None, ext: str, tag: str | None = None
) -> tuple[str, str]:
    base = _sanitize(title) + (f" ({year})" if year else "")
    return base, f"{base} [{_sanitize(tag)}]{ext}" if tag else f"{base}{ext}"


def import_completed_movie(
    config: AppConfig,
    target: LibraryTarget,
    *,
    content_path: str,
    title: str,
    year: int | None,
    on_progress: ProgressCb = None,
    infohash: str | None = None,
    cancel: CancelEvent = None,
    replaces: ReplacementGrant | None = None,
) -> str:
    """Copy a finished movie into the library and return its destination path.

    With ``replaces`` the file is named after its quality, so it never collides with
    the movie it's replacing: the same deterministic name on every retry, and if
    that's somehow taken the usual conflict refusal applies.
    """

    video = find_primary_video(content_path)
    if video is None:
        raise ImportError_(f"no media file found under {content_path!r}")
    ext = os.path.splitext(video)[1].lower()
    folder, filename = _movie_dir_and_file(title, year, ext, replaces.tag if replaces else None)
    rel_dir = f"{config.nas.movies_root.rstrip('/')}/{folder}"
    dest = target.import_file(
        video, rel_dir, filename, on_progress, infohash=infohash, cancel=cancel
    )
    log.info("import.done", title=title, source=video, dest=dest)
    return dest


def retire_replaced_file(
    config: AppConfig,
    grant: ReplacementGrant,
    new_dest: str,
    *,
    reason: str,
    cancel: CancelEvent = None,
) -> int | None:
    """Send the file an upgrade replaced to the trash; returns its trash record's id,
    or None if it's already gone (a retry after an earlier attempt got this far).

    Call only once the import has returned, which means ``new_dest`` was read back
    at its final path. Any failure leaves both files in the library.
    """

    from ...trash import TrashUnavailableError, move_to_trash

    _check_cancelled(cancel)
    old = os.path.basename(grant.path)
    if not config.trash.enabled:
        raise LibraryConflictError(f"trash is off, so {old!r} stays beside its upgrade")
    ensure_mounted(config.torrent.library_base_dir or "", None)
    try:
        existing = _existing_size(grant.path)
        if existing is None:
            return None
        if os.path.samefile(grant.path, new_dest):
            raise LibraryConflictError(f"the upgrade landed on {old!r} itself; keeping it")
        if not _may_replace(existing, grant):
            raise LibraryConflictError(
                f"{old!r} is {existing} bytes, the approved upgrade was for {grant.size}. "
                "It changed since, so both copies are kept; resolve it by hand."
            )
        return move_to_trash(config, grant.path, reason=reason)
    except FileNotFoundError:
        return None
    except TrashUnavailableError as exc:
        raise NasUnavailableError(redact_exc(exc)) from None
    except OSError as exc:
        raise _classify_oserror(exc, grant.path) from exc


@dataclass(frozen=True, slots=True)
class EpisodeImport:
    """One episode file placed into the library."""

    dest: str
    filename: str  # the release-style basename (kept: the scanner parses it)
    season: int | None
    episode: int | None
    episode_end: int | None  # multi-episode files (S03E01E02)
    source: str  # the local file this was copied from


def _norm_folder(name: str) -> str:
    """Case/punctuation-insensitive key: 'Colin.From.Accounts' == 'Colin From Accounts'."""

    return re.sub(r"[^a-z0-9]", "", name.lower())


def _series_folder(target: LibraryTarget, tv_root: str, title: str) -> str:
    wanted = _sanitize(title)
    key = _norm_folder(wanted)
    try:
        for existing in target.list_dir(tv_root.rstrip("/")):
            if _norm_folder(existing) == key:
                return existing
    except Exception:  # listing is an optimisation; never fail the import over it
        log.warning("import.list_series_failed", tv_root=tv_root)
    return wanted


def import_completed_episodes(
    config: AppConfig,
    target: LibraryTarget,
    *,
    content_path: str,
    series_title: str,
    season: int | None,
    on_progress: ProgressCb = None,
    infohash: str | None = None,
    cancel: CancelEvent = None,
) -> list[EpisodeImport]:
    """Copy a finished episode/season-pack torrent into the library.

    Each media file lands in ``TV Shows/<Series>/Season NN/<its own name>`` —
    NN parsed from the filename, falling back to the candidate's ``season``.
    Progress is reported across the whole batch. Returns one record per file.
    """

    from .select import parse_season_episode

    all_videos = sorted(_walk_videos(content_path))
    if not all_videos:
        raise ImportError_(f"no media file found under {content_path!r}")

    # Keep only real episodes: a season pack ships featurettes/extras ("The Cast
    # Ask Each Other Anything", "<Actor> on <Show>") that guessit parses with no
    # episode number. Importing those spawns junk, poster-less catalog titles, so
    # they're skipped here; find_unimported_videos decides whether one is big
    # enough that the local data has to be held instead of deleted.
    episodes_only: list[tuple[str, int, int, int | None]] = []  # path, size, episode, end
    for video, size in all_videos:
        _seasons, eps = parse_season_episode(os.path.basename(video))
        if not eps:
            log.info("import.extra_skipped", series=series_title, file=os.path.basename(video))
            continue
        episodes_only.append((video, size, eps[0], eps[-1] if len(eps) > 1 else None))
    if not episodes_only:
        raise ImportError_(f"no season/episode-numbered file under {content_path!r} (extras only?)")

    target.ready()  # a dropped mount lists empty, and the show would split in two
    tv_root = config.nas.tv_root.rstrip("/")
    folder = _series_folder(target, tv_root, series_title)

    total = sum(size for _p, size, _e, _end in episodes_only)
    copied_before = 0
    out: list[EpisodeImport] = []
    for video, size, file_episode, episode_end in episodes_only:
        name = os.path.basename(video)
        seasons, _episodes = parse_season_episode(name)
        file_season = seasons[0] if len(seasons) == 1 else season
        season_dir = f"Season {file_season:02d}" if file_season is not None else "Season 00"
        rel_dir = f"{tv_root}/{folder}/{season_dir}"

        def batch_progress(done: int, _file_total: int, *, offset: int = copied_before) -> None:
            if on_progress is not None:
                on_progress(offset + done, total)

        _check_cancelled(cancel)
        dest = target.import_file(
            video, rel_dir, _sanitize(name), batch_progress, infohash=infohash, cancel=cancel
        )
        copied_before += size
        out.append(EpisodeImport(dest, name, file_season, file_episode, episode_end, video))
        log.info("import.episode_done", series=series_title, source=video, dest=dest)
    return out


def build_library_target(config: AppConfig) -> LibraryTarget:
    """SMB target by default; a local target when ``torrent.library_base_dir`` set."""

    base = config.torrent.library_base_dir
    if base:
        # For a /Volumes/<share> mount, carry the SMB creds so a dropped mount can
        # self-heal mid-sync (guest shares drop during big copies).
        mount: SmbMount | None = None
        if base.startswith("/Volumes/") and config.secrets.smb_host:
            mount = SmbMount(
                host=config.secrets.smb_host,
                share=os.path.basename(base.rstrip("/")),
                username=config.secrets.smb_user,
                password=(
                    config.secrets.smb_pass.get_secret_value() if config.secrets.smb_pass else None
                ),
            )
        return LocalLibraryTarget(base, mount)
    secrets = config.secrets
    if not secrets.smb_host or not config.nas.share:
        raise NotConfiguredError(
            "Library import needs a NAS target: set SMB_HOST + nas.share, or "
            "torrent.library_base_dir to a local path."
        )
    return SMBLibraryTarget(
        host=secrets.smb_host,
        share=config.nas.share,
        username=secrets.smb_user,
        password=secrets.smb_pass.get_secret_value() if secrets.smb_pass else None,
    )


class LocalLibraryTarget:
    """Copy into a local (or locally-mounted) directory."""

    def __init__(self, base_dir: str, mount: SmbMount | None = None) -> None:
        self.base_dir = base_dir
        self.mount = mount

    def ready(self) -> None:
        ensure_mounted(self.base_dir, self.mount)  # self-heal a dropped NAS mount

    def list_dir(self, rel_dir: str) -> list[str]:
        path = os.path.join(self.base_dir, *rel_dir.split("/"))
        try:
            return os.listdir(path)
        except OSError:
            return []

    def import_file(
        self,
        local_src: str,
        rel_dir: str,
        filename: str,
        on_progress: ProgressCb = None,
        *,
        infohash: str | None = None,
        cancel: CancelEvent = None,
    ) -> str:
        self.ready()
        dest_dir = os.path.join(self.base_dir, *rel_dir.split("/"))
        dest = os.path.join(dest_dir, filename)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            return self._place(local_src, dest, on_progress, infohash, cancel)
        except OSError as exc:
            # Leave any partial .part for the resumable retry; a transport fault
            # backs the whole sweep off rather than thrashing the faulting NAS.
            raise _classify_oserror(exc, dest) from exc

    def _place(
        self,
        local_src: str,
        dest: str,
        on_progress: ProgressCb,
        infohash: str | None,
        cancel: CancelEvent,
    ) -> str:
        src_stat = os.stat(local_src)
        src_size = src_stat.st_size

        # Idempotent: a file already in place is done (a season pack retried after
        # one bad episode must not re-copy the good ones), but the same size proves
        # nothing, so it has to match before it can authorise deleting the source.
        existing = _existing_size(dest)
        if existing == src_size:
            self._require_mounted(dest)
            if not _copy_matches(local_src, dest, src_size):
                raise _conflict_same_size(dest)
            _drop_sidecar(dest + ".part")
            _report(on_progress, src_size, src_size)
            return dest
        _guard_existing(dest, existing, src_size)

        tmp = dest + ".part"
        stamp = _source_stamp(src_stat, infohash)
        have = _existing_size(tmp)
        same_source = have is not None and _read_sidecar(tmp) == stamp

        # A previous run finished the copy but died before the rename.
        if (
            same_source
            and have == src_size
            and _part_matches(local_src, tmp, _sample_ranges(src_size))
        ):
            self._publish(local_src, tmp, dest, src_size)
            _report(on_progress, src_size, src_size)
            return dest

        # Resume a partial .part left by a faulted copy (WD MyCloud drops the SMB
        # link mid-transfer): append the remainder instead of re-copying multi-GB
        # files from zero, so a big file completes across sweeps. Only with proof
        # it's the same source, and only if its last bytes survived the fault:
        # sampling alone could miss a torn seam. Anything else starts over; a
        # leftover that can't be cleared falls back to a fresh temp name.
        mode, base = "wb", 0
        tail = min(have or 0, _VERIFY_CHUNK)
        if (
            same_source
            and have is not None
            and 0 < have < src_size
            and _part_matches(local_src, tmp, [(have - tail, tail)])
        ):
            mode, base = "ab", have
            log.info("import.resume", dest=dest, at=have, size=src_size)
        elif have is not None:
            try:
                os.remove(tmp)
            except OSError:
                tmp = f"{dest}.{os.getpid()}.part"
                log.warning("import.stale_part", path=dest + ".part", using=tmp)

        with open(local_src, "rb") as fsrc, open(tmp, mode) as fdst:
            if mode == "wb":  # after the truncate, so a stale stamp never covers new bytes
                _write_sidecar(tmp, stamp)
            fsrc.seek(base)
            if base:
                _report(on_progress, base, src_size)
            _copy_stream(fsrc, fdst, src_size, on_progress, base=base, cancel=cancel)
            _flush_to_disk(fdst)

        self._require_mounted(dest)
        if not _copy_matches(local_src, tmp, src_size):
            with contextlib.suppress(OSError):
                os.remove(tmp)
            _drop_sidecar(tmp)
            raise ImportError_(
                f"read-back of {os.path.basename(dest)!r} didn't match the source; "
                "discarded the copy, the next sweep starts over."
            )
        self._publish(local_src, tmp, dest, src_size)
        return dest

    def _publish(self, local_src: str, tmp: str, dest: str, size: int) -> None:
        """Rename a verified ``tmp`` onto ``dest`` and confirm what landed there."""

        # The copy can take hours: look again right before the overwrite.
        _guard_existing(dest, _existing_size(dest), size)
        _finalize_local(tmp, dest)
        self._require_mounted(dest)
        if not _copy_matches(local_src, dest, size):
            with contextlib.suppress(OSError):  # our own file, or the retry could never pass
                os.remove(dest)
            raise ImportError_(
                f"{os.path.basename(dest)!r} didn't match the source after the rename; "
                "removed it, the next sweep starts over."
            )
        _drop_sidecar(tmp)

    def _require_mounted(self, dest: str) -> None:
        if _mount_gone(self.base_dir):
            raise NasUnavailableError(
                f"NAS mount dropped while importing {os.path.basename(dest)!r}; "
                "the copy can't be confirmed, retrying next sweep."
            )


class SMBLibraryTarget:
    """Copy into the NAS share over SMB (write path; the scanner's is read-only)."""

    def __init__(self, host: str, share: str, username: str | None, password: str | None) -> None:
        self.host = host
        self.share = share
        self._username = username
        self._password = password
        self._registered = False

    def _ensure_session(self) -> None:
        if self._registered:
            return
        import smbclient

        if self._username and self._password:
            smbclient.register_session(self.host, username=self._username, password=self._password)
        else:
            # Guest/password-less share — relax signing/secure-negotiate exactly as
            # the read-only scanner does (see SMBFileSystem). NAS-side permissions
            # still decide whether guest may write.
            smbclient.ClientConfig(require_secure_negotiate=False)
            smbclient.register_session(
                self.host,
                username=self._username or "guest",
                password=self._password or "",
                require_signing=False,
            )
        self._registered = True

    def _unc(self, rel: str) -> str:
        parts = [p for p in rel.replace("/", "\\").split("\\") if p]
        return "\\\\" + "\\".join([self.host, self.share, *parts])

    def ready(self) -> None:
        self._ensure_session()

    def list_dir(self, rel_dir: str) -> list[str]:
        import smbclient

        self._ensure_session()
        try:
            return list(smbclient.listdir(self._unc(rel_dir)))
        except OSError:
            return []

    def import_file(
        self,
        local_src: str,
        rel_dir: str,
        filename: str,
        on_progress: ProgressCb = None,
        *,
        infohash: str | None = None,  # unused: this target never resumes a .part
        cancel: CancelEvent = None,
    ) -> str:
        import smbclient

        self._ensure_session()
        remote_dir = self._unc(rel_dir)
        remote = remote_dir + "\\" + filename
        remote_tmp = remote + ".part"
        try:
            smbclient.makedirs(remote_dir, exist_ok=True)
            src_size = os.path.getsize(local_src)
            existing = self._remote_size(remote)
            if existing == src_size:
                if not self._matches(local_src, remote, src_size):
                    raise _conflict_same_size(remote)
                _report(on_progress, src_size, src_size)
                return remote
            _guard_existing(remote, existing, src_size)

            with open(local_src, "rb") as fsrc, smbclient.open_file(remote_tmp, mode="wb") as fdst:
                _copy_stream(fsrc, fdst, src_size, on_progress, cancel=cancel)
                fdst.flush()
            if not self._matches(local_src, remote_tmp, src_size):
                smbclient.remove(remote_tmp)
                raise ImportError_(
                    f"read-back of {filename!r} didn't match the source; "
                    "discarded the copy, the next sweep starts over."
                )

            existing = self._remote_size(remote)  # the copy can take hours: look again
            _guard_existing(remote, existing, src_size)
            if existing is not None:  # only an empty leftover gets past the guard
                with contextlib.suppress(OSError):
                    smbclient.remove(remote)
            smbclient.rename(remote_tmp, remote)
            if not self._matches(local_src, remote, src_size):
                with contextlib.suppress(OSError):
                    smbclient.remove(remote)
                raise ImportError_(
                    f"{filename!r} didn't match the source after the rename; "
                    "removed it, the next sweep starts over."
                )
        except OSError as exc:
            raise _classify_oserror(exc, remote) from exc
        return remote

    def _remote_size(self, remote: str) -> int | None:
        import smbclient

        try:
            return int(smbclient.stat(remote).st_size)
        except OSError as exc:
            if exc.errno == errno.ENOENT:  # SMBOSError isn't a FileNotFoundError
                return None
            raise

    def _matches(self, local_src: str, remote: str, size: int) -> bool:
        import smbclient

        if self._remote_size(remote) != size:
            return False
        with open(local_src, "rb") as fsrc, smbclient.open_file(remote, mode="rb") as fcopy:
            return _same_ranges(fsrc, fcopy, _sample_ranges(size))
