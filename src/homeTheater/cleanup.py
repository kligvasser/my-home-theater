"""Library/download cleanup (dry-run first, explicit apply).

Two safe, reversible-by-re-download janitorial jobs:

* **NAS extras** — samples, trailers and featurettes in the TV library. A file
  qualifies only on positive evidence (see :func:`_looks_like_extra`); having no
  ``SxxExx`` proves nothing, because plenty of real episodes don't.
* **Stuck torrents** — completed torrents the client is still holding whose
  every download row is ``imported`` with its NAS copy verified. Any other
  completed torrent (foreign, failed, cancelled, unverified) is reported as
  *unmanaged* and never removed; a pending import isn't touched or reported.

Nothing happens without review: :func:`plan_cleanup` reports what *would* be
removed, and :func:`apply_cleanup` takes that reviewed list and acts only on items
a fresh plan still agrees on. NAS extras go to the trash (:mod:`.trash`) unless
``trash.enabled`` is off, through the local ``/Volumes`` mount (the WD MyCloud rejects
``smbclient`` deletes), same as the importer's write path.
NAS work runs on its own thread with a timeout, so a wedged mount can't freeze the
event loop. It has a single worker so that hung calls can't use up the shared
thread pool: later cleanups time out too until the mount recovers.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from .config import AppConfig
from .db.models import Download
from .db.session import session_scope
from .errors import redact_exc
from .logging_setup import get_logger
from .scanner.filesystem import is_ignored_dir
from .scanner.parse import is_media_file
from .trash import TrashError, move_to_trash

log = get_logger(__name__)

# Download states that mean "the client still needs this torrent" — never remove
# a torrent backing one of these (its data may not be on the NAS yet).
_PENDING_STATES = ("queued", "downloading", "importing", "completed")

# Larger than this is never an extra, whatever it's called. Samples, trailers and
# featurettes sit well below it; a real episode can be mistaken for one by name.
EXTRA_MAX_BYTES = 500 * 1024 * 1024

# Folder names below the show folder (the show itself can be called "Extras").
_EXTRA_DIRS = frozenset(
    {
        "behind the scenes",
        "deleted scenes",
        "extra",
        "extras",
        "featurette",
        "featurettes",
        "interviews",
        "sample",
        "samples",
        "trailer",
        "trailers",
    }
)
_EXTRA_WORDS = frozenset({"featurette", "sample", "trailer"})
# "Season" / "episode": guessit reads neither, so check before trusting a keyword.
_HEBREW_EPISODE_RE = re.compile("עונה|פרק")
_WORD_RE = re.compile(r"[^\W_]+")

_FIND_TIMEOUT_SECONDS = 300.0
_VERIFY_TIMEOUT_SECONDS = 60.0
_DELETE_TIMEOUT_SECONDS = 300.0

_nas_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleanup-nas")


async def _on_nas_thread[T](timeout: float, fn: Callable[..., T], *args: Any) -> T:
    """Run blocking NAS I/O off the event loop; raises TimeoutError past ``timeout``.

    The thread can't be killed: a timed-out call keeps running until the OS gives
    up on it, so anything with side effects must also take a cancel flag.
    """

    future = asyncio.get_running_loop().run_in_executor(_nas_pool, fn, *args)
    return await asyncio.wait_for(future, timeout)


@dataclass(frozen=True, slots=True)
class ExtraFile:
    path: str  # absolute path on the mounted library
    size: int


@dataclass(frozen=True, slots=True)
class StuckTorrent:
    infohash: str
    name: str
    reason: str


@dataclass
class CleanupPlan:
    extras: list[ExtraFile] = field(default_factory=list)
    stuck: list[StuckTorrent] = field(default_factory=list)  # safe to remove
    unmanaged: list[StuckTorrent] = field(default_factory=list)  # reported, never removed
    notes: list[str] = field(default_factory=list)

    @property
    def extras_bytes(self) -> int:
        return sum(e.size for e in self.extras)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["extras_bytes"] = self.extras_bytes
        return d


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


def _looks_like_extra(rel_path: str) -> bool:
    """Positive evidence from a path relative to the TV root: an extras-style
    folder below the show folder, or a sample/trailer word in the filename that
    isn't just the show's name ("Trailer Park Boys"). Nothing else counts."""

    *dirs, name = rel_path.split(os.sep)
    if any(" ".join(_WORD_RE.findall(d.lower())) in _EXTRA_DIRS for d in dirs[1:]):
        return True
    show = dirs[0] if dirs else ""
    return bool((_words(os.path.splitext(name)[0]) - _words(show)) & _EXTRA_WORDS)


def _names_an_episode(name: str) -> bool:
    """Any episode number overrides the evidence: deleting a real sample late costs
    nothing, deleting "Show.S02E03.The.Sample.mkv" costs an episode."""

    from .acquisition.torrent.select import parse_season_episode

    return bool(_HEBREW_EPISODE_RE.search(name) or parse_season_episode(name)[1])


def find_nas_extras(config: AppConfig) -> tuple[list[ExtraFile], list[str]]:
    """Media files under the TV library that are provably extras — (extras, notes).

    Walks the mounted library (``torrent.library_base_dir``) so deletes can go
    through the OS mount. Requires that mount; returns a note if it's unset.
    """

    from .acquisition.torrent.importer import ensure_mounted

    base = config.torrent.library_base_dir
    notes: list[str] = []
    if not base:
        notes.append("NAS extras skipped: torrent.library_base_dir (the mount) is not set.")
        return [], notes

    tv_root = os.path.join(base, *config.nas.tv_root.split("/"))
    try:
        ensure_mounted(base, None)
    except Exception as exc:
        notes.append(f"NAS extras skipped: {redact_exc(exc)}")
        return [], notes
    if not os.path.isdir(tv_root):
        notes.append(f"NAS extras skipped: {tv_root!r} not found.")
        return [], notes

    extras: list[ExtraFile] = []
    walk_errors: list[OSError] = []
    for dirpath, dirs, files in os.walk(tv_root, onerror=walk_errors.append):
        dirs[:] = [d for d in dirs if not is_ignored_dir(d)]  # the trash, among others
        for name in files:
            full = os.path.join(dirpath, name)
            if not is_media_file(name) or not _looks_like_extra(os.path.relpath(full, tv_root)):
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if size <= EXTRA_MAX_BYTES and not _names_an_episode(name):
                extras.append(ExtraFile(full, size))
    if walk_errors:
        notes.append(f"{len(walk_errors)} directory read error(s) while walking {tv_root!r}.")
    extras.sort(key=lambda e: e.size, reverse=True)
    return extras, notes


def delete_nas_extras(
    config: AppConfig, paths: list[str], cancel: threading.Event | None = None
) -> tuple[int, list[str]]:
    """Move the given extra files to the trash. Returns (removed_count, errors).

    A file the trash can't take stays put and is reported. Only ``trash.enabled:
    false`` deletes outright. Stops between files once ``cancel`` is set, so a delete
    abandoned after a timeout doesn't carry on outside the job lock.
    """

    deleted = 0
    errors: list[str] = []
    for path in paths:
        if cancel is not None and cancel.is_set():
            break
        try:
            if config.trash.enabled:
                move_to_trash(config, path, reason="cleanup: NAS extra")
            else:
                os.remove(path)
            deleted += 1
            log.info("cleanup.extra_deleted", path=path)
        except FileNotFoundError:
            deleted += 1  # already gone — the desired end state
        except (OSError, TrashError) as exc:
            errors.append(f"{os.path.basename(path)}: {redact_exc(exc)}")
            log.warning("cleanup.extra_delete_failed", path=path, detail=redact_exc(exc))
    return deleted, errors


def _download_rows() -> dict[str, list[tuple[str | None, str | None]]]:
    """Every ``(state, save_path)`` per lower-cased hash; a hash can have several
    rows (one season pack serving more than one candidate)."""

    rows: dict[str, list[tuple[str | None, str | None]]] = {}
    with session_scope() as s:
        for infohash, state, save_path in s.execute(
            select(Download.external_id, Download.state, Download.save_path)
        ):
            if infohash:
                rows.setdefault(infohash.lower(), []).append((state, save_path))
    return rows


def _nas_copy_exists(path: str, base: str) -> bool:
    """True if ``path`` is under the library mount and really holds media: a
    non-empty file (movie) or a folder with a media file in it (season)."""

    root = os.path.normpath(base) + os.sep
    if not os.path.normpath(path).startswith(root):
        return False  # a local download dir, or an SMB path we can't check
    try:
        if os.path.isdir(path):
            return any(is_media_file(name) for name in os.listdir(path))
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _verified_paths(config: AppConfig, paths: list[str]) -> set[str]:
    """The subset of ``paths`` confirmed on the NAS; empty if there's no mount."""

    from .acquisition.torrent.importer import ensure_mounted

    base = config.torrent.library_base_dir
    if not base:
        return set()
    try:
        ensure_mounted(base, None)
    except Exception as exc:
        log.warning("cleanup.mount_unavailable", detail=redact_exc(exc))
        return set()
    return {p for p in paths if _nas_copy_exists(p, base)}


