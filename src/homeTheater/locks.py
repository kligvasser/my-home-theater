"""Cross-process job locks.

The scheduler serializes jobs that share a resource with ``asyncio`` locks, but a
CLI run (``home-theater sync``) lives in another process and a dashboard trigger
bypasses the scheduler. Two
syncs importing the same files at once corrupt each other's ``.part`` copies
on the NAS (EBUSY / size mismatch / ENOENT), so the write-heavy jobs take an
advisory ``flock`` on a file next to the database. Non-blocking: a second
runner gets :class:`JobBusyError` and should just report "already running".
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import time
from collections.abc import Iterator

from .config import AppConfig
from .errors import JobBusyError, redact_exc
from .logging_setup import get_logger

log = get_logger(__name__)

# The WD MyCloud throws EIO when other work overlaps an import, and subtitle sweeps
# and scans take different locks from sync. Importers hold this shared; everyone else
# only looks. It's a hint, never a gate: no import ever waits on it or fails over it.
_IMPORT_ADVISORY = "import-copy"
_ADVISORY_TRIES = 3  # a looker holds the lock for microseconds, so this is plenty
_ADVISORY_RETRY_S = 0.001
IMPORT_POLL_S = 5.0
SUBTITLE_IMPORT_WAIT_S = 600.0  # between two subtitle items
SCAN_IMPORT_WAIT_S = 900.0  # a scheduled scan, before it gives up until its next turn


def _lock_dir(config: AppConfig) -> str:
    url = config.database.url
    if url.startswith("sqlite:///"):
        db_dir = os.path.dirname(os.path.abspath(url[len("sqlite:///") :]))
        if os.path.isdir(db_dir):
            return db_dir
    return os.path.join(os.path.expanduser("~"), ".home-theater")


@contextlib.contextmanager
def job_lock(config: AppConfig, name: str) -> Iterator[None]:
    """Hold the ``name`` job lock for the block; raise JobBusyError if taken."""

    directory = _lock_dir(config)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{name}.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise JobBusyError(f"{name} is already running") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)  # releases the flock


def is_job_running(config: AppConfig, name: str) -> bool:
    """True if the ``name`` job lock is currently held (by any process).

    A non-blocking probe: grab-and-release the flock. Used to tell the user a
    sync/acquire is already in progress instead of silently no-op'ing."""

    path = os.path.join(_lock_dir(config), f"{name}.lock")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


@contextlib.contextmanager
def import_active(config: AppConfig) -> Iterator[None]:
    """Advertise that an import copy is running for the block.

    Shared, so concurrent importers don't conflict, and released by the OS if the
    process dies. Any failure just means nobody is told.
    """

    fd: int | None = None
    try:
        directory = _lock_dir(config)
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, f"{_IMPORT_ADVISORY}.lock"), os.O_RDWR | os.O_CREAT)
        for _ in range(_ADVISORY_TRIES):
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:  # someone is looking right now
                time.sleep(_ADVISORY_RETRY_S)
    except OSError as exc:
        log.warning("import.advisory_failed", detail=redact_exc(exc))
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def is_import_active(config: AppConfig) -> bool:
    """True while any process is inside :func:`import_active`."""

    return is_job_running(config, _IMPORT_ADVISORY)


async def wait_for_import_gap(
    config: AppConfig, timeout: float, *, poll: float = IMPORT_POLL_S
) -> bool:
    """Wait up to ``timeout`` seconds for import copies to stop. False if one still is."""

    deadline = time.monotonic() + timeout
    while is_import_active(config):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(poll, remaining))
    return True
