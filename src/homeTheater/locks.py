"""Cross-process job locks.

The scheduler serializes its own jobs with an ``asyncio.Lock``, but a CLI run
(``home-theater sync``) or a dashboard trigger lives in another process. Two
syncs importing the same files at once corrupt each other's ``.part`` copies
on the NAS (EBUSY / size mismatch / ENOENT), so the write-heavy jobs take an
advisory ``flock`` on a file next to the database. Non-blocking: a second
runner gets :class:`JobBusyError` and should just report "already running".
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator

from .config import AppConfig
from .errors import JobBusyError


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
            raise JobBusyError(
                f"{name} is already running in another process (lock: {path})"
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)  # releases the flock