def _unmanaged_reason(rows: list[tuple[str | None, str | None]], verified: set[str]) -> str | None:
    """Why a completed torrent may not be removed; None if it may. Every row must
    pass, since a pack shared with a failed season may be that season's only copy."""

    if not rows:
        return "no download record"
    other = sorted({str(state) for state, _ in rows if state != "imported"})
    if other:
        return f"download {', '.join(other)}"
    if not all(path in verified for _, path in rows):
        return "imported, but NAS copy not verified"
    return None


async def find_stuck_torrents(
    config: AppConfig,
) -> tuple[list[StuckTorrent], list[StuckTorrent], list[str]]:
    """Classify completed torrents — (stuck, unmanaged, notes).

    ``stuck`` are safe to remove: every download row is ``imported`` and its NAS
    copy was verified just now. ``unmanaged`` is every other completed torrent,
    for a human to look at. A torrent backing a pending import is in neither.
    """

    from .acquisition.torrent.service import _download_client

    notes: list[str] = []
    known = await asyncio.to_thread(_download_rows)

    try:
        async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
            client = _download_client(config, http)
            torrents = await client.list_torrents()
    except Exception as exc:
        notes.append(f"Stuck torrents skipped: download client unreachable ({redact_exc(exc)}).")
        return [], [], notes

    held = [
        t
        for t in torrents
        if t.complete
        and not any(state in _PENDING_STATES for state, _ in known.get(t.infohash, []))
    ]
    saved = sorted({p for t in held for _, p in known.get(t.infohash, []) if p})
    verified: set[str] = set()
    if saved:
        try:
            verified = await _on_nas_thread(_VERIFY_TIMEOUT_SECONDS, _verified_paths, config, saved)
        except TimeoutError:
            notes.append(
                f"NAS copies not verified: gave up after {_VERIFY_TIMEOUT_SECONDS:.0f}s "
                "(is the mount wedged?). No torrent will be removed."
            )

    stuck: list[StuckTorrent] = []
    unmanaged: list[StuckTorrent] = []
    for t in held:
        name = t.name or t.infohash
        reason = _unmanaged_reason(known.get(t.infohash, []), verified)
        if reason is None:
            stuck.append(StuckTorrent(t.infohash, name, "already imported"))
        else:
            unmanaged.append(StuckTorrent(t.infohash, name, reason))
    return stuck, unmanaged, notes


