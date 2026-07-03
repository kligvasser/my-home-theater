"""NAS library scanner (Phase 1): read-only catalog of what you already own."""

from __future__ import annotations

from ..config import AppConfig
from ..db.models import TitleKind
from .filesystem import FileEntry, FileSystem, LocalFileSystem, SMBFileSystem
from .parse import ParsedMedia, parse_media, subtitle_lang_for
from .service import ScanStats, scan_library

__all__ = [
    "FileEntry",
    "FileSystem",
    "LocalFileSystem",
    "SMBFileSystem",
    "ParsedMedia",
    "ScanStats",
    "build_filesystem",
    "config_roots",
    "parse_media",
    "scan_library",
    "subtitle_lang_for",
]


def build_filesystem(config: AppConfig) -> FileSystem:
    """Construct the SMB filesystem from config + secrets, failing with guidance.

    Prefers ``SMB_HOST`` (IP) when set, since ``.local`` mDNS is flaky from some
    hosts/containers (plan §5.2). Credentials come from ``.env``.
    """

    secrets = config.secrets
    host = secrets.smb_host
    share = config.nas.share
    if not host:
        raise ValueError(
            "SMB_HOST is not set. Put the NAS IP (or hostname) in .env, e.g. "
            "SMB_HOST=192.168.1.50"
        )
    if not share:
        raise ValueError("nas.share is not set in config.yaml (the SMB share name).")
    if not (secrets.smb_user and secrets.smb_pass):
        raise ValueError("SMB_USER / SMB_PASS are not set in .env.")

    return SMBFileSystem(
        host=host,
        share=share,
        username=secrets.smb_user,
        password=secrets.smb_pass.get_secret_value(),
    )


def config_roots(config: AppConfig) -> dict[TitleKind, str]:
    """Map each title kind to its configured NAS root path."""

    return {
        TitleKind.movie: config.nas.movies_root,
        TitleKind.series: config.nas.tv_root,
    }
