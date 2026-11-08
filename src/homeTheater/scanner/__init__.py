"""NAS library scanner (Phase 1): read-only catalog of what you already own."""

from __future__ import annotations

from collections.abc import Mapping

from ..config import AppConfig
from ..db.models import TitleKind
from ..errors import NotConfiguredError
from ..locks import job_lock
from .filesystem import (
    FileEntry,
    FileSystem,
    LocalFileSystem,
    SMBFileSystem,
    abort_active_walks,
    reset_smb_pool,
)
from .parse import ParsedMedia, parse_media, subtitle_lang_for
from .service import ScanStats, scan_library

__all__ = [
    "FileEntry",
    "FileSystem",
    "LocalFileSystem",
    "SMBFileSystem",
    "ParsedMedia",
    "ScanStats",
    "abort_active_walks",
    "build_filesystem",
    "config_roots",
    "parse_media",
    "reset_smb_pool",
    "run_scan",
    "scan_library",
    "subtitle_lang_for",
]


def build_filesystem(config: AppConfig) -> FileSystem:
    """Construct the SMB filesystem from config + secrets, failing with guidance.

    ``SMB_HOST`` is the name catalog paths are keyed on; ``nas.connect_host``, when
    set, is the address actually dialled (see :class:`SMBFileSystem`). Credentials
    come from ``.env``.
    """

    secrets = config.secrets
    host = secrets.smb_host
    share = config.nas.share
    if not host:
        raise NotConfiguredError(
            "SMB_HOST is not set. Put the NAS IP (or hostname) in .env, e.g. "
            "SMB_HOST=192.168.1.50"
        )
    if not share:
        raise NotConfiguredError("nas.share is not set in config.yaml (the SMB share name).")

    # No (or password-less) credentials => guest/public share, common on
    # consumer NAS; SMBFileSystem opens a guest session in that case.
    password = secrets.smb_pass.get_secret_value() if secrets.smb_pass else None
    return SMBFileSystem(
        host=host,
        share=share,
        username=secrets.smb_user,
        password=password,
        connect_host=config.nas.connect_host,
        walk_timeout=config.scanner.walk_timeout_minutes * 60 or None,
    )


def config_roots(config: AppConfig) -> dict[TitleKind, str]:
    """Map each title kind to its configured NAS root path."""

    return {
        TitleKind.movie: config.nas.movies_root,
        TitleKind.series: config.nas.tv_root,
    }


def run_scan(
    config: AppConfig,
    *,
    force_prune: bool = False,
    accepted_prunes: Mapping[str, str] | None = None,
) -> ScanStats:
    """Scan the configured roots under the cross-process ``scan`` job lock.

    The one entry point for the scheduler, API and CLI. Raises JobBusyError if a
    scan is already running. The lock lives in the calling thread, so a scheduled
    scan that outlives its timeout keeps holding it until the walk really ends.
    ``accepted_prunes`` limits ``force_prune`` to the refusals a person reviewed.
    """

    with job_lock(config, "scan"):
        return scan_library(
            build_filesystem(config),
            config_roots(config),
            limits=config.scanner,
            force_prune=force_prune,
            accepted_prunes=accepted_prunes,
        )
