"""Scheduler job registration + the global concurrency guard."""

from __future__ import annotations

import asyncio
from pathlib import Path

from homeTheater.config import AppConfig
from homeTheater.config.settings import NASPaths, Schedule


def _config(**schedule_kwargs: object) -> AppConfig:
    return AppConfig(
        nas=NASPaths(share="T", movies_root="M", tv_root="TV"),
        schedule=Schedule(**schedule_kwargs),  # type: ignore[arg-type]
    )


def test_build_scheduler_respects_intervals() -> None:
    from homeTheater.scheduler import build_scheduler

    cfg = _config(
        scan_interval_minutes=0,  # disabled
        discovery_interval_minutes=30,
        subtitle_interval_minutes=0,  # disabled
        sync_interval_minutes=5,
        import_reconcile_interval_minutes=0,  # disabled
        backup_interval_minutes=0,  # disabled
    )
    sched = build_scheduler(cfg)
    assert {j.id for j in sched.get_jobs()} == {"discovery", "sync"}


async def test_global_guard_serializes_jobs(config_file: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    active = 0
    max_seen = 0

    async def body() -> None:
        nonlocal active, max_seen
        active += 1
        max_seen = max(max_seen, active)
        await asyncio.sleep(0.02)
        active -= 1
        return None

    cfg = get_config()
    await asyncio.gather(
        jobs._guarded("a", cfg, body),
        jobs._guarded("b", cfg, body),
        jobs._guarded("c", cfg, body),
    )
    # The lock must have serialized them: never more than one at a time.
    assert max_seen == 1


async def test_guarded_swallows_failure(config_file: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    async def boom() -> None:
        raise RuntimeError("kaboom")

    # Must not propagate — a failing job can't kill the scheduler.
    await jobs._guarded("x", get_config(), boom)
