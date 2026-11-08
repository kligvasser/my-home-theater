"""Filesystem abstraction so the scanner is testable without a live NAS.

The scanner depends only on :class:`FileSystem`. :class:`LocalFileSystem` walks a
real local directory (used in tests and for local media), and
:class:`SMBFileSystem` walks the NAS over SMB2/3. Both yield the same
:class:`FileEntry` records, and both skip NAS/OS junk (thumbnail trees, recycle
bins, AppleDouble ``._*`` forks) that would otherwise pollute the catalog.

A walk that could not read a directory must never look like a walk that found
nothing there: the scanner prunes catalog rows for files it did not see. Both
``os.walk`` and ``smbclient.walk`` ignore ``scandir`` errors unless given an
``onerror`` callback, so both implementations pass :func:`_raise_walk_error`.

The SMB side has to survive a Mac that sleeps between scans: a connection pooled
before the sleep is dead, ``.local`` names stop resolving for a while, and a dropped
session comes back without credentials unless every call carries them. So each
walk owns a private connection pool, re-registers its session, dials a resolved
address and retries transient failures from the top. Whatever finally fails still
raises, so the scanner never reads a failed walk as deleted files.
"""

from __future__ import annotations

import errno
import os
import socket
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..errors import redact_exc
from ..logging_setup import get_logger
from . import _mountwalk
from ._mountwalk import MountIdentityError

log = get_logger(__name__)

# Junk directories NAS appliances and OSes sprinkle around media trees
# (Synology @eaDir thumbnails, recycle bins, ...). They never hold real media.
IGNORED_DIRS = frozenset({"@eaDir", "@Recycle", "#recycle", "$RECYCLE.BIN"})


# Waits between attempts at a walk that failed transiently (3 attempts in all).
SMB_RETRY_DELAYS: tuple[float, ...] = (5.0, 20.0)
# Closing a pool talks to the server, so it can hang on the wedge it's clearing.
_POOL_RESET_TIMEOUT_SECONDS = 10.0

# socket-level errors that say "not now" rather than "no": NAS asleep, Wi-Fi coming
# back after a wake. Access denied or a missing root must fail on the first try.
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.ETIMEDOUT,
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ECONNABORTED,
        errno.EPIPE,
        errno.EHOSTUNREACH,
        errno.EHOSTDOWN,
        errno.ENETUNREACH,
        errno.ENETDOWN,
    }
)
# smbprotocol and pyspnego use plain exception classes for these; the message is
# the only thing that tells them apart. All four are in the production failures.
_TRANSIENT_MESSAGES = (
    "socket was closed",  # SMBConnectionClosed
    "failed to connect to",  # ValueError from smbprotocol.transport
    "no username or password",  # SpnegoError: a session re-opened without creds
    "credits",  # "Request requires 1 credits but only 0 credits are available"
)

# Last address each ``.local`` name resolved to, for when mDNS drops out mid-run.
_last_resolved: dict[str, str] = {}
_active_walks: weakref.WeakSet[SMBFileSystem | MountedFileSystem] = weakref.WeakSet()


def is_transient_smb_error(exc: BaseException) -> bool:
    """True if retrying the walk on a fresh connection could plausibly work."""

    if isinstance(exc, socket.gaierror | ConnectionError | TimeoutError):
        return True
    if isinstance(exc, OSError):
        return exc.errno in _TRANSIENT_ERRNOS
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_MESSAGES)


def _bounded(action: Callable[[], None], timeout: float = _POOL_RESET_TIMEOUT_SECONDS) -> None:
    """Run a pool reset on a daemon thread; give up waiting after ``timeout``."""

    def run() -> None:
        try:
            action()
        except Exception as exc:  # two resets racing, or a server already gone
            log.warning("scan.smb_reset_failed", error=redact_exc(exc))

    thread = threading.Thread(target=run, name="smb-pool-reset", daemon=True)
    thread.start()
    thread.join(timeout)


def reset_smb_pool() -> None:
    """Close the process-wide smbclient pool (the importer's SMB connections), so a
    thread blocked on one of its sockets gets an error instead of hanging on."""

    smbclient = sys.modules.get("smbclient")
    if smbclient is not None:
        _bounded(lambda: smbclient.reset_connection_cache(fail_on_error=False))


def abort_active_walks() -> None:
    """Stop every SMB walk in progress: the cooperative cancel for a scan that ran
    past its deadline. Each one raises :class:`WalkAbortedError`, so it fails closed."""

    for fs in list(_active_walks):
        fs.abort()


