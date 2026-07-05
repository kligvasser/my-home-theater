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
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from ...config import AppConfig
from ...errors import NotConfiguredError
from ...logging_setup import get_logger
from ...scanner.parse import is_media_file

log = get_logger(__name__)

# Called during a copy with (bytes_copied, total_bytes) so the dashboard can show
# NAS-import progress. May be None.
ProgressCb = Callable[[int, int], None] | None

# Characters illegal in SMB/Windows and most NAS filesystems.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# A "sample" clip is only junk when it's also tiny; a real 4 GB file that merely
# has "sample" in its name is kept.
_SAMPLE_MAX_BYTES = 300 * 1024 * 1024
_COPY_CHUNK = 4 * 1024 * 1024  # 4 MiB — throughput + progress granularity


class ImportError_(RuntimeError):
    """The completed torrent could not be imported (no media file, copy failed)."""


def _copy_stream(fsrc: Any, fdst: Any, total: int, on_progress: ProgressCb) -> int:
    """Copy fsrc -> fdst in chunks, reporting progress. Returns bytes written."""

    written = 0
    while chunk := fsrc.read(_COPY_CHUNK):
        fdst.write(chunk)
        written += len(chunk)
        if on_progress is not None:
            on_progress(written, total)
    return written


@dataclass(frozen=True, slots=True)
class SmbMount:
    """Enough to re-mount a dropped ``/Volumes/<share>`` SMB mount (WD MyCloud and
    other guest shares drop the mount during sustained multi-GB copies)."""

    host: str
    share: str
    username: str | None
    password: str | None

    @property
    def url(self) -> str:
        user = self.username or "guest"
        auth = f"{user}:{quote(self.password, safe='')}" if self.password else user
        return f"smb://{auth}@{self.host}/{self.share}"


def ensure_mounted(base_dir: str, mount: SmbMount | None) -> None:
    """Guard a ``/Volumes/<share>`` library path before writing to it.

    A dropped SMB mount leaves the path either gone or a bare root-owned stub, so
    a copy fails with a cryptic ``Permission denied: '/Volumes/…'``. For such paths
    we verify the mount and, given credentials, try one ``mount volume`` re-mount so
    a dropped share self-heals instead of failing the whole sync run. Plain local
    dirs (not under ``/Volumes``) are left alone — ``makedirs`` handles those.
    """

    if not base_dir.startswith("/Volumes/"):
        return
    if os.path.ismount(base_dir):
        return
    if mount is None:
        raise ImportError_(
            f"NAS share not mounted at {base_dir!r} — remount it "
            "(Finder → Go → Connect to Server) and retry."
        )
    log.warning("import.mount_dropped", base_dir=base_dir)
    try:
        # Never surface the raw command/exception: mount.url embeds the password.
        proc = subprocess.run(
            ["osascript", "-e", f'mount volume "{mount.url}"'],
            capture_output=True,
            timeout=30,
        )
    except Exception:
        raise ImportError_(
            f"NAS share not mounted at {base_dir!r}; auto-remount timed out. "
            "Remount it manually and retry."
        ) from None
    if proc.returncode != 0 or not os.path.ismount(base_dir):
        raise ImportError_(
            f"NAS share not mounted at {base_dir!r}; auto-remount failed. "
            "Remount it manually and retry."
        )
    log.info("import.remounted", base_dir=base_dir)


class LibraryTarget(Protocol):
    def import_file(
        self, local_src: str, rel_dir: str, filename: str, on_progress: ProgressCb = None
    ) -> str:
        """Copy ``local_src`` into ``rel_dir/filename`` under the library root,
        creating directories, verifying size, and returning the final path.
        Calls ``on_progress(copied, total)`` during the copy when provided."""
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
    on_progress: ProgressCb = None,
) -> str:
    """Copy a finished movie into the library and return its destination path."""

    video = find_primary_video(content_path)
    if video is None:
        raise ImportError_(f"no media file found under {content_path!r}")
    ext = os.path.splitext(video)[1].lower()
    folder, filename = _movie_dir_and_file(title, year, ext)
    rel_dir = f"{config.nas.movies_root.rstrip('/')}/{folder}"
    dest = target.import_file(video, rel_dir, filename, on_progress)
    log.info("import.done", title=title, source=video, dest=dest)
    return dest


def build_library_target(config: AppConfig) -> LibraryTarget:
    """SMB target by default; a local target when ``torrent.library_base_dir`` set."""

    base = config.torrent.library_base_dir
    if base:
        # For a /Volumes/<share> mount, carry the SMB creds so a dropped mount can
        # self-heal mid-sync (guest shares drop during big copies).
        mount: SmbMount | None = None
        if base.startswith("/Volumes/") and config.secrets.smb_host:
            mount = SmbMount(
                host=config.secrets.smb_host,
                share=os.path.basename(base.rstrip("/")),
                username=config.secrets.smb_user,
                password=(
                    config.secrets.smb_pass.get_secret_value() if config.secrets.smb_pass else None
                ),
            )
        return LocalLibraryTarget(base, mount)
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

    def __init__(self, base_dir: str, mount: SmbMount | None = None) -> None:
        self.base_dir = base_dir
        self.mount = mount

    def import_file(
        self, local_src: str, rel_dir: str, filename: str, on_progress: ProgressCb = None
    ) -> str:
        ensure_mounted(self.base_dir, self.mount)  # self-heal a dropped NAS mount
        dest_dir = os.path.join(self.base_dir, *rel_dir.split("/"))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, filename)
        tmp = dest + ".part"
        src_size = os.path.getsize(local_src)
        with open(local_src, "rb") as fsrc, open(tmp, "wb") as fdst:
            _copy_stream(fsrc, fdst, src_size, on_progress)
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

    def import_file(
        self, local_src: str, rel_dir: str, filename: str, on_progress: ProgressCb = None
    ) -> str:
        import smbclient

        self._ensure_session()
        remote_dir = self._unc(rel_dir)
        smbclient.makedirs(remote_dir, exist_ok=True)
        remote = remote_dir + "\\" + filename
        remote_tmp = remote + ".part"
        src_size = os.path.getsize(local_src)
        with open(local_src, "rb") as fsrc, smbclient.open_file(remote_tmp, mode="wb") as fdst:
            _copy_stream(fsrc, fdst, src_size, on_progress)
        if smbclient.stat(remote_tmp).st_size != src_size:
            smbclient.remove(remote_tmp)
            raise ImportError_(f"size mismatch copying to {remote!r}")
        # Overwrite any prior import, then atomically move the verified copy in.
        with contextlib.suppress(OSError):
            smbclient.remove(remote)
        smbclient.rename(remote_tmp, remote)
        return remote
