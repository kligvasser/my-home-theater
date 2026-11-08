"""Subtitle sweep (plan §5.5): ask Bazarr to search for missing subtitles.

This triggers Bazarr; Bazarr owns the actual provider search + sidecar placement.
Filtered to the configured target languages so we don't nudge Bazarr for languages
you don't care about.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from ..config import AppConfig
from ..db.models import RunStatus
from ..errors import NotConfiguredError, redact_exc
from ..locks import job_lock
from ..logging_setup import bind_run, clear_run, get_logger
from .bazarr import BazarrClient, WantedItem
from .runs import MAX_ERRORS_SHOWN, finish_run, start_run

log = get_logger(__name__)

SWEEP_LOCK = "subtitles"


@dataclass
class SweepStats:
    wanted_movies: int = 0
    wanted_episodes: int = 0
    searched_movies: int = 0
    searched_episodes: int = 0
    capped: int = 0  # wanted items skipped by max_searches_per_sweep
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def sweep_subtitles(
    config: AppConfig, *, on_started: Callable[[], None] | None = None
) -> Any:
    """Run the configured subtitle backend: Bazarr (default) or native providers.

    Returns a stats object with ``.as_dict()`` either way (SweepStats or
    NativeSweepStats), so callers log/serialize it uniformly. One sweep runs at a
    time across processes: a second raises JobBusyError. ``on_started`` fires once
    the lock is held and the backend's configuration checks have passed.
    """

    with job_lock(config, SWEEP_LOCK):
        if config.subtitles.backend == "native":
            from .native import sweep_native

            return await sweep_native(config, on_started=on_started)
        return await sweep_missing(config, on_started=on_started)


def _wants_lang(item: WantedItem, languages: set[str]) -> bool:
    return any(lang in languages for lang in item.missing_langs)


async def sweep_missing(
    config: AppConfig, *, on_started: Callable[[], None] | None = None
) -> SweepStats:
    """Trigger a Bazarr search for every wanted item missing a target language."""

    secrets = config.secrets
    if not (secrets.bazarr_url and secrets.bazarr_api_key):
        raise NotConfiguredError("BAZARR_URL / BAZARR_API_KEY are not set in .env.")

    languages = set(config.subtitles.languages)
    budget = config.subtitles.max_searches_per_sweep

    if on_started is not None:
        on_started()
    run_id = await start_run()

    bind_run(run_id=run_id, job="subtitle")
    stats = SweepStats()
    status = RunStatus.failed  # only a sweep that got to the end earns better
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            bazarr = BazarrClient(
                secrets.bazarr_url, secrets.bazarr_api_key.get_secret_value(), http
            )
            movies = [m for m in await bazarr.wanted_movies() if _wants_lang(m, languages)]
            episodes = [e for e in await bazarr.wanted_episodes() if _wants_lang(e, languages)]
            stats.wanted_movies = len(movies)
            stats.wanted_episodes = len(episodes)
            log.info("subtitle.wanted", movies=len(movies), episodes=len(episodes))

            searched = 0
            for m in movies:
                if m.radarr_id is None:
                    continue
                if searched >= budget:
                    stats.capped += 1
                    continue
                try:
                    await bazarr.search_movie(m.radarr_id)
                    stats.searched_movies += 1
                    searched += 1
                except Exception as exc:
                    stats.errors.append(f"movie {m.title}: {redact_exc(exc)}")
            for e in episodes:
                if e.sonarr_series_id is None or e.sonarr_episode_id is None:
                    continue
                if searched >= budget:
                    stats.capped += 1
                    continue
                try:
                    await bazarr.search_episode(e.sonarr_series_id, e.sonarr_episode_id)
                    stats.searched_episodes += 1
                    searched += 1
                except Exception as exc:
                    stats.errors.append(f"episode {e.title}: {redact_exc(exc)}")
            if stats.capped:
                log.info("subtitle.capped", skipped=stats.capped, budget=budget)
        if not stats.errors:
            status = RunStatus.success
        elif stats.searched_movies or stats.searched_episodes:
            status = RunStatus.partial
        log.info("subtitle.done", status=status.value, **stats.as_dict())
    except asyncio.CancelledError:
        stats.errors.append("sweep cancelled or timed out before finishing")
        raise
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(redact_exc(exc))
        log.error("subtitle.failed", error=redact_exc(exc))
    finally:
        try:
            await finish_run(run_id, status, stats.as_dict())
        finally:
            clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Subtitle sweep failed: {stats.errors[:MAX_ERRORS_SHOWN]}")
    return stats
