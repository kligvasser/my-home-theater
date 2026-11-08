"""Child process that checks the SMB mount and lists one library root through it.

Run by :class:`~homeTheater.scanner.filesystem.MountedFileSystem` as
``python -I -S _mountwalk.py <mount> <root> <share> <uid> <host>...`` with an empty
environment: standard library only, so it imports nothing from the app and is
handed none of its secrets (its argv holds no secret either). Everything that
touches the mount happens here, identity check included, so a hung mount stalls
only a process the parent can kill.

Writes one JSON line per file, ``{"p": [components below the root], "s": size}``,
then ``{"done": <count>}``. It fails closed, with no ``done`` line, when the volume
isn't the expected share (before and after the walk), when any folder can't be
read, and when it would have to skip something the SMB walker might have listed (a
symlink, or a volume mounted inside the share). Exit status says which:
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

EXIT_READ_ERROR = 2
EXIT_IDENTITY = 3
EXIT_NOT_MOUNTED = 4
EXIT_SKIPPED = 5
EXIT_USAGE = 64
NO_VERIFY = "--no-verify"  # tests only: walk a plain folder

# Must match filesystem.IGNORED_DIRS / is_ignored_dir / is_hidden_file (tested).
IGNORED_DIRS = frozenset({"@eaDir", "@Recycle", "#recycle", "$RECYCLE.BIN"})


def is_ignored_dir(name: str) -> bool:
    return name.startswith(".") or name in IGNORED_DIRS


def is_hidden_file(name: str) -> bool:
    return name.startswith(".")


class MountIdentityError(RuntimeError):
    """The mount point is not the NAS share the catalog belongs to."""


class NotMountedError(MountIdentityError):
    """Nothing is mounted at the mount point (the parent may remount and retry)."""


class SkippedEntriesError(OSError):
    """The walk would have had to leave out entries; its listing is incomplete."""


@dataclass(frozen=True, slots=True)
class MountInfo:
    fstype: str
    mounted_on: str
    source: str  # e.g. //user@host/share: holds the user name, never print it
    owner: int


def read_mount_info(path: str) -> MountInfo:
    """``statfs(2)`` for the filesystem holding ``path`` (macOS 64-bit-inode layout)."""

    import ctypes
    import ctypes.util
    import platform

    if sys.platform != "darwin":
        raise MountIdentityError("scanning through a mount is supported on macOS only")

    class _StatFs(ctypes.Structure):
        _fields_ = [  # noqa: RUF012 - ctypes needs a plain class attribute
            ("f_bsize", ctypes.c_uint32),
            ("f_iosize", ctypes.c_int32),
            ("f_blocks", ctypes.c_uint64),
            ("f_bfree", ctypes.c_uint64),
            ("f_bavail", ctypes.c_uint64),
            ("f_files", ctypes.c_uint64),
            ("f_ffree", ctypes.c_uint64),
            ("f_fsid", ctypes.c_int32 * 2),
            ("f_owner", ctypes.c_uint32),
            ("f_type", ctypes.c_uint32),
            ("f_flags", ctypes.c_uint32),
            ("f_fssubtype", ctypes.c_uint32),
            ("f_fstypename", ctypes.c_char * 16),
            ("f_mntonname", ctypes.c_char * 1024),
            ("f_mntfromname", ctypes.c_char * 1024),
            ("f_flags_ext", ctypes.c_uint32),
            ("f_reserved", ctypes.c_uint32 * 7),
        ]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    # Intel's plain `statfs` is the legacy 32-bit-inode layout.
    fn = getattr(libc, "statfs$INODE64") if platform.machine() == "x86_64" else libc.statfs
    buf = _StatFs()
    if fn(os.fsencode(path), ctypes.byref(buf)) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return MountInfo(
        fstype=buf.f_fstypename.decode(errors="replace"),
        mounted_on=os.fsdecode(buf.f_mntonname),
        source=os.fsdecode(buf.f_mntfromname),
        owner=int(buf.f_owner),
    )


def normalize_host(host: str) -> str:
    """Compare hosts whole: case-folded, minus a trailing ``.local`` (mDNS) and the
    Bonjour service suffix Finder mounts sometimes carry. IP literals stay exact."""

    h = host.strip().strip("[]").casefold().rstrip(".")
    h = h.removesuffix(".local")
    return h.removesuffix("._smb._tcp")


def verify_smb_mount(
    base: str,
    share: str,
    hosts: tuple[str, ...],
    uid: int,
    *,
    read_info: Callable[[str], MountInfo] = read_mount_info,
) -> None:
    """Refuse ``base`` unless it is ``uid``'s smbfs mount of ``share`` from one of
    ``hosts``: a USB disk, an empty folder or another share at the same path must
    not stand in for the library (a scan prunes what it doesn't see). Messages
    never echo the mount source, which carries the SMB user name."""

    from urllib.parse import unquote

    if not os.path.ismount(base):
        raise NotMountedError(f"nothing is mounted at {base}")
    info = read_info(base)
    if info.fstype != "smbfs":
        raise MountIdentityError(f"{base} is a {info.fstype} volume, not an SMB share")
    if os.path.realpath(info.mounted_on) != os.path.realpath(base):
        raise MountIdentityError(f"{base} is not the mount point of its volume")
    if info.owner != uid:
        raise MountIdentityError(f"{base} was mounted by another user")
    source = info.source.removeprefix("//")
    server, _, mounted_share = source.rpartition("@")[2].partition("/")
    if unquote(mounted_share.strip("/")).casefold() != share.casefold():
        raise MountIdentityError(f"{base} is not the {share!r} share")
    wanted = {normalize_host(h) for h in hosts if h}
    if normalize_host(unquote(server)) not in wanted:
        raise MountIdentityError(f"{base} is mounted from a different server")


def walk(base: str, root: str, out: TextIO, check: Callable[[], None] | None = None) -> int:
    """List ``root`` under ``base``. ``check`` re-verifies the volume, and runs
    before and after so a mount swapped mid-walk can't pass for the library."""

    if check is not None:
        check()
    device = os.stat(base).st_dev
    start = os.path.join(base, *root.split("/"))
    top = os.lstat(start)
    if not os.path.isdir(start) or os.path.islink(start) or top.st_dev != device:
        raise NotADirectoryError(f"library root {root!r} is not a folder on the share")
    count = links = foreign = 0
    lines: list[str] = []
    stack: list[tuple[str, list[str]]] = [(start, [])]
    while stack:
        path, rel = stack.pop()
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_symlink():
                    if not (is_hidden_file(entry.name) or is_ignored_dir(entry.name)):
                        links += 1
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if is_ignored_dir(entry.name):
                        continue
                    if entry.stat(follow_symlinks=False).st_dev != device:
                        foreign += 1
                        continue
                    stack.append((entry.path, [*rel, entry.name]))
                elif entry.is_file(follow_symlinks=False):
                    if is_hidden_file(entry.name):
                        continue
                    try:
                        size = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        size = 0  # as the SMB walker does
                    lines.append(json.dumps({"p": [*rel, entry.name], "s": size}))
                    count += 1
    if links or foreign:
        raise SkippedEntriesError(
            f"walk would skip {links} symlink(s) and {foreign} nested volume(s); "
            "the SMB walk may list them, so this listing can't be used to prune"
        )
    if check is not None:
        check()
    if os.stat(base).st_dev != device:
        raise MountIdentityError("the volume changed during the walk")
    for line in lines:
        out.write(line + "\n")
    out.write(json.dumps({"done": count}) + "\n")
    return count


def main(argv: list[str]) -> int:
    args = argv[1:]
    verify = NO_VERIFY not in args
    args = [a for a in args if a != NO_VERIFY]
    if len(args) < (4 if verify else 2):
        sys.stderr.write("usage: _mountwalk.py <mount> <root> <share> <uid> <host>...\n")
        return EXIT_USAGE
    base, root = args[0], args[1]
    check: Callable[[], None] | None = None
    if verify:
        share, uid, hosts = args[2], int(args[3]), tuple(args[4:])

        def check() -> None:
            verify_smb_mount(base, share, hosts, uid)

    try:
        walk(base, root, sys.stdout, check)
    except NotMountedError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_NOT_MOUNTED
    except MountIdentityError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_IDENTITY
    except SkippedEntriesError as exc:
        sys.stderr.write(f"{exc}\n")
        return EXIT_SKIPPED
    except OSError as exc:
        # Type and errno only: the parent logs this, and paths stay out of logs.
        sys.stderr.write(f"{type(exc).__name__} errno={exc.errno}\n")
        return EXIT_READ_ERROR
    except BaseException as exc:  # noqa: BLE001 - never let a traceback (paths) out
        sys.stderr.write(f"{type(exc).__name__}\n")
        return EXIT_READ_ERROR
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
