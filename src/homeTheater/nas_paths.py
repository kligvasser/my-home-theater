"""Map between the catalog's UNC paths and a macOS SMB mount of the same share.

``OwnedFile.path`` is the UNC path ``smbclient`` sees, ``\\\\<SMB_HOST>\\<share>\\...``.
The same file through ``/Volumes/<share>`` differs in two ways, both checked
against the live library (1361 of 1361 files identical after mapping):

* macOS hands back decomposed (NFD) names for some accented letters, while
  ``smbclient`` gives the NAS's composed (NFC) form.
* Characters Windows forbids, and a trailing dot or space, travel over SMB as the
  Services-for-Mac private-use code points (``Crime.101.2026.`` is
  ``Crime.101.2026\\uf029`` to ``smbclient``). The mount shows the plain character.

Every conversion goes through here, so the scanner, the importer and subtitle
placement agree on one identity per file. Mapping to a mount path refuses anything
that could leave the mount (``..``, empty or absolute components, a path whose
real location is outside it through a symlink).
"""

from __future__ import annotations

import os
import unicodedata

# Services for Mac mapping (as used by Samba's ``catia``/``fruit`` and macOS smbfs).
_SFM_ENCODE = {
    '"': "",
    "*": "",
    ":": "",
    "<": "",
    ">": "",
    "?": "",
    "\\": "",
    "|": "",
}
_SFM_DECODE = {v: k for k, v in _SFM_ENCODE.items()}
_TRAILING_SPACE = ""
_TRAILING_DOT = ""
# Where macOS mounts network shares (a seam for tests).
VOLUMES = "/Volumes"


class MountPathError(ValueError):
    """A UNC path that cannot be mapped safely into the mount."""


def encode_component(name: str) -> str:
    """One mount path component as ``smbclient`` names it."""

    out = "".join(_SFM_ENCODE.get(ch, ch) for ch in unicodedata.normalize("NFC", name))
    if out.endswith(" "):
        out = out[:-1] + _TRAILING_SPACE
    elif out.endswith("."):
        out = out[:-1] + _TRAILING_DOT
    return out


def decode_component(name: str) -> str:
    """One UNC path component as the mount names it."""

    out = "".join(_SFM_DECODE.get(ch, ch) for ch in name)
    if out.endswith(_TRAILING_SPACE):
        out = out[:-1] + " "
    elif out.endswith(_TRAILING_DOT):
        out = out[:-1] + "."
    return out


def unc_for(host: str, share: str, rel_components: list[str]) -> str:
    """The catalog path for mount-relative components (already in mount form)."""

    return "\\\\" + "\\".join([host, share, *(encode_component(c) for c in rel_components)])


def mount_to_unc(path: str, base: str, host: str, share: str) -> str | None:
    """UNC path for ``path`` under the mount ``base``, or None if it isn't under it."""

    root = base.rstrip("/")
    if not root or not (path == root or path.startswith(root + "/")):
        return None
    rel = [c for c in path[len(root) :].split("/") if c]
    _require_safe(rel)
    return unc_for(host, share, rel)


def _require_safe(components: list[str]) -> None:
    for part in components:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part:
            raise MountPathError("path has an unsafe component")


def _rel_components(unc: str) -> list[str]:
    parts = unc.lstrip("\\").split("\\")
    if len(parts) < 3:
        raise MountPathError("not a file path inside an SMB share")
    # Checked after decoding too: ".\uf029" decodes to "..".
    raw = parts[2:]  # drop host and share
    _require_safe(raw)
    rel = [decode_component(p) for p in raw]
    _require_safe(rel)
    return rel


def unc_to_mount(unc: str, base: str) -> str:
    """Mount path for a catalog UNC path. Purely lexical (no NAS I/O, so it is safe
    on the event loop); raises :class:`MountPathError` for a malformed path or one
    with ``..`` components. Symlinks are checked where files are written, by
    :func:`require_no_redirect`."""

    rel = _rel_components(unc)
    path = os.path.join(base, *rel)
    root = os.path.normpath(base)
    if not os.path.normpath(path).startswith(root.rstrip(os.sep) + os.sep):
        raise MountPathError("path leaves the library mount")
    return path


def require_no_redirect(directory: str) -> None:
    """Refuse a write into ``/Volumes/<volume>/...`` whose folder really lives
    elsewhere: a symlinked folder on the NAS must not point a subtitle write (or its
    temp file) at the Mac's own disk. Paths outside ``/Volumes`` are left alone.
    Does NAS I/O; call it from a worker thread."""

    if any(part in (".", "..") for part in directory.split(os.sep)):
        raise MountPathError("path has an unsafe component")  # normpath would hide it
    path = os.path.normpath(directory)
    prefix = VOLUMES.rstrip(os.sep) + os.sep
    if not path.startswith(prefix):
        return
    rest = path[len(prefix) :].split(os.sep)
    volume = os.path.join(VOLUMES, rest[0])
    expected = os.path.join(os.path.realpath(volume), *rest[1:])
    if os.path.realpath(directory) != expected:
        raise MountPathError("folder on the NAS is a link that leaves the library")
