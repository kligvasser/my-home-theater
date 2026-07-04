"""Native subtitle sweep: fetch missing subs from providers, write beside media.

Drives off our own catalog: for each owned file lacking a target language, search
the enabled providers (in configured order — first with a hit wins, so you can put
ktuvit ahead of OpenSubtitles for Hebrew), download the best, and write it into the
file's ``Subs/`` folder. Capped per run to respect provider quotas. Recording a
``Subtitle`` row and updating the file's ``subtitle_langs`` means the next sweep
skips what we've already fetched.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy import select

from ...config import AppConfig
from ...db.base import utcnow
from ...db.models import JobRun, OwnedFile, RunStatus, Subtitle, Title, TitleKind
from ...db.session import session_scope
from ...errors import NotConfiguredError, redact_exc
from ...logging_setup import bind_run, clear_run, get_logger
from .base import SubtitleQuery, SubtitleSource, opensubtitles_hash
from .ktuvit import KtuvitSource
from .opensubtitles import OpenSubtitlesSource
from .opensubtitles_org import OpenSubtitlesOrgSource
from .placement import resolve_local_media, subtitle_dest, write_subtitle

log = get_logger(__name__)


@dataclass
class NativeSweepStats:
    considered: int = 0  # (file, language) pairs missing a sub
    downloaded: int = 0
    not_found: int = 0
    capped: int = 0  # skipped because max_searches_per_sweep was hit
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _Work:
    owned_file_id: int
    title_id: int
    lang: str
    media_path: str
    kind: TitleKind
    title: str
    year: int | None
    imdb_id: str | None
    season: int | None
    episode: int | None


def _build_sources(config: AppConfig, http: httpx.AsyncClient) -> list[SubtitleSource]:
    s = config.secrets
    sub = config.subtitles
    out: list[SubtitleSource] = []
    for name in sub.sources:
        if name == "opensubtitles":
            if s.opensubtitles_api_key is None:
                log.warning("subtitle.source_unconfigured", source=name)
                continue
            out.append(
                OpenSubtitlesSource(
                    s.opensubtitles_api_key.get_secret_value(),
                    http,
                    username=s.opensubtitles_username,
                    password=(
                        s.opensubtitles_password.get_secret_value()
                        if s.opensubtitles_password
                        else None
                    ),
                    user_agent=sub.opensubtitles_user_agent,
                    timeout=sub.request_timeout,
                )
            )
        elif name == "opensubtitles_org":
            if not (s.opensubtitles_org_username and s.opensubtitles_org_password):
                log.warning("subtitle.source_unconfigured", source=name)
                continue
            out.append(
                OpenSubtitlesOrgSource(
                    s.opensubtitles_org_username,
                    s.opensubtitles_org_password.get_secret_value(),
                    http,
                    user_agent=sub.opensubtitles_org_user_agent,
                    timeout=sub.request_timeout,
                )
            )
        elif name == "ktuvit":
            if not (s.ktuvit_email and s.ktuvit_password):
                log.warning("subtitle.source_unconfigured", source=name)
                continue
            out.append(
                KtuvitSource(
                    s.ktuvit_email,
                    s.ktuvit_password.get_secret_value(),
                    http,
                    timeout=sub.request_timeout,
                )
            )
        else:
            log.warning("subtitle.unknown_source", source=name)
    return out


def _collect_work(config: AppConfig) -> list[_Work]:
    languages = config.subtitles.languages
    work: list[_Work] = []
    with session_scope() as s:
        for f, t in s.execute(
            select(OwnedFile, Title).join(Title, Title.id == OwnedFile.title_id)
        ).all():
            have = set(f.subtitle_langs or [])
            for lang in languages:
                if lang in have:
                    continue
                work.append(
                    _Work(
                        owned_file_id=f.id,
                        title_id=f.title_id,
                        lang=lang,
                        media_path=f.path,
                        kind=f.kind,
                        title=t.title,
                        year=t.year,
                        imdb_id=t.imdb_id,
                        season=f.season,
                        episode=f.episode,
                    )
                )
    return work


async def _fetch_one(config: AppConfig, sources: list[SubtitleSource], item: _Work) -> str | None:
    """Search + download + place one subtitle; return the dest path or ``None``."""

    local_media = resolve_local_media(item.media_path, config)  # may raise NotConfigured
    exists = os.path.exists(local_media)
    query = SubtitleQuery(
        lang=item.lang,
        kind=item.kind,
        title=item.title,
        year=item.year,
        imdb_id=item.imdb_id,
        release_name=os.path.splitext(os.path.basename(local_media))[0],
        season=item.season,
        episode=item.episode,
        moviehash=opensubtitles_hash(local_media) if exists else None,
        filesize=os.path.getsize(local_media) if exists else None,
    )
    for source in sources:
        if not source.supports(item.lang):
            continue
        try:
            results = await source.search(query)
            if not results:
                continue
            best = max(results, key=lambda r: r.score)
            data = await source.download(best)
        except Exception as exc:
            # A dead provider / exhausted quota / bad payload must not abort the
            # fallback chain (e.g. OpenSubtitles.com 401 while ktuvit could serve it).
            log.warning(
                "subtitle.source_error", source=source.name, lang=item.lang, detail=redact_exc(exc)
            )
            continue
        if not data:
            continue
        dest = subtitle_dest(local_media, item.lang, config.organizer.subs_folder)
        write_subtitle(dest, data)
        _record(item, source.name, dest)
        log.info(
            "subtitle.downloaded",
            title=item.title,
            lang=item.lang,
            source=source.name,
            dest=dest,
        )
        return dest
    return None


def _record(item: _Work, provider: str, dest: str) -> None:
    with session_scope() as s:
        s.add(
            Subtitle(
                title_id=item.title_id,
                owned_file_id=item.owned_file_id,
                lang=item.lang,
                provider=provider,
                path=dest,
                status="downloaded",
            )
        )
        f = s.get(OwnedFile, item.owned_file_id)
        if f is not None:
            # Reassign a new list so SQLAlchemy flags the JSON column dirty.
            f.subtitle_langs = sorted({*(f.subtitle_langs or []), item.lang})


async def fetch_for_owned_file(config: AppConfig, owned_file_id: int) -> list[str]:
    """Fetch missing target-language subtitles for one owned file (used right after
    a torrent import so the pipeline completes without waiting for a full sweep)."""

    languages = config.subtitles.languages
    with session_scope() as s:
        f = s.get(OwnedFile, owned_file_id)
        if f is None:
            return []
        title = s.get(Title, f.title_id)
        if title is None:
            return []
        have = set(f.subtitle_langs or [])
        works = [
            _Work(
                owned_file_id=f.id,
                title_id=f.title_id,
                lang=lang,
                media_path=f.path,
                kind=f.kind,
                title=title.title,
                year=title.year,
                imdb_id=title.imdb_id,
                season=f.season,
                episode=f.episode,
            )
            for lang in languages
            if lang not in have
        ]
    if not works:
        return []

    out: list[str] = []
    async with httpx.AsyncClient(timeout=config.subtitles.request_timeout) as http:
        sources = _build_sources(config, http)
        if not sources:
            return []
        for w in works:
            try:
                dest = await _fetch_one(config, sources, w)
            except Exception as exc:
                log.warning(
                    "subtitle.fetch_failed", title=w.title, lang=w.lang, detail=redact_exc(exc)
                )
                continue
            if dest is not None:
                out.append(dest)
    return out


async def sweep_native(config: AppConfig) -> NativeSweepStats:
    """Fetch every missing target-language subtitle for owned files (capped)."""

    budget = config.subtitles.max_searches_per_sweep
    with session_scope() as s:
        run = JobRun(kind="subtitle", started_at=utcnow(), status=RunStatus.running)
        s.add(run)
        s.flush()
        run_id = run.id

    bind_run(run_id=run_id, job="subtitle")
    stats = NativeSweepStats()
    status = RunStatus.success
    try:
        async with httpx.AsyncClient(timeout=config.subtitles.request_timeout) as http:
            sources = _build_sources(config, http)
            if not sources:
                raise NotConfiguredError(
                    "No subtitle sources configured; set OpenSubtitles/ktuvit creds "
                    "in .env and subtitles.sources in config.yaml."
                )
            work = _collect_work(config)
            stats.considered = len(work)
            attempted = 0
            for item in work:
                # Cap on searches *attempted*, not just successful downloads —
                # otherwise a library of titles with no available subs re-searches
                # every provider on every run and burns quota unbounded.
                if attempted >= budget:
                    stats.capped += 1
                    continue
                attempted += 1
                try:
                    dest = await _fetch_one(config, sources, item)
                    if dest is not None:
                        stats.downloaded += 1
                    else:
                        stats.not_found += 1
                except Exception as exc:
                    stats.errors.append(f"{item.title} [{item.lang}]: {redact_exc(exc)}")
            log.info("subtitle.done", **stats.as_dict())
    except Exception as exc:
        status = RunStatus.failed
        stats.errors.append(redact_exc(exc))
        log.error("subtitle.failed", error=redact_exc(exc))
    finally:
        with session_scope() as s:
            job_run = s.get(JobRun, run_id)
            if job_run is not None:
                job_run.finished_at = utcnow()
                job_run.status = status
                job_run.stats = stats.as_dict()
        clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Native subtitle sweep failed: {stats.errors}")
    return stats
