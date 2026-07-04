r"""Place a downloaded subtitle next to its media file, in a ``Subs/`` subfolder.

Owned-file paths are SMB UNC (``\\host\share\Movies\...``) from the scanner, but
the reliable write path is the local SMB mount (same lesson as the torrent
importer). :func:`resolve_local_media` maps a UNC path onto ``library_base_dir``;
local paths (tests, mounted media) pass through unchanged. The file is named
``<media stem>.<lang>.srt`` so the scanner detects its language on the next scan.
"""

from __future__ import annotations

import os

from ...config import AppConfig
from ...errors import NotConfiguredError


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


def subtitle_dest(local_media_path: str, lang: str, subs_folder: str) -> str:
    directory = os.path.dirname(local_media_path)
    stem = os.path.splitext(os.path.basename(local_media_path))[0]
    return os.path.join(directory, subs_folder, f"{stem}.{lang}.srt")


def write_subtitle(dest: str, data: bytes) -> None:
    """Atomically write ``data`` to ``dest`` (via a ``.part`` sidecar)."""

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