async def remove_stuck_torrents(config: AppConfig, hashes: list[str]) -> tuple[int, list[str]]:
    """Remove the given torrents (and their local data). Returns (removed, errors)."""

    if not hashes:
        return 0, []
    from .acquisition.torrent.service import remove_torrents

    try:
        await remove_torrents(config, hashes)
    except Exception as exc:
        return 0, [redact_exc(exc)]
    return len(hashes), []


async def _plan(config: AppConfig) -> CleanupPlan:
    try:
        extras, notes = await _on_nas_thread(_FIND_TIMEOUT_SECONDS, find_nas_extras, config)
    except TimeoutError:
        extras = []
        notes = [
            f"NAS extras skipped: gave up after {_FIND_TIMEOUT_SECONDS:.0f}s "
            "(is the mount wedged?)."
        ]
    stuck, unmanaged, stuck_notes = await find_stuck_torrents(config)
    return CleanupPlan(extras=extras, stuck=stuck, unmanaged=unmanaged, notes=notes + stuck_notes)


async def plan_cleanup(config: AppConfig) -> CleanupPlan:
    """Dry-run: report what cleanup would remove, without changing anything.

    Takes the ``sync`` job lock: cleanup walks/deletes on the NAS, and the WD
    MyCloud throws EIO when that overlaps an import — so cleanup and sync are
    mutually exclusive. Raises JobBusyError if an import is in progress."""

    from .locks import job_lock

    with job_lock(config, "sync"):
        return await _plan(config)


async def apply_cleanup(
    config: AppConfig, *, extra_paths: Collection[str], infohashes: Collection[str]
) -> dict[str, Any]:
    """Perform cleanup under the ``sync`` job lock (mutually exclusive with
    imports — see :func:`plan_cleanup`). Raises JobBusyError if sync is running.

    ``extra_paths`` and ``infohashes`` are what the reviewer saw. Only items that
    are both reviewed and in a fresh plan are touched: a stale preview can't delete
    something no longer eligible, and a new find waits for its own review.
    Returns a summary dict."""

    from .locks import job_lock

    with job_lock(config, "sync"):
        return await _apply_locked(config, set(extra_paths), {h.lower() for h in infohashes})


async def _apply_locked(
    config: AppConfig, extra_paths: set[str], infohashes: set[str]
) -> dict[str, Any]:
    plan = await _plan(config)
    extras = [e for e in plan.extras if e.path in extra_paths]
    hashes = [t.infohash for t in plan.stuck if t.infohash in infohashes]
    result: dict[str, Any] = {"notes": list(plan.notes), "errors": []}

    skipped = len(extra_paths) + len(infohashes) - len(extras) - len(hashes)
    if skipped:
        result["notes"].append(f"{skipped} reviewed item(s) no longer qualify and were left alone.")

    cancel = threading.Event()
    try:
        deleted, errors = await _on_nas_thread(
            _DELETE_TIMEOUT_SECONDS, delete_nas_extras, config, [e.path for e in extras], cancel
        )
    except TimeoutError:
        cancel.set()
        deleted, errors = 0, [
            f"NAS delete gave up after {_DELETE_TIMEOUT_SECONDS:.0f}s (is the mount wedged?); "
            "some files may already be gone."
        ]
    result["extras_deleted"] = deleted
    result["extras_bytes"] = sum(e.size for e in extras)
    result["errors"].extend(errors)

    removed, errors = await remove_stuck_torrents(config, hashes)
    result["torrents_removed"] = removed
    result["errors"].extend(errors)

    log.info(
        "cleanup.applied",
        extras_deleted=result["extras_deleted"],
        torrents_removed=result["torrents_removed"],
        errors=len(result["errors"]),
    )
    return result
