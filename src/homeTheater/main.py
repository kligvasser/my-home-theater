"""Entry point: ``home-theater [serve|scan]`` (defaults to ``serve``)."""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

from .errors import JobBusyError, redact_exc
from .logging_setup import configure_logging, get_logger

if TYPE_CHECKING:
    from .scanner.repair import RepairPlan

log = get_logger(__name__)


def _configure() -> None:
    configure_logging(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        json_logs=os.environ.get("LOG_JSON", "").lower() in {"1", "true", "yes"},
    )


def serve() -> None:
    import uvicorn

    _configure()
    uvicorn.run(
        "homeTheater.api.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),  # noqa: S104 - bind LAN by default
        port=int(os.environ.get("PORT", "8000")),
        reload=os.environ.get("RELOAD", "").lower() in {"1", "true", "yes"},
    )


def scan(force_prune: bool = False) -> None:
    """Run a one-off NAS library scan against the configured roots."""

    from .config import get_config
    from .db import init_db
    from .scanner import run_scan

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    try:
        stats = run_scan(config, force_prune=force_prune)
    except JobBusyError as exc:
        log.warning("scan.cli_skipped", reason=redact_exc(exc))
        raise SystemExit(2) from None
    except RuntimeError as exc:  # a failed root or refused prune; already in job_run
        print(redact_exc(exc))
        raise SystemExit(1) from None
    log.info("scan.cli_done", **stats.as_dict())


def enrich(force: bool = False) -> None:
    """Backfill TMDb/IMDb ids, ratings, votes, and genres onto the catalog."""

    import asyncio

    from .config import get_config
    from .db import init_db
    from .locks import job_lock
    from .metadata import enrich_catalog

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    try:
        with job_lock(config, "enrich"):
            stats = asyncio.run(enrich_catalog(config, force=force))
    except JobBusyError as exc:
        log.warning("enrich.cli_skipped", reason=redact_exc(exc))
        raise SystemExit(2) from None
    log.info("enrich.cli_done", **stats.as_dict())


def discover() -> None:
    """Find candidate titles that pass your thresholds and aren't already owned."""

    import asyncio

    from .config import effective_config
    from .db import init_db
    from .discovery import run_discovery

    _configure()
    init_db()  # dev convenience; production uses Alembic
    # effective_config: dashboard runtime overrides (thresholds etc.) apply here too.
    try:
        stats = asyncio.run(run_discovery(effective_config()))
    except JobBusyError as exc:
        log.warning("discover.cli_skipped", reason=redact_exc(exc))
        raise SystemExit(2) from None
    log.info("discover.cli_done", **stats.as_dict())


def subtitles() -> None:
    """Fetch missing target-language subtitles (Bazarr or native backend)."""

    import asyncio

    from .config import effective_config
    from .db import init_db
    from .subtitles import sweep_subtitles

    _configure()
    init_db()  # dev convenience; production uses Alembic
    stats = asyncio.run(sweep_subtitles(effective_config()))
    log.info("subtitles.cli_done", **stats.as_dict())


def subtitle_sync() -> None:
    """Check the timing of subtitles chosen without a hash match (needs ffsubsync)."""

    import asyncio

    from .config import effective_config
    from .db import init_db
    from .errors import NotConfiguredError
    from .subtitles.sync import run_sync_check

    _configure()
    init_db()  # dev convenience; production uses Alembic
    try:
        stats = asyncio.run(run_sync_check(effective_config()))
    except (NotConfiguredError, JobBusyError) as exc:
        print(redact_exc(exc))
        raise SystemExit(2) from None
    log.info("subtitle_sync.cli_done", **stats.as_dict())


def kodi_sync() -> None:
    """Read Kodi's watch history and send any library scan that is due."""

    import asyncio

    from .config import effective_config
    from .db import init_db
    from .errors import NotConfiguredError
    from .kodi import KodiUnavailableError, scan_if_due, sync_history

    async def both() -> None:
        config = effective_config()
        await scan_if_due(config)
        await sync_history(config)

    _configure()
    init_db()  # dev convenience; production uses Alembic
    try:
        asyncio.run(both())
    except (NotConfiguredError, JobBusyError, KodiUnavailableError) as exc:
        print(redact_exc(exc))
        raise SystemExit(2) from None


def trakt_sync() -> None:
    """Pull Trakt's watched history; push Kodi's if trakt.sync.push is on."""

    import asyncio

    import httpx

    from .config import effective_config
    from .db import init_db
    from .errors import NotConfiguredError
    from .watch import sync_trakt

    _configure()
    init_db()  # dev convenience; production uses Alembic
    try:
        stats = asyncio.run(sync_trakt(effective_config()))
    except (NotConfiguredError, JobBusyError, httpx.HTTPError) as exc:
        print(redact_exc(exc))
        raise SystemExit(2) from None
    log.info("trakt_sync.cli_done", **stats.as_dict())


