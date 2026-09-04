"""Library/download cleanup (dry-run first, explicit apply).

Two safe, reversible-by-re-download janitorial jobs:

* **NAS extras** — featurette/behind-the-scenes files that season packs ship
  (no ``SxxExx`` in the name). The scanner/importer ignore them now, but ones
  imported by older builds still occupy space in the TV season folders.
* **Stuck torrents** — completed torrents the client is still holding that are
  either already imported (a redundant seed) or orphaned (no live download row).
  Torrents whose import is still *pending* (state ``completed``/``importing``)
  are never touched, so nothing that hasn't reached the NAS is removed.

Everything defaults to a dry run: :func:`plan_cleanup` reports what *would* be
removed; :func:`apply_cleanup` performs it. Deletions go through the local
``/Volumes`` mount (the WD MyCloud rejects ``smbclient`` deletes), same as the
importer's write path.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from .config import AppConfig
from .db.models import Download
from .db.session import session_scope
from .errors import redact_exc
from .logging_setup import get_logger
from .scanner.parse import is_media_file

log = get_logger(__name__)

# Download states that mean "the client still needs this torrent" — never remove
# a torrent backing one of these (its data may not be on the NAS yet).
_PENDING_STATES = ("queued", "downloading", "importing", "completed")


@dataclass(frozen=True, slots=True)
class ExtraFile:
    path: str  # absolute path on the mounted library
    size: int


@dataclass(frozen=True, slots=True)
class StuckTorrent:
    infohash: str
    name: str
    reason: str  # "already imported" | "orphaned (no active download)"


@dataclass
class CleanupPlan:
    extras: list[ExtraFile] = field(default_factory=list)
    stuck: list[StuckTorrent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def extras_bytes(self) -> int:
        return sum(e.size for e in self.extras)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["extras_bytes"] = self.extras_bytes
        return d


def find_nas_extras(config: AppConfig) -> tuple[list[ExtraFile], list[str]]:
    """Non-episode media files under the TV library — (extras, notes).

    Walks the mounted library (``torrent.library_base_dir``) so deletes can go
    through the OS mount. Requires that mount; returns a note if it's unset.
    """

    from .acquisition.torrent.importer import ensure_mounted
    from .acquisition.torrent.select import parse_season_episode

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
    for dirpath, _dirs, files in os.walk(tv_root, onerror=walk_errors.append):
        for name in files:
            if not is_media_file(name):
                continue
            _seasons, episodes = parse_season_episode(name)
            if episodes:  # a real episode — keep
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            extras.append(ExtraFile(full, size))
    if walk_errors:
        notes.append(f"{len(walk_errors)} directory read error(s) while walking {tv_root!r}.")
    extras.sort(key=lambda e: e.size, reverse=True)
    return extras, notes


def delete_nas_extras(paths: list[str]) -> tuple[int, list[str]]:
    """Delete the given extra files. Returns (deleted_count, errors)."""

    deleted = 0
    errors: list[str] = []
    for path in paths:
        try:
            os.remove(path)
            deleted += 1
            log.info("cleanup.extra_deleted", path=path)
        except FileNotFoundError:
            deleted += 1  # already gone — the desired end state
        except OSError as exc:
            errors.append(f"{os.path.basename(path)}: {redact_exc(exc)}")
            log.warning("cleanup.extra_delete_failed", path=path, detail=redact_exc(exc))
    return deleted, errors


async def find_stuck_torrents(config: AppConfig) -> tuple[list[StuckTorrent], list[str]]:
    """Completed torrents safe to remove — (stuck, notes).

    Safe = the client holds it at 100% AND no live download still needs it: it's
    either already imported (redundant seed) or orphaned. A torrent backing a
    pending import (``completed``/``importing``) is left alone.
    """

    from .acquisition.torrent.service import _download_client

    notes: list[str] = []
    with session_scope() as s:
        pending = {
            h.lower()
            for h in s.scalars(
                select(Download.external_id).where(Download.state.in_(_PENDING_STATES))
            ).all()
            if h
        }
        imported = {
            h.lower()
            for h in s.scalars(
                select(Download.external_id).where(Download.state == "imported")
            ).all()
            if h
        }

    try:
        async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
            client = _download_client(config, http)
            torrents = await client.list_torrents()
    except Exception as exc:
        notes.append(f"Stuck torrents skipped: download client unreachable ({redact_exc(exc)}).")
        return [], notes

    stuck: list[StuckTorrent] = []
    for t in torrents:
        if not t.complete or t.infohash in pending:
            continue
        reason = "already imported" if t.infohash in imported else "orphaned (no active download)"
        stuck.append(StuckTorrent(t.infohash, t.name or t.infohash, reason))
    return stuck, notes


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
    extras, notes = find_nas_extras(config)
    stuck, stuck_notes = await find_stuck_torrents(config)
    return CleanupPlan(extras=extras, stuck=stuck, notes=notes + stuck_notes)


async def plan_cleanup(config: AppConfig) -> CleanupPlan:
    """Dry-run: report what cleanup would remove, without changing anything.

    Takes the ``sync`` job lock: cleanup walks/deletes on the NAS, and the WD
    MyCloud throws EIO when that overlaps an import — so cleanup and sync are
    mutually exclusive. Raises JobBusyError if an import is in progress."""

    from .locks import job_lock

    with job_lock(config, "sync"):
        return await _plan(config)


async def apply_cleanup(
    config: AppConfig, *, extras: bool = True, stuck: bool = True
) -> dict[str, Any]:
    """Perform cleanup under the ``sync`` job lock (mutually exclusive with
    imports — see :func:`plan_cleanup`). Raises JobBusyError if sync is running.

    Re-derives the plan (so it acts on current state) and deletes/removes what it
    finds. Returns a summary dict."""

    from .locks import job_lock

    with job_lock(config, "sync"):
        return await _apply_locked(config, extras=extras, stuck=stuck)


async def _apply_locked(
    config: AppConfig, *, extras: bool = True, stuck: bool = True
) -> dict[str, Any]:
    plan = await _plan(config)
    result: dict[str, Any] = {"notes": list(plan.notes)}

    if extras:
        deleted, errors = delete_nas_extras([e.path for e in plan.extras])
        result["extras_deleted"] = deleted
        result["extras_bytes"] = plan.extras_bytes
        result.setdefault("errors", []).extend(errors)
    if stuck:
        removed, errors = await remove_stuck_torrents(config, [t.infohash for t in plan.stuck])
        result["torrents_removed"] = removed
        result.setdefault("errors", []).extend(errors)

    log.info(
        "cleanup.applied",
        extras_deleted=result.get("extras_deleted"),
        torrents_removed=result.get("torrents_removed"),
        errors=len(result.get("errors", [])),
    )
    return result
