"""Scheduler job registration + the global concurrency guard."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
        acquire_interval_minutes=0,  # disabled
        sync_interval_minutes=5,
        import_reconcile_interval_minutes=0,  # disabled
        backup_interval_minutes=0,  # disabled
    )
    sched = build_scheduler(cfg)
    assert {j.id for j in sched.get_jobs()} == {"discovery", "sync"}


def test_build_scheduler_registers_acquire_job() -> None:
    from homeTheater.scheduler import build_scheduler
    from homeTheater.scheduler.scheduler import MISFIRE_GRACE_SECONDS

    cfg = _config(
        scan_interval_minutes=0,
        discovery_interval_minutes=0,
        subtitle_interval_minutes=0,
        acquire_interval_minutes=30,
        sync_interval_minutes=0,
        import_reconcile_interval_minutes=0,
        backup_interval_minutes=0,
    )
    sched = build_scheduler(cfg)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"acquire"}
    # A blocked loop / busy lock must delay a run, not silently skip it.
    assert jobs["acquire"].misfire_grace_time == MISFIRE_GRACE_SECONDS
    assert jobs["acquire"].coalesce is True


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


async def test_guarded_skips_quietly_on_not_configured(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NotConfiguredError is the expected 'provider missing' skip — no alert."""

    from homeTheater.config import get_config
    from homeTheater.errors import NotConfiguredError
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)

    async def not_configured() -> str | None:
        raise NotConfiguredError("RADARR_API_KEY is not set")

    await jobs._guarded("acquire", get_config(), not_configured)
    assert sent == []


async def test_guarded_alerts_on_plain_value_error(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine ValueError is a real failure, not a quiet config skip."""

    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)

    async def buggy() -> str | None:
        raise ValueError("unexpected payload shape")

    await jobs._guarded("discovery", get_config(), buggy)
    assert len(sent) == 1 and "failed" in sent[0]


async def test_guarded_redacts_secrets_in_failure_notification(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.scheduler import jobs

    sent: list[str] = []

    async def fake_notify(config: AppConfig, text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(jobs, "notify", fake_notify)

    async def leaky() -> str | None:
        raise RuntimeError(
            "Client error '401 Unauthorized' for url "
            "'https://api.themoviedb.org/3/configuration?api_key=sekret123'"
        )

    await jobs._guarded("discovery", get_config(), leaky)
    assert len(sent) == 1
    assert "sekret123" not in sent[0]
    assert "api_key=REDACTED" in sent[0]