def acquire() -> None:
    """Hand all approved candidates to Radarr/Sonarr (respects dry_run)."""

    import asyncio

    from .acquisition import queue_approved
    from .config import effective_config
    from .db import init_db

    _configure()
    init_db()  # dev convenience; production uses Alembic
    try:
        stats = asyncio.run(queue_approved(effective_config()))
    except JobBusyError as exc:
        log.warning("acquire.cli_skipped", reason=redact_exc(exc))
        raise SystemExit(2) from None
    log.info("acquire.cli_done", **stats.as_dict())


def sync() -> None:
    """Poll Radarr/Sonarr and advance in-flight download states."""

    import asyncio

    from .acquisition import sync_downloads
    from .config import effective_config
    from .db import init_db

    _configure()
    init_db()  # dev convenience; production uses Alembic
    try:
        stats = asyncio.run(sync_downloads(effective_config()))
    except JobBusyError as exc:
        log.warning("sync.cli_skipped", reason=redact_exc(exc))
        raise SystemExit(2) from None
    log.info("sync.cli_done", **stats.as_dict())


def reconcile() -> None:
    """Poll Radarr/Sonarr and reconcile owned items into the catalog."""

    import asyncio

    from .config import get_config
    from .db import init_db
    from .reconcile import reconcile_library

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    stats = asyncio.run(reconcile_library(config))
    log.info("reconcile.cli_done", **stats.as_dict())


def apply_naming() -> None:
    """Push the library folder-structure policy to Radarr/Sonarr/Bazarr."""

    import asyncio

    from .acquisition.naming import apply_naming_policy
    from .config import effective_config

    _configure()
    report = asyncio.run(apply_naming_policy(effective_config()))
    for service, result in report.as_dict().items():
        print(f"{service:8} {result}")


def trakt_auth() -> None:
    """Authorize Trakt via the device flow; tokens land in the setting table."""

    import asyncio

    import httpx

    from .config import get_config
    from .db import init_db
    from .trakt import TraktClient

    _configure()
    secrets = get_config().secrets
    client_id, client_secret = secrets.trakt_client_id, secrets.trakt_client_secret
    if client_id is None or client_secret is None:
        print("TRAKT_CLIENT_ID / TRAKT_CLIENT_SECRET are not set in .env.")
        return
    init_db()

    async def flow() -> None:
        async with httpx.AsyncClient(timeout=15.0) as http:
            client = TraktClient(client_id, client_secret.get_secret_value(), http)
            device = await client.device_code()
            print(f"\nGo to {device['verification_url']} and enter: {device['user_code']}\n")
            print("Waiting for approval…")
            await client.poll_device_token(device)
            items = await client.watchlist()
            print(f"Authorized ✓ — your watchlist has {len(items)} item(s).")

    asyncio.run(flow())


def train() -> None:
    """Train the preference classifier from approve/reject decisions."""

    from .config import get_config
    from .db import init_db
    from .preferences import train as train_model

    _configure()
    init_db()
    stats = train_model(get_config())
    print(stats.message)
    if stats.trained:
        print(f"model -> {stats.model_path} (AUC: {stats.auc})")


def insights() -> None:
    """Cluster the owned library by content and print the taste profile."""

    from .config import get_config
    from .db import init_db
    from .db.models import TitleKind
    from .taste import build_index

    _configure()
    cfg = get_config().taste
    init_db()  # dev convenience; production uses Alembic
    for kind in TitleKind:
        label = "movies" if kind is TitleKind.movie else "series"
        index = build_index(kind, min_library=cfg.min_library)
        if index is None:
            print(f"{label}: not enough enriched owned titles (need {cfg.min_library}+)")
            continue
        print(f"\n{label} — {index.size} titles:")
        for c in index.clusters(cfg.max_clusters):
            print(f"  [{c.size:>3}] {c.label}")
            print(f"        e.g. {', '.join(c.titles[:5])}")


def backup() -> None:
    """Write a timestamped SQLite backup and prune old ones."""

    from .backup import backup_database
    from .config import get_config

    _configure()
    dest = backup_database(get_config())
    log.info("backup.cli_done", dest=str(dest))


