"""The NAS trash: what the app deletes from the library can be put back.

A delete becomes a rename into ``<library_base_dir>/.home-theater-trash/<date>/<original
relative path>``, on the same volume, so it's instant. Nothing here ever copies: a
rename that can't happen leaves the file where it is and says so, and a file that
can't be moved is never deleted instead.

Every path is re-checked at the moment of use, stored ones included, because a row
is only as trustworthy as whatever last wrote the database:

* A library path must resolve inside the library and outside the trash root.
* A trash path must resolve inside the trash root. :func:`purge_expired` touches
  nothing else: symlinks are unlinked, never followed, and folders are never removed
  recursively.
* Nothing runs without the mount. A dropped share reads as "file gone", and that
  must not count as a finished move or a finished purge.

A row is written ``pending`` before the rename and confirmed after it, so a crash
in between leaves a record that :func:`_settle_pending` can resolve from what's on
disk. Purge leaves files it has no row for alone; their folder path still says where
they came from.

Callers that aren't already inside the ``sync`` lock use :func:`restore` and
:func:`purge`: the WD MyCloud throws EIO when NAS work overlaps an import.
"""

from __future__ import annotations

import errno
import os
import stat
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from .config import AppConfig
from .db.base import utcnow
from .db.models import TrashItem
from .db.session import session_scope
from .errors import NotConfiguredError, redact_exc
from .locks import job_lock
from .logging_setup import get_logger

log = get_logger(__name__)

TRASH_DIRNAME = ".home-theater-trash"

STATE_PENDING = "pending"
STATE_TRASHED = "trashed"
STATE_RESTORED = "restored"
STATE_PURGED = "purged"

