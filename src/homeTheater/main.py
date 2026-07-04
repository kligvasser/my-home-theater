"""Entry point: ``home-theater [serve|scan]`` (defaults to ``serve``)."""

from __future__ import annotations

import argparse
import os

from .logging_setup import configure_logging, get_logger

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


def scan() -> None:
    """Run a one-off NAS library scan against the configured roots."""

    from .config import get_config
    from .db import init_db
    from .scanner import build_filesystem, config_roots, scan_library

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    fs = build_filesystem(config)
    stats = scan_library(fs, config_roots(config))
    log.info("scan.cli_done", **stats.as_dict())


def enrich(force: bool = False) -> None:
    """Backfill TMDb/IMDb ids, ratings, votes, and genres onto the catalog."""

    import asyncio

    from .config import get_config
    from .db import init_db
    from .metadata import enrich_catalog

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    stats = asyncio.run(enrich_catalog(config, force=force))
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
    stats = asyncio.run(run_discovery(effective_config()))
    log.info("discover.cli_done", **stats.as_dict())


def subtitles() -> None:
    """Ask Bazarr to search for all missing target-language subtitles."""

    import asyncio

    from .config import get_config
    from .db import init_db
    from .subtitles import sweep_missing

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    stats = asyncio.run(sweep_missing(config))
    log.info("subtitles.cli_done", **stats.as_dict())


def acquire() -> None:
    """Hand all approved candidates to Radarr/Sonarr (respects dry_run)."""

    import asyncio

    from .acquisition import queue_approved
    from .config import get_config
    from .db import init_db

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    stats = asyncio.run(queue_approved(config))
    log.info("acquire.cli_done", **stats.as_dict())


def sync() -> None:
    """Poll Radarr/Sonarr and advance in-flight download states."""

    import asyncio

    from .acquisition import sync_downloads
    from .config import get_config
    from .db import init_db

    _configure()
    config = get_config()
    init_db()  # dev convenience; production uses Alembic
    stats = asyncio.run(sync_downloads(config))
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="home-theater")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the dashboard/API server (default)")
    sub.add_parser("scan", help="scan the NAS and update the owned catalog")
    enrich_p = sub.add_parser("enrich", help="backfill TMDb/IMDb metadata onto the catalog")
    enrich_p.add_argument(
        "--force",
        action="store_true",
        help="re-attempt titles still missing data, ignoring the retry cooldown",
    )
    sub.add_parser("discover", help="find candidate titles that pass your thresholds")
    sub.add_parser("subtitles", help="ask Bazarr to search for missing subtitles")
    sub.add_parser("acquire", help="queue approved candidates to Radarr/Sonarr")
    sub.add_parser("sync", help="advance in-flight download states from Radarr/Sonarr")
    sub.add_parser("reconcile", help="reconcile Radarr/Sonarr owned items into the catalog")
    sub.add_parser("insights", help="cluster the owned library and print the taste profile")
    sub.add_parser("trakt-auth", help="authorize Trakt (device flow) for the watchlist source")
    sub.add_parser("train", help="train the preference classifier from approve/reject labels")
    sub.add_parser("backup", help="write a timestamped SQLite backup")
    args = parser.parse_args()

    if args.command == "scan":
        scan()
    elif args.command == "enrich":
        enrich(force=args.force)
    elif args.command == "discover":
        discover()
    elif args.command == "subtitles":
        subtitles()
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
    elif args.command == "train":
        train()
    elif args.command == "backup":
        backup()
    else:
        serve()


if __name__ == "__main__":
    main()