def cleanup(apply: bool = False) -> None:
    """Find (and with --apply, remove) NAS extras + stuck completed torrents."""

    import asyncio

    from .cleanup import apply_cleanup, plan_cleanup
    from .config import get_config
    from .db import init_db

    _configure()
    config = get_config()
    init_db()

    try:
        plan = asyncio.run(plan_cleanup(config))
    except JobBusyError as exc:
        print(f"Cleanup skipped: {redact_exc(exc)}")
        raise SystemExit(2) from None
    gb = plan.extras_bytes / 1e9
    print(f"NAS extras: {len(plan.extras)} file(s), {gb:.2f} GB")
    for e in plan.extras:
        print(f"  {e.size / 1e6:8.1f} MB  {e.path}")
    print(f"Stuck torrents: {len(plan.stuck)}")
    for t in plan.stuck:
        print(f"  [{t.reason}] {t.name}")
    if plan.unmanaged:
        print(f"Unmanaged torrents (never removed automatically): {len(plan.unmanaged)}")
    for t in plan.unmanaged:
        print(f"  [{t.reason}] {t.name}")
    for note in plan.notes:
        print(f"note: {note}")

    if not apply:
        print("\nDry run. Re-run with --apply to remove the above (NAS files go to the trash).")
        return
    try:
        result = asyncio.run(
            apply_cleanup(
                config,
                extra_paths=[e.path for e in plan.extras],
                infohashes=[t.infohash for t in plan.stuck],
            )
        )
    except JobBusyError as exc:
        print(f"Cleanup skipped: {redact_exc(exc)}")
        raise SystemExit(2) from None
    log.info("cleanup.cli_done", **{k: v for k, v in result.items() if k != "notes"})
    print(
        f"\nRemoved {result.get('extras_deleted', 0)} extra file(s), "
        f"removed {result.get('torrents_removed', 0)} torrent(s)."
    )
    for err in result.get("errors", []):
        print(f"  error: {err}")


def trash(action: str, item_id: int | None = None, dry_run: bool = False) -> None:
    """List, restore or purge the NAS trash."""

    from .config import get_config
    from .db import init_db
    from .errors import NotConfiguredError
    from .trash import TrashError, list_trash, purge, restore

    _configure()
    config = get_config()
    init_db()
    try:
        if action == "list":
            items = list_trash(config)
            for item in items:
                size = (item.size_bytes or 0) / 1e6
                print(f"{item.id:>5}  {size:8.1f} MB  purge {item.purge_on}  {item.original_path}")
            print(f"{len(items)} item(s) in the trash.")
        elif action == "restore":
            assert item_id is not None  # argparse requires it
            print(f"Restored {restore(config, item_id)}")
        else:
            stats = purge(config, dry_run=dry_run)
            if dry_run:
                print(f"Dry run: {stats.expired} item(s) past retention.")
            else:
                print(f"Purged {stats.purged} of {stats.expired} expired item(s).")
            for error in stats.errors:
                print(f"  error: {error}")
    except (JobBusyError, NotConfiguredError, TrashError, ValueError) as exc:
        print(f"Trash: {redact_exc(exc)}")
        raise SystemExit(2) from None


def upgrades() -> None:
    """Look for clearly better copies of owned movies. Proposes; never downloads."""

    import asyncio

    from .config import effective_config
    from .db import init_db
    from .errors import NotConfiguredError
    from .upgrades import scan_for_upgrades

    _configure()
    init_db()
    try:
        stats = asyncio.run(scan_for_upgrades(effective_config()))
    except (JobBusyError, NotConfiguredError) as exc:
        print(f"Upgrades skipped: {redact_exc(exc)}")
        raise SystemExit(2) from None
    print(f"Checked {stats.checked} movie(s), proposed {stats.proposed} upgrade(s).")


def _print_title_repair(plan: RepairPlan) -> None:
    for repair in plan.repairs:
        print(f"{repair.folder}  ->  {repair.keep.title!r} (id {repair.keep.id})")
        for ref in repair.merge:
            print(f"  merge {ref.title!r} (id {ref.id}, {ref.files} file(s))")
        for ref in repair.move_files:
            print(f"  move {ref.files} file(s) from {ref.title!r} (id {ref.id})")
        if repair.rename_to is not None:
            print(f"  rename to {repair.rename_to!r}")
    for ref in plan.suspect:
        print(f"check by hand: {ref.title!r} (id {ref.id}) doesn't resemble its folder")
    if plan.skipped_paths:
        print(f"{plan.skipped_paths} series file(s) are outside the TV root; left alone.")