_MAX_NAME_TRIES = 1000
_FAULT_ERRNOS = frozenset(
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


class TrashError(RuntimeError):
    """The move didn't happen and the file is where it was."""


class TrashPathError(TrashError):
    """A path is outside what the trash may touch. Refused, whatever asked for it."""


class TrashConflictError(TrashError):
    """Something already sits where a restore would put the file."""


class TrashUnavailableError(TrashError):
    """The share isn't there or is faulting, so nothing on it can be trusted."""


@dataclass(frozen=True, slots=True)
class TrashEntry:
    id: int
    original_path: str
    size_bytes: int | None
    reason: str | None
    trashed_at: str  # ISO timestamp
    purge_on: str  # ISO date


@dataclass
class PurgeStats:
    expired: int = 0
    purged: int = 0
    purged_bytes: int = 0
    already_gone: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _base(config: AppConfig) -> str:
    base = config.torrent.library_base_dir
    if not base:
        raise NotConfiguredError("The trash needs torrent.library_base_dir (the NAS mount).")
    return os.path.normpath(base)


def _mount_gone(base: str) -> bool:
    from .acquisition.torrent.importer import _mount_gone as gone

    return gone(base)


def _require_mounted(base: str) -> None:
    from .acquisition.torrent.importer import NasUnavailableError, ensure_mounted

    try:
        ensure_mounted(base, None)
    except NasUnavailableError as exc:
        raise TrashUnavailableError(redact_exc(exc)) from None


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _trash_root(base: str, *, create: bool = False) -> str:
    """The trash root, refused if it isn't really that folder in the library: a
    symlink here would point every later rename, and purge, somewhere else."""

    root = os.path.join(base, TRASH_DIRNAME)
    if create:
        os.makedirs(root, exist_ok=True)
    if os.path.realpath(root) != os.path.join(os.path.realpath(base), TRASH_DIRNAME):
        raise TrashPathError(f"{TRASH_DIRNAME} doesn't resolve inside the library")
    return root


def _clean_relative(rel: str) -> list[str]:
    """The parts of a stored relative path, refusing anything that could climb out."""

    parts = rel.split(os.sep)
    if not rel or os.path.isabs(rel) or any(p in ("", ".", "..") for p in parts):
        raise TrashPathError("refusing a path that isn't plainly relative")
    return parts


def _library_relative(base: str, path: str) -> str:
    """``path`` relative to the library, if it's strictly inside and not trash."""

    if not os.path.isabs(path) or ".." in path.split(os.sep):
        raise TrashPathError(f"refusing {os.path.basename(path)!r}: not a plain library path")
    norm = os.path.normpath(path)
    if not norm.startswith(base.rstrip(os.sep) + os.sep):
        raise TrashPathError(f"refusing {os.path.basename(path)!r}: outside the library")
    rel = norm[len(base.rstrip(os.sep)) + 1 :]
    if rel.split(os.sep)[0] == TRASH_DIRNAME:
        raise TrashPathError(f"refusing {os.path.basename(path)!r}: already in the trash")
    return rel


def _require_real_library_path(base: str, path: str) -> None:
    """Follow symlinks in the folders above ``path``: it has to end up in the library."""

    real_base = os.path.realpath(base)
    real = os.path.realpath(os.path.dirname(path))
    if not _within(real, real_base) or _within(real, os.path.join(real_base, TRASH_DIRNAME)):
        raise TrashPathError(f"refusing {os.path.basename(path)!r}: resolves outside the library")


def _trash_member(root: str, rel: str) -> str:
    """Absolute path of a stored trash path, proven to resolve inside ``root``."""

    path = os.path.join(root, *_clean_relative(rel))
    if not _within(os.path.realpath(os.path.dirname(path)), os.path.realpath(root)):
        raise TrashPathError(f"refusing {os.path.basename(path)!r}: resolves outside the trash")
    return path


def _free_name(path: str) -> str:
    """``path``, or a numbered variant: a rename would silently replace what's there."""

    if not os.path.lexists(path):
        return path
    stem, ext = os.path.splitext(path)
    for n in range(1, _MAX_NAME_TRIES):
        numbered = f"{stem}.{n}{ext}"
        if not os.path.lexists(numbered):
            return numbered
    raise TrashError(f"no free name for {os.path.basename(path)!r} in the trash")


def _same_volume(a: str, b: str) -> bool:
    return os.stat(a).st_dev == os.stat(b).st_dev


def _moved_failure(exc: OSError, base: str, name: str) -> TrashError:
    """What a failed rename means. The file wasn't moved in any of these."""

    if exc.errno == errno.EXDEV:
        return TrashError(f"{name!r} is on another volume from the trash; it was kept where it is")
    if exc.errno in _FAULT_ERRNOS or _mount_gone(base):
        return TrashUnavailableError(f"NAS unavailable moving {name!r} ({exc.strerror}); kept")
    return TrashError(f"couldn't move {name!r} to the trash ({exc.strerror}); it was kept")


def _forget(item_id: int) -> None:
    with session_scope() as s:
        item = s.get(TrashItem, item_id)
        if item is not None:
            s.delete(item)


def _finish(item_id: int, state: str) -> None:
    with session_scope() as s:
        item = s.get(TrashItem, item_id)
        if item is not None:
            item.state = state
            if state != STATE_TRASHED:
                item.finished_at = utcnow()


def move_to_trash(config: AppConfig, path: str, *, reason: str) -> int:
    """Rename one library file into the trash; returns its record's id.

    Raises :class:`TrashError` with the file still in place, or
    ``FileNotFoundError`` if it was already gone (only with the share confirmed up).
    """

    base = _base(config)
    name = os.path.basename(path)
    rel = _library_relative(base, path)
    _require_mounted(base)
    _require_real_library_path(base, path)
    try:
        before = os.lstat(path)
        if stat.S_ISDIR(before.st_mode):
            raise TrashPathError(f"refusing {name!r}: the trash takes files, not folders")
        root = _trash_root(base, create=True)
        trash_rel = os.path.join(utcnow().date().isoformat(), rel)
        wanted = _trash_member(root, trash_rel)  # before makedirs, which follows symlinks
        os.makedirs(os.path.dirname(wanted), exist_ok=True)
        if not _same_volume(os.path.dirname(path), root):
            raise TrashError(f"{name!r} isn't on the trash's volume; it was kept where it is")
        target = _free_name(wanted)
    except FileNotFoundError:
        if _mount_gone(base):
            raise TrashUnavailableError(f"NAS mount dropped trashing {name!r}; kept") from None
        raise
    except OSError as exc:
        raise _moved_failure(exc, base, name) from None

    with session_scope() as s:
        item = TrashItem(
            original_path=rel,
            trash_path=os.path.relpath(target, root),
            size_bytes=before.st_size,
            reason=reason,
            state=STATE_PENDING,
            trashed_at=utcnow(),
        )
        s.add(item)
        s.flush()
        item_id = item.id

    try:
        os.rename(path, target)
    except FileNotFoundError:
        _forget(item_id)
        if _mount_gone(base):
            raise TrashUnavailableError(f"NAS mount dropped trashing {name!r}; kept") from None
        raise
    except OSError as exc:
        _forget(item_id)
        raise _moved_failure(exc, base, name) from None

    try:
        landed = os.lstat(target)
    except OSError:
        # The rename returned, then the share went. The pending row stays for
        # _settle_pending, which can tell what really happened once it's back.
        raise TrashUnavailableError(f"can't confirm {name!r} reached the trash") from None
    if landed.st_size != before.st_size:
        log.warning("trash.size_changed", path=rel, before=before.st_size, after=landed.st_size)
    _finish(item_id, STATE_TRASHED)
    log.info("trash.moved", path=rel, reason=reason)
    return item_id


def _settle_pending(base: str, root: str) -> None:
    """Resolve records of a move nobody saw finish, from what's on disk. A file in
    neither place stays pending: with both missing, nothing here can be concluded."""

    with session_scope() as s:
        pending = [
            (i.id, i.original_path, i.trash_path)
            for i in s.scalars(select(TrashItem).where(TrashItem.state == STATE_PENDING)).all()
        ]
    for item_id, original, trashed in pending:
        try:
            if os.path.lexists(_trash_member(root, trashed)):
                _finish(item_id, STATE_TRASHED)
            elif os.path.lexists(os.path.join(base, *_clean_relative(original))):
                _forget(item_id)
        except (OSError, TrashError) as exc:
            log.warning("trash.settle_failed", item=item_id, detail=redact_exc(exc))


def list_trash(config: AppConfig) -> list[TrashEntry]:
    """What's restorable, newest first. Database only, so a wedged mount can't hang it."""

    keep = timedelta(days=config.trash.retention_days)
    with session_scope() as s:
        items = s.scalars(
            select(TrashItem)
            .where(TrashItem.state == STATE_TRASHED)
            .order_by(TrashItem.trashed_at.desc(), TrashItem.id.desc())
        ).all()
        return [
            TrashEntry(
                id=i.id,
                original_path=i.original_path,
                size_bytes=i.size_bytes,
                reason=i.reason,
                trashed_at=_aware(i.trashed_at).isoformat(),
                purge_on=(_aware(i.trashed_at) + keep).date().isoformat(),
            )
            for i in items
        ]


def restore(config: AppConfig, item_id: int) -> str:
    """Put a trashed file back; returns its relative path. Takes the ``sync`` lock
    (JobBusyError if an import is running). Refuses to overwrite anything."""

    with job_lock(config, "sync"):
        return _restore(config, item_id)


def _restore(config: AppConfig, item_id: int) -> str:
    base = _base(config)
    with session_scope() as s:
        item = s.get(TrashItem, item_id)
        if item is None:
            raise ValueError(f"trash item {item_id} not found")
        state, original, trashed = item.state, item.original_path, item.trash_path
    if state != STATE_TRASHED:
        raise TrashError(f"trash item {item_id} is {state}, nothing to restore")

    parts = _clean_relative(original)
    if parts[0] == TRASH_DIRNAME:
        raise TrashPathError("refusing to restore into the trash")
    dest = os.path.join(base, *parts)
    _require_real_library_path(base, dest)
    _require_mounted(base)
    name = os.path.basename(dest)
    try:
        root = _trash_root(base)
        source = _trash_member(root, trashed)
        os.lstat(source)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not _same_volume(root, os.path.dirname(dest)):
            raise TrashError(f"{name!r} would cross a volume boundary; it stays in the trash")
        # Last, right before the rename, which would silently replace what's there. POSIX
        # has no portable no-replace rename; the sync lock keeps the app's own writers out.
        if os.path.lexists(dest):
            raise TrashConflictError(f"{original!r} already exists; nothing was overwritten")
        os.rename(source, dest)
    except FileNotFoundError:
        if _mount_gone(base):
            raise TrashUnavailableError(f"NAS mount dropped restoring {name!r}") from None
        raise TrashError(f"{name!r} is no longer in the trash") from None
    except OSError as exc:
        raise _moved_failure(exc, base, name) from None
    _finish(item_id, STATE_RESTORED)
    log.info("trash.restored", path=original)
    return original


def _expired(cutoff: datetime) -> list[tuple[int, str, int | None]]:
    with session_scope() as s:
        items = s.scalars(select(TrashItem).where(TrashItem.state == STATE_TRASHED)).all()
        return [(i.id, i.trash_path, i.size_bytes) for i in items if _aware(i.trashed_at) <= cutoff]


def _has_pending() -> bool:
    with session_scope() as s:
        return s.scalar(select(TrashItem.id).where(TrashItem.state == STATE_PENDING)) is not None


def _prune_empty_dirs(path: str, root: str) -> None:
    """Remove folders the purge emptied, up to but never including ``root``."""

    folder = os.path.dirname(path)
    while folder != root and _within(folder, root):
        try:
            os.rmdir(folder)  # only ever succeeds on an empty folder
        except OSError:
            return
        folder = os.path.dirname(folder)


def _purge_one(root: str, rel: str) -> bool:
    """Unlink one trash entry. False if it was already gone. ``unlink`` is the point:
    a symlink goes and what it points at doesn't, and a folder is an error."""

    path = _trash_member(root, rel)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    _prune_empty_dirs(path, root)
    return True


def purge(
    config: AppConfig, *, dry_run: bool = False, cancel: threading.Event | None = None
) -> PurgeStats:
    """Delete what's past ``trash.retention_days``, under the ``sync`` lock."""

    if dry_run:
        return purge_expired(config, dry_run=True)
    with job_lock(config, "sync"):
        return purge_expired(config, cancel=cancel)


def purge_expired(
    config: AppConfig,
    *,
    dry_run: bool = False,
    cancel: threading.Event | None = None,
    now: datetime | None = None,
) -> PurgeStats:
    """The only code that destroys trash. Stops at the first sign the share is gone."""

    base = _base(config)
    cutoff = (now or utcnow()) - timedelta(days=config.trash.retention_days)
    items = _expired(cutoff)
    stats = PurgeStats(expired=len(items))
    if dry_run:
        stats.purged_bytes = sum(size or 0 for _, _, size in items)
        return stats
    if not items and not _has_pending():
        return stats  # nothing to do, so no reason to touch a share that may be wedged

    _require_mounted(base)
    root = _trash_root(base)
    if not os.path.isdir(root):
        if items:
            stats.errors.append(f"{TRASH_DIRNAME} is missing; {len(items)} record(s) left as is")
        return stats
    _settle_pending(base, root)

    for item_id, rel, size in items:
        if cancel is not None and cancel.is_set():
            break
        if _mount_gone(base):
            stats.errors.append("NAS mount dropped during the purge; stopped")
            break
        try:
            existed = _purge_one(root, rel)
        except (OSError, TrashError, ValueError) as exc:
            stats.errors.append(f"{os.path.basename(rel)}: {redact_exc(exc)}")
            log.warning("trash.purge_failed", item=item_id, detail=redact_exc(exc))
            continue
        _finish(item_id, STATE_PURGED)
        if existed:
            stats.purged += 1
            stats.purged_bytes += size or 0
        else:
            stats.already_gone += 1
    log.info("trash.purged", **{k: v for k, v in stats.as_dict().items() if k != "errors"})
    return stats