def is_ignored_dir(name: str) -> bool:
    """Dot-dirs (``.@__thumb``, ``.AppleDouble``, ``.Trash*``, ...) and NAS junk."""

    return name.startswith(".") or name in IGNORED_DIRS


def is_hidden_file(name: str) -> bool:
    """Dotfiles, including macOS AppleDouble ``._*`` resource forks."""

    return name.startswith(".")


class WalkAbortedError(RuntimeError):
    """The walk was stopped before it finished. Its listing is not usable."""


def _raise_walk_error(error: OSError) -> None:
    """``onerror`` for a walk: abort it instead of silently dropping the subtree."""

    raise error


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One file discovered during a walk."""

    path: str  # full path (local path or SMB UNC), the stable identity of a file
    name: str  # basename with extension
    parent: str  # directory containing the file (for sibling/sidecar lookup)
    size: int


@runtime_checkable
class FileSystem(Protocol):
    """Read-only view over a media tree."""

    def resolve(self, root: str) -> str:
        """Return the full path (local or UNC) that ``root`` walks from.

        This is the prefix of every :attr:`FileEntry.path` yielded for ``root``,
        used to derive root-relative paths and to scope stale-row pruning.
        """
        ...

    def walk(self, root: str) -> Iterator[FileEntry]:
        """Recursively yield files (not directories) under ``root``.

        Must raise if ``root`` or any directory below it cannot be read, including
        a read that fails part-way through a listing: a partial result is taken as
        proof that the unlisted files are gone.
        """
        ...


class LocalFileSystem:
    """Walk a local directory. Used in tests and for locally-mounted media.

    ``base_dir`` is prepended to the ``root`` passed to :meth:`walk`, so callers
    can use the same relative roots (``Movies``, ``TV Shows``) as with SMB.
    """

    def __init__(self, base_dir: str | os.PathLike[str] = "") -> None:
        self.base_dir = str(base_dir)

    def resolve(self, root: str) -> str:
        return os.path.join(self.base_dir, root) if self.base_dir else root

    def walk(self, root: str) -> Iterator[FileEntry]:
        start = self.resolve(root)
        for dirpath, dirnames, filenames in os.walk(start, onerror=_raise_walk_error):
            dirnames[:] = [d for d in dirnames if not is_ignored_dir(d)]
            for name in filenames:
                if is_hidden_file(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                yield FileEntry(path=full, name=name, parent=dirpath, size=size)


_MOUNTWALK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mountwalk.py")
# Minimal environment for the walker: no secrets, no PYTHON* overrides.
_CHILD_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
# What may come back on the child's stderr into an error message (and so a log).
_SAFE_STDERR = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-=.,:;'()/"
)


def _sanitize(text: bytes) -> str:
    decoded = text.decode("ascii", "replace").strip()
    return "".join(ch if ch in _SAFE_STDERR else "?" for ch in decoded)[:300]


class MountedFileSystem:
    """Walk the NAS through its macOS SMB mount (``/Volumes/<share>``), naming every
    file exactly as :class:`SMBFileSystem` would, so the catalog is unchanged.

    Needs no network access of its own: macOS's mount does the SMB traffic, which
    is why a launchd service without Local Network permission can still scan. All
    mount I/O (identity check included) runs in a killable child (``_mountwalk.py``)
    with an empty environment. The walk fails closed on a read error, a volume that
    isn't the share, skipped entries, or a timeout. Read-only: never writes.
    """

    def __init__(
        self,
        base_dir: str,
        host: str,
        share: str,
        *,
        accept_hosts: tuple[str, ...] = (),
        walk_timeout: float | None = None,
        ensure_mounted: Callable[[], None] | None = None,
        verify_identity: bool = True,
    ) -> None:
        self.base_dir = base_dir.rstrip("/") or "/"
        self.host = host
        self.share = share
        self._hosts = tuple(h for h in (host, *accept_hosts) if h)
        self._walk_timeout = walk_timeout
        self._ensure_mounted = ensure_mounted
        self._verify_identity = verify_identity
        self._aborted = threading.Event()
        self._proc: Any = None
        self._lock = threading.Lock()

    def resolve(self, root: str) -> str:
        # Byte-for-byte SMBFileSystem._unc(root): the root itself is not re-encoded.
        cleaned = root.strip("\\/").replace("/", "\\")
        return "\\\\" + "\\".join([self.host, self.share, cleaned])

    def abort(self) -> None:
        self._aborted.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def _check_aborted(self) -> None:
        if self._aborted.is_set():
            raise WalkAbortedError(f"mount walk of {self.share} was aborted")

    def _argv(self, root: str) -> list[str]:
        argv = [sys.executable, "-I", "-S", _MOUNTWALK, self.base_dir, root]
        if self._verify_identity:
            return [*argv, self.share, str(os.getuid()), *self._hosts]
        return [*argv, _mountwalk.NO_VERIFY]

    def _run(self, root: str) -> tuple[int, bytes, bytes]:
        import subprocess

        with self._lock:
            self._check_aborted()
            self._proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                self._argv(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_CHILD_ENV,
                close_fds=True,
            )
        proc = self._proc
        try:
            out, err = proc.communicate(timeout=self._walk_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise WalkAbortedError(f"mount walk of {root!r} timed out") from None
        finally:
            with self._lock:
                self._proc = None
            if proc.poll() is None:  # an exception mid-read: never leave it running
                proc.kill()
                proc.wait()
        self._check_aborted()
        return proc.returncode, out, err

    def _list(self, root: str) -> list[tuple[list[str], int]]:
        import json

        code, out, err = self._run(root)
        if code == _mountwalk.EXIT_NOT_MOUNTED and self._ensure_mounted is not None:
            # The one parent-side mount call, made only once the child has found
            # nothing mounted (so nothing there can hang): remount, then retry.
            self._ensure_mounted()
            code, out, err = self._run(root)
        detail = _sanitize(err) or f"exit {code}"
        if code in (_mountwalk.EXIT_IDENTITY, _mountwalk.EXIT_NOT_MOUNTED):
            raise MountIdentityError(detail)
        if code != 0:
            raise OSError(f"mount walk of {root!r} failed: {detail}")
        entries: list[tuple[list[str], int]] = []
        done: int | None = None
        for line in out.decode("utf-8").splitlines():
            record = json.loads(line)
            if "done" in record:
                done = int(record["done"])
                continue
            parts, size = record["p"], record["s"]
            if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts):
                raise OSError("mount walk returned a malformed entry")
            entries.append((parts, int(size)))
        if done is None or done != len(entries):
            raise OSError(f"mount walk of {root!r} ended early")
        return entries

    def walk(self, root: str) -> Iterator[FileEntry]:
        from ..nas_paths import encode_component

        if any(c in ("", ".", "..") for c in root.strip("/").split("/")):
            raise ValueError(f"unsafe library root {root!r}")
        _active_walks.add(self)
        started = time.time()
        try:
            listed = self._list(root)
        finally:
            _active_walks.discard(self)
        identity = self.resolve(root)
        entries = []
        for parts, size in listed:
            if not parts:
                raise OSError("mount walk returned a malformed entry")
            names = [encode_component(p) for p in parts]
            parent = "\\".join([identity, *names[:-1]])
            entries.append(
                FileEntry(path=parent + "\\" + names[-1], name=names[-1], parent=parent, size=size)
            )
        log.info("scan.mount_walked", files=len(entries), seconds=round(time.time() - started, 1))
        yield from entries


class SMBFileSystem:
    """Walk the NAS over SMB via ``smbclient`` (from the ``smbprotocol`` package).

    Paths are UNC. Two hosts are in play: :attr:`host` names the file, so it is in
    every :attr:`FileEntry.path` and therefore in ``OwnedFile.path``, and must not
    change; the connect host is where the socket goes (``connect_host`` if given,
    else a fresh resolve of a ``.local`` name). Read-only: never writes.
    """

    def __init__(
        self,
        host: str,
        share: str,
        username: str | None = None,
        password: str | None = None,
        *,
        connect_host: str | None = None,
        walk_timeout: float | None = None,
        retry_delays: tuple[float, ...] = SMB_RETRY_DELAYS,
    ) -> None:
        self.host = host
        self.share = share
        self._username = username
        self._password = password
        self._connect_host = connect_host
        self._walk_timeout = walk_timeout
        self._retry_delays = retry_delays
        self._aborted = threading.Event()
        # Private, so resetting it per walk can't cut off an import that is using
        # smbclient's global pool on another thread.
        self._pool: dict[str, Any] = {}

    def _session_args(self) -> dict[str, Any]:
        """Passed to EVERY smbclient call. A call that finds its connection gone
        opens a new one from these arguments alone; without them a dropped session
        comes back as ``SpnegoError: No username or password``."""

        if self._username and self._password:
            # Authenticated session: keep SMB signing on (the secure default).
            return {
                "username": self._username,
                "password": self._password,
                "connection_cache": self._pool,
            }
        # Guest / password-less share — the same access Kodi/Finder use for a public
        # NAS share. Guest sessions have no signing key, so many NAS (e.g. WD
        # MyCloud) reject them unless signing and secure-negotiate are relaxed. Safe
        # here: there are no credentials to protect and the walk is read-only.
        return {
            "username": self._username or "guest",
            "password": self._password or "",
            "require_signing": False,
            "connection_cache": self._pool,
        }

    def _ensure_session(self, server: str) -> None:
        import smbclient  # imported lazily so tests don't require smbprotocol

        args = self._session_args()
        if "require_signing" in args:
            smbclient.ClientConfig(require_secure_negotiate=False)
        smbclient.register_session(server, **args)

    def _reset_pool(self) -> None:
        import smbclient

        pool = self._pool
        _bounded(lambda: smbclient.reset_connection_cache(False, connection_cache=pool))

    def _resolve_connect_host(self) -> str:
        """The address to dial. Only ``.local`` names are resolved here: mDNS is
        what fails after a wake, and unicast names resolve fine inside smbclient."""

        if self._connect_host:
            return self._connect_host
        host = self.host
        if not host.lower().endswith(".local"):
            return host
        try:
            infos = socket.getaddrinfo(host, 445, socket.AF_INET, socket.SOCK_STREAM)
            address = str(infos[0][4][0])
        except socket.gaierror:
            known = _last_resolved.get(host.lower())
            if known is None:
                raise
            log.warning("scan.smb_dns_fallback", host=host, address=known)
            return known
        _last_resolved[host.lower()] = address
        return address

    def _unc(self, *parts: str, server: str | None = None) -> str:
        cleaned = [p.strip("\\/").replace("/", "\\") for p in parts if p]
        return "\\\\" + "\\".join([server or self.host, self.share, *cleaned])

    def resolve(self, root: str) -> str:
        return self._unc(root)

    def abort(self) -> None:
        """Stop this filesystem's walks from any thread. Closing the pool is what
        reaches a walk blocked inside a socket read; the flag stops it retrying."""

        self._aborted.set()
        self._reset_pool()

    def _check_aborted(self) -> None:
        if self._aborted.is_set():
            raise WalkAbortedError(f"SMB walk of {self.share} was aborted")

    def _walk_once(self, root: str) -> list[FileEntry]:
        """One complete pass on a fresh connection, listed in full before anything
        is yielded so a retry can't hand the scanner a file twice."""

        import smbclient

        self._reset_pool()
        server = self._resolve_connect_host()
        self._ensure_session(server)
        args = self._session_args()
        start = self._unc(root, server=server)
        identity = self.resolve(root)
        entries: list[FileEntry] = []
        for dirpath, dirnames, filenames in smbclient.walk(
            start, onerror=_raise_walk_error, **args
        ):
            self._check_aborted()
            # smbclient.walk mirrors os.walk: pruning dirnames in place skips
            # those subtrees (saves SMB round-trips on @eaDir thumbnail forests).
            dirnames[:] = [d for d in dirnames if not is_ignored_dir(d)]
            parent = identity + dirpath[len(start) :]
            for name in filenames:
                if is_hidden_file(name):
                    continue
                full = dirpath.rstrip("\\") + "\\" + name
                try:
                    size = smbclient.stat(full, **args).st_size
                except OSError:
                    size = 0
                entries.append(
                    FileEntry(
                        path=parent.rstrip("\\") + "\\" + name,
                        name=name,
                        parent=parent,
                        size=size,
                    )
                )
        return entries

    def _walk_with_retries(self, root: str) -> list[FileEntry]:
        delays = iter(self._retry_delays)
        while True:
            self._check_aborted()
            try:
                return self._walk_once(root)
            except Exception as exc:
                self._check_aborted()  # the abort is the story, not the closed socket
                delay = next(delays, None)
                if delay is None or not is_transient_smb_error(exc):
                    raise
                log.warning("scan.smb_retry", delay=delay, error_type=type(exc).__name__)
                if self._aborted.wait(delay):
                    self._check_aborted()

    def walk(self, root: str) -> Iterator[FileEntry]:
        _active_walks.add(self)
        watchdog: threading.Timer | None = None
        if self._walk_timeout:
            # A stuck socket read never returns to check a deadline in a loop.
            watchdog = threading.Timer(self._walk_timeout, self.abort)
            watchdog.daemon = True
            watchdog.start()
        started = time.time()
        try:
            entries = self._walk_with_retries(root)
        finally:
            if watchdog is not None:
                watchdog.cancel()
            _active_walks.discard(self)
            self._reset_pool()
        log.info("scan.smb_walked", files=len(entries), seconds=round(time.time() - started, 1))
        yield from entries