def repair_titles(apply: bool = False) -> None:
    """Merge series split by filename into their folder's title (dry run unless --apply)."""

    from .config import get_config
    from .db import init_db
    from .scanner.repair import repair_guard
    from .scanner.repair import repair_titles as run_repair

    _configure()
    config = get_config()
    init_db()
    tv_root = config.nas.tv_root
    if not apply:
        plan = run_repair(tv_root)
        _print_title_repair(plan)
        print(f"\nDry run: {len(plan.repairs)} folder(s). Re-run with --apply to repair them.")
        return
    try:
        with repair_guard(config):
            plan = run_repair(tv_root, apply=True)
    except JobBusyError as exc:
        print(f"Repair skipped: {redact_exc(exc)}")
        raise SystemExit(2) from None
    _print_title_repair(plan)
    print(f"\nRepaired {len(plan.repairs)} folder(s).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="home-theater")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the dashboard/API server (default)")
    scan_p = sub.add_parser("scan", help="scan the NAS and update the owned catalog")
    scan_p.add_argument(
        "--force-prune",
        action="store_true",
        help="prune past the scanner.* safety limits (the files really were deleted)",
    )
    enrich_p = sub.add_parser("enrich", help="backfill TMDb/IMDb metadata onto the catalog")
    enrich_p.add_argument(
        "--force",
        action="store_true",
        help="re-attempt titles still missing data, ignoring the retry cooldown",
    )
    sub.add_parser("discover", help="find candidate titles that pass your thresholds")
    sub.add_parser("subtitles", help="ask Bazarr to search for missing subtitles")
    sub.add_parser("subtitle-sync", help="check subtitle timing with ffsubsync")
    sub.add_parser("kodi-sync", help="read Kodi watch history, send any due library scan")
    sub.add_parser("trakt-sync", help="pull Trakt watched history (and push, if enabled)")
    sub.add_parser("acquire", help="queue approved candidates to Radarr/Sonarr")
    sub.add_parser("sync", help="advance in-flight download states from Radarr/Sonarr")
    sub.add_parser("reconcile", help="reconcile Radarr/Sonarr owned items into the catalog")
    sub.add_parser("insights", help="cluster the owned library and print the taste profile")
    sub.add_parser("trakt-auth", help="authorize Trakt (device flow) for the watchlist source")
    sub.add_parser("apply-naming", help="push the folder-structure policy to Radarr/Sonarr/Bazarr")
    sub.add_parser("train", help="train the preference classifier from approve/reject labels")
    sub.add_parser("backup", help="write a timestamped SQLite backup")
    cleanup_p = sub.add_parser(
        "cleanup", help="find NAS extras + stuck torrents (dry run unless --apply)"
    )
    cleanup_p.add_argument("--apply", action="store_true", help="actually delete/remove")
    trash_p = sub.add_parser("trash", help="list, restore or purge the NAS trash")
    trash_sub = trash_p.add_subparsers(dest="trash_action", required=True)
    trash_sub.add_parser("list", help="show what's restorable")
    trash_restore = trash_sub.add_parser("restore", help="put a file back; never overwrites")
    trash_restore.add_argument("item_id", type=int)
    trash_purge = trash_sub.add_parser("purge", help="delete what's past trash.retention_days")
    trash_purge.add_argument("--dry-run", action="store_true", help="only count what would go")
    sub.add_parser("upgrades", help="propose better copies of owned movies (never downloads)")
    repair_p = sub.add_parser(
        "repair-titles", help="merge series split by filename (dry run unless --apply)"
    )
    repair_p.add_argument("--apply", action="store_true", help="actually merge the titles")
    args = parser.parse_args()

    if args.command == "scan":
        scan(force_prune=args.force_prune)
    elif args.command == "enrich":
        enrich(force=args.force)
    elif args.command == "discover":
        discover()
    elif args.command == "subtitles":
        subtitles()
    elif args.command == "subtitle-sync":
        subtitle_sync()
    elif args.command == "kodi-sync":
        kodi_sync()
    elif args.command == "trakt-sync":
        trakt_sync()
    elif args.command == "acquire":
        acquire()
    elif args.command == "sync":
        sync()
    elif args.command == "reconcile":
        reconcile()
    elif args.command == "insights":
        insights()
    elif args.command == "trakt-auth":
        trakt_auth()
    elif args.command == "apply-naming":
        apply_naming()
    elif args.command == "train":
        train()
    elif args.command == "backup":
        backup()
    elif args.command == "cleanup":
        cleanup(apply=args.apply)
    elif args.command == "trash":
        trash(
            args.trash_action,
            item_id=getattr(args, "item_id", None),
            dry_run=getattr(args, "dry_run", False),
        )
    elif args.command == "upgrades":
        upgrades()
    elif args.command == "repair-titles":
        repair_titles(apply=args.apply)
    else:
        serve()


if __name__ == "__main__":
    main()
