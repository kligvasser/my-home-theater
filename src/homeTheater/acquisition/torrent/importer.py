"""Import a completed torrent's media into the NAS library layout.

This is the app's first *write* path to the NAS. It copies the finished file
into ``Movies/<Title (Year)>/<Title (Year)>.<ext>`` (the layout the arr stack
would otherwise produce) and verifies the copy by size before it counts as done.

Two targets implement the same :class:`LibraryTarget` seam:

* :class:`SMBLibraryTarget` — writes to the NAS share over SMB (the default).
* :class:`LocalLibraryTarget` — writes to a local/mounted directory; also what
  the tests use, so import logic is exercised without a live NAS.

Copies land at a ``.part`` sidecar and are atomically renamed into place, so a
half-written file is never mistaken for a real one (mirrors the scanner's
verify-after-move rule, plan §12). Series import is intentionally not handled
here — a season pack is many files with per-episode placement we don't model yet.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from typing import Protocol

from ...config import AppConfig
from ...errors import NotConfiguredError
from ...logging_setup import get_logger
from ...scanner.parse import is_media_file

log = get_logger(__name__)

# Characters illegal in SMB/Windows and most NAS filesystems.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# A "sample" clip is only junk when it's also tiny; a real 4 GB file that merely
# has "sample" in its name is kept.
_SAMPLE_MAX_BYTES = 300 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024


class ImportError_(RuntimeError):
    """The completed torrent could not be imported (no media file, copy failed)."""


class LibraryTarget(Protocol):
    def import_file(self, local_src: str, rel_dir: str, filename: str) -> str:
        """Copy ``local_src`` into ``rel_dir/filename`` under the library root,
        creating directories, verifying size, and returning the final path."""
        ...


def _sanitize(name: str) -> str:
    cleaned = _ILLEGAL.sub("", name).strip().rstrip(". ")
    return cleaned or "Untitled"


def find_primary_video(content_path: str) -> str | None:
    """The main video file for a completed torrent: the file itself if the torrent
    is a single file, else the largest non-sample media file in its folder.

    Raises :class:`ImportError_` if the folder can't be read — on macOS,
    ``~/Downloads``/``~/Desktop``/``~/Documents`` are privacy-protected (TCC) and
    a terminal/launchd process is denied listing them, which would otherwise look
    like an empty folder ("no media file found").
    """

    if os.path.isfile(content_path):
        return content_path if is_media_file(os.path.basename(content_path)) else None
    walk_errors: list[OSError] = []
    best: str | None = None
    best_size = -1
    for dirpath, _dirnames, filenames in os.walk(content_path, onerror=walk_errors.append):
        for name in filenames:
            if not is_media_file(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            if "sample" in name.lower() and size < _SAMPLE_MAX_BYTES:
                continue
            if size > best_size:
                best, best_size = full, size
    if best is None and any(isinstance(e, PermissionError) for e in walk_errors):
        raise ImportError_(
            f"permission denied reading {content_path!r} — on macOS, grant the app "
            "Full Disk Access, or set torrent.movie_download_dir to a folder outside "
            "~/Downloads, ~/Desktop and ~/Documents (which are privacy-protected)."
        )
    return best


def _movie_dir_and_file(title: str, year: int | None, ext: str) -> tuple[str, str]:
    base = _sanitize(title) + (f" ({year})" if year else "")
    return base, f"{base}{ext}"


def import_completed_movie(
    config: AppConfig,
    target: LibraryTarget,
    *,
    content_path: str,
    title: str,
    year: int | None,
) -> str:
    """Copy a finished movie into the library and return its destination path."""

    video = find_primary_video(content_path)
    if video is None:
        raise ImportError_(f"no media file found under {content_path!r}")
    ext = os.path.splitext(video)[1].lower()
    folder, filename = _movie_dir_and_file(title, year, ext)
    rel_dir = f"{config.nas.movies_root.rstrip('/')}/{folder}"
    dest = target.import_file(video, rel_dir, filename)
    log.info("import.done", title=title, source=video, dest=dest)
    return dest


def build_library_target(config: AppConfig) -> LibraryTarget:
    """SMB target by default; a local target when ``torrent.library_base_dir`` set."""

    base = config.torrent.library_base_dir
    if base:
        return LocalLibraryTarget(base)
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

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir

    def import_file(self, local_src: str, rel_dir: str, filename: str) -> str:
        dest_dir = os.path.join(self.base_dir, *rel_dir.split("/"))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, filename)
        tmp = dest + ".part"
        shutil.copyfile(local_src, tmp)
        src_size = os.path.getsize(local_src)
        if os.path.getsize(tmp) != src_size:
            os.remove(tmp)
            raise ImportError_(f"size mismatch copying to {dest!r}")
        os.replace(tmp, dest)
        return dest


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

    def import_file(self, local_src: str, rel_dir: str, filename: str) -> str:
        import smbclient

        self._ensure_session()
        remote_dir = self._unc(rel_dir)
        smbclient.makedirs(remote_dir, exist_ok=True)
        remote = remote_dir + "\\" + filename
        remote_tmp = remote + ".part"
        src_size = os.path.getsize(local_src)
        with open(local_src, "rb") as fsrc, smbclient.open_file(remote_tmp, mode="wb") as fdst:
            while chunk := fsrc.read(_COPY_CHUNK):
                fdst.write(chunk)
        if smbclient.stat(remote_tmp).st_size != src_size:
            smbclient.remove(remote_tmp)
            raise ImportError_(f"size mismatch copying to {remote!r}")
        # Overwrite any prior import, then atomically move the verified copy in.
        with contextlib.suppress(OSError):
            smbclient.remove(remote)
        smbclient.rename(remote_tmp, remote)
        return remote
