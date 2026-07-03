"""Filesystem abstraction so the scanner is testable without a live NAS.

The scanner depends only on :class:`FileSystem`. :class:`LocalFileSystem` walks a
real local directory (used in tests and for local media), and
:class:`SMBFileSystem` walks the NAS over SMB2/3. Both yield the same
:class:`FileEntry` records.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


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

    def walk(self, root: str) -> Iterator[FileEntry]:
        """Recursively yield files (not directories) under ``root``."""
        ...


class LocalFileSystem:
    """Walk a local directory. Used in tests and for locally-mounted media.

    ``base_dir`` is prepended to the ``root`` passed to :meth:`walk`, so callers
    can use the same relative roots (``Movies``, ``TV Shows``) as with SMB.
    """

    def __init__(self, base_dir: str | os.PathLike[str] = "") -> None:
        self.base_dir = str(base_dir)

    def _resolve(self, root: str) -> str:
        return os.path.join(self.base_dir, root) if self.base_dir else root

    def walk(self, root: str) -> Iterator[FileEntry]:
        start = self._resolve(root)
        for dirpath, _dirnames, filenames in os.walk(start):
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                yield FileEntry(path=full, name=name, parent=dirpath, size=size)


class SMBFileSystem:
    """Walk the NAS over SMB via ``smbclient`` (from the ``smbprotocol`` package).

    Paths are UNC (``\\\\host\\share\\root\\...``). Supports connecting by IP as a
    fallback for flaky ``.local`` mDNS (plan §5.2). Read-only: never writes.
    """

    def __init__(self, host: str, share: str, username: str, password: str) -> None:
        self.host = host
        self.share = share
        self._username = username
        self._password = password
        self._registered = False

    def _ensure_session(self) -> None:
        if self._registered:
            return
        import smbclient  # imported lazily so tests don't require smbprotocol

        smbclient.register_session(self.host, username=self._username, password=self._password)
        self._registered = True

    def _unc(self, *parts: str) -> str:
        cleaned = [p.strip("\\/").replace("/", "\\") for p in parts if p]
        return "\\\\" + "\\".join([self.host, self.share, *cleaned])

    def walk(self, root: str) -> Iterator[FileEntry]:
        import smbclient

        self._ensure_session()
        start = self._unc(root)
        for dirpath, _dirnames, filenames in smbclient.walk(start):
            for name in filenames:
                full = dirpath.rstrip("\\") + "\\" + name
                try:
                    size = smbclient.stat(full).st_size
                except OSError:
                    size = 0
                yield FileEntry(path=full, name=name, parent=dirpath, size=size)
