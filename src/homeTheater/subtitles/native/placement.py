r"""Place a downloaded subtitle next to its media file, in a ``Subs/`` subfolder.

Owned-file paths are SMB UNC (``\\host\share\Movies\...``) from the scanner, but
the reliable write path is the local SMB mount (same lesson as the torrent
importer). :func:`resolve_local_media` maps a UNC path onto ``library_base_dir``;
local paths (tests, mounted media) pass through unchanged. The file is named
``<media stem>.<lang>.srt`` (or ``.ass``/``.ssa`` for those formats) so the scanner
detects its language on the next scan.
"""

from __future__ import annotations

import os
import uuid

from ...config import AppConfig
from ...errors import NotConfiguredError, redact_exc
from ...logging_setup import get_logger
from .base import InvalidSubtitleError
from .payload import MAX_DECOMPRESSED_BYTES, SUBTITLE_EXTENSIONS, validate_subtitle

log = get_logger(__name__)


def resolve_local_media(media_path: str, config: AppConfig) -> str:
    """Local filesystem path for an owned media file (mapping UNC → the mount)."""

    if not media_path.startswith("\\\\"):
        return media_path  # already local (mounted media or tests)
    base = config.subtitles.library_base_dir
    if not base:
        raise NotConfiguredError(
            "Owned files are SMB paths; set subtitles.library_base_dir to the "
            "mounted share (e.g. /Volumes/Elements_25A1-1) so subs can be written."
        )
    # \\host\share\A\B\file.mkv -> drop host + share, keep the rest.
    parts = [p for p in media_path.lstrip("\\").split("\\") if p]
    rel = parts[2:]  # [host, share, *rel]
    return os.path.join(base, *rel)


def library_mounted(config: AppConfig) -> bool:
    """False when ``library_base_dir`` is set but nothing is mounted there."""

    base = config.subtitles.library_base_dir
    return not base or os.path.ismount(base)


def media_present(path: str) -> bool:
    """Whether the media file exists. Unlike ``os.path.exists`` an I/O failure raises:
    a sick share must not look like a missing file."""

    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def subtitle_dest(local_media_path: str, lang: str, subs_folder: str, ext: str = ".srt") -> str:
    directory = os.path.dirname(local_media_path)
    stem = os.path.splitext(os.path.basename(local_media_path))[0]
    return os.path.join(directory, subs_folder, f"{stem}.{lang}{ext}")


def find_valid_subtitle(local_media_path: str, lang: str, subs_folder: str) -> str | None:
    """Path of a usable subtitle already at the destination, if any.

    A garbage file (an HTML page stored by an older version) is not adoptable; it
    is left for a fresh download to overwrite.
    """

    for ext in SUBTITLE_EXTENSIONS:
        path = subtitle_dest(local_media_path, lang, subs_folder, ext)
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_DECOMPRESSED_BYTES + 1)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if len(data) > MAX_DECOMPRESSED_BYTES:
            continue
        try:
            validate_subtitle(data, lang)
        except InvalidSubtitleError:
            continue
        return path
    return None


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("subtitle.temp_cleanup_failed", path=path, detail=redact_exc(exc))


def write_subtitle(dest: str, data: bytes) -> None:
    """Atomically write ``data`` to ``dest`` through a uniquely named temp file.

    The temp name is never reused, so a ``.part`` left open on the NAS can't block
    later writes. The legacy ``<dest>.part`` is removed when possible; if it is
    still busy that is only logged.
    """

    directory = os.path.dirname(dest)
    os.makedirs(directory, exist_ok=True)
    _remove_quietly(dest + ".part")
    tmp = os.path.join(directory, f".{uuid.uuid4().hex}.part")
    try:
        with open(tmp, "xb") as f:
            f.write(data)
        os.replace(tmp, dest)
    finally:
        _remove_quietly(tmp)
