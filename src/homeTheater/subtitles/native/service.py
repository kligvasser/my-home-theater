"""Native subtitle sweep: fetch missing subs from providers, write beside media.

Drives off our own catalog: for each owned file lacking a target language, search
the enabled providers (in configured order — first with a hit wins, so you can put
ktuvit ahead of OpenSubtitles for Hebrew), download the best valid result, and write
it into the file's ``Subs/`` folder.

Work is ordered so a backlog can't starve anything: never-tried newest files first,
then the oldest retry. A (file, language) that fails enters an exponential cooldown
in ``subtitle_attempt`` and costs no budget until it expires, so an unmatchable or
poisoned item can't pin the head of the queue. ``max_searches_per_sweep`` counts
searches actually sent. A provider that keeps failing, or reports an auth or quota
problem, is disabled for the rest of the sweep.

All NAS and database work runs in worker threads with timeouts: a wedged SMB mount
must not freeze the event loop the dashboard and scheduler share.
"""

from __future__ import annotations

import asyncio
import enum
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from ...config import AppConfig, Secrets
from ...db.base import utcnow
from ...db.models import OwnedFile, RunStatus, Subtitle, SubtitleAttempt, Title, TitleKind
from ...db.session import session_scope
from ...errors import NotConfiguredError, redact_exc
from ...locks import SUBTITLE_IMPORT_WAIT_S, wait_for_import_gap
from ...logging_setup import bind_run, clear_run, get_logger
from ..choices import Rejections, content_hash, load_rejections, record_choice, result_key
from ..runs import DB_TIMEOUT_S, MAX_ERRORS_SHOWN, finish_run, start_run
from .base import (
    InvalidSubtitleError,
    SourceAuthError,
    SourceQuotaError,
    SubtitleQuery,
    SubtitleResult,
    SubtitleSource,
    opensubtitles_hash,
)
from .ktuvit import KtuvitSource
from .opensubtitles import OpenSubtitlesSource
from .opensubtitles_org import OpenSubtitlesOrgSource
from .payload import ValidatedSubtitle, validate_subtitle
from .placement import (
    find_valid_subtitle,
    library_mounted,
    media_present,
    resolve_local_media,
    subtitle_dest,
    write_subtitle,
)
from .ranking import rank_results

log = get_logger(__name__)

NAS_TIMEOUT_S = 60.0  # one hash, stat or write; generous for a sleepy disk
PROVIDER_TIMEOUT_S = 120.0  # wall clock for one search or download
MAX_NAS_TIMEOUTS = 3  # in a row: the share is wedged, stop leaving threads stuck on it
MAX_SOURCE_FAILURES = 3  # in a row: disable the source for the rest of the sweep
MAX_CANDIDATES_PER_SOURCE = 3  # downloads can spend quota; don't walk a long result list
MAX_STORED_ERRORS = 50  # job_run.stats stays small; ``failed`` keeps the real count
COOLDOWN_BASE = timedelta(days=1)
COOLDOWN_MAX = timedelta(days=30)
EXISTING_PROVIDER = "existing"  # Subtitle.provider for a file adopted from disk


class LibraryUnavailableError(RuntimeError):
    """The library mount is gone or stopped answering; stop before spending more."""


class _NasTimeoutError(RuntimeError):
    """One NAS operation overran. Its thread can't be killed, only abandoned."""


@dataclass
class NativeSweepStats:
    considered: int = 0  # (file, language) pairs missing a sub
    downloaded: int = 0
    adopted: int = 0  # a valid sub was already on disk; no provider call
    not_found: int = 0
    invalid: int = 0  # only unusable payloads were on offer
    failed: int = 0  # the item itself failed: write error, or no source could answer
    capped: int = 0  # left for later because max_searches_per_sweep was hit
    cooling_down: int = 0  # recently failed; skipped at no cost
    absent: int = 0  # media file isn't there
    unidentified: int = 0  # series file with no season/episode; searching would guess
    source_down: int = 0  # every source for this language is disabled this sweep
    unsupported: int = 0  # no configured source serves this language
    disabled_sources: dict[str, str] = field(default_factory=dict)  # name -> reason
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add_error(self, message: str) -> None:
        if len(self.errors) < MAX_STORED_ERRORS:
            self.errors.append(message)


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


class _Outcome(enum.StrEnum):
    downloaded = "downloaded"
    not_found = "not_found"
    invalid = "invalid"
    error = "error"


@dataclass(frozen=True, slots=True)
class _Attempt:
    outcome: _Outcome
    dest: str | None = None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Prepared:
    """What the disk says about one work item, before any provider is involved."""

    local_media: str
    present: bool = True
    existing: str | None = None
    query: SubtitleQuery | None = None


class _SourceBreaker:
    """Per-sweep circuit breaker: which sources are still worth calling."""

    def __init__(self) -> None:
        self.disabled: dict[str, str] = {}
        self._failures: dict[str, int] = {}

    def live(self, sources: list[SubtitleSource], lang: str) -> list[SubtitleSource]:
        return [s for s in sources if s.supports(lang) and s.name not in self.disabled]

    def is_live(self, source: SubtitleSource) -> bool:
        return source.name not in self.disabled

    def succeeded(self, source: SubtitleSource) -> None:
        self._failures[source.name] = 0

    def failed(self, source: SubtitleSource, detail: str, *, fatal: bool = False) -> None:
        count = self._failures.get(source.name, 0) + 1
        self._failures[source.name] = count
        if fatal or count >= MAX_SOURCE_FAILURES:
            self.disabled.setdefault(source.name, detail)
            log.warning("subtitle.source_disabled", source=source.name, detail=detail)


def _has_credentials(name: str, s: Secrets) -> bool:
    if name == "opensubtitles":
        return s.opensubtitles_api_key is not None
    if name == "opensubtitles_org":
        return bool(s.opensubtitles_org_username and s.opensubtitles_org_password)
    if name == "ktuvit":
        return bool(s.ktuvit_email and s.ktuvit_password)
    return False


def _require_sources(config: AppConfig) -> None:
    if not any(_has_credentials(n, config.secrets) for n in config.subtitles.sources):
        raise NotConfiguredError(
            "No subtitle sources configured; set OpenSubtitles/ktuvit creds "
            "in .env and subtitles.sources in config.yaml."
        )


def _require_library_path(config: AppConfig) -> None:
    """UNC catalog paths can't be written without a mount to map them onto."""

    if config.subtitles.library_base_dir:
        return
    with session_scope() as s:
        unc = s.scalar(
            select(OwnedFile.id).where(OwnedFile.path.startswith("\\\\", autoescape=True)).limit(1)
        )
    if unc is not None:
        resolve_local_media("\\\\", config)  # raises the NotConfiguredError with the fix


def _build_sources(config: AppConfig, http: httpx.AsyncClient) -> list[SubtitleSource]:
    s = config.secrets
    sub = config.subtitles
    out: list[SubtitleSource] = []
    for name in sub.sources:
        if not _has_credentials(name, s):
            log.warning("subtitle.source_unusable", source=name)
            continue
        if name == "opensubtitles":
            assert s.opensubtitles_api_key is not None
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
            assert s.opensubtitles_org_username and s.opensubtitles_org_password
            out.append(
                OpenSubtitlesOrgSource(
                    s.opensubtitles_org_username,
                    s.opensubtitles_org_password.get_secret_value(),
                    http,
                    user_agent=sub.opensubtitles_org_user_agent,
                    timeout=sub.request_timeout,
                )
            )
        else:
            assert s.ktuvit_email and s.ktuvit_password
            out.append(
                KtuvitSource(
                    s.ktuvit_email,
                    s.ktuvit_password.get_secret_value(),
                    http,
                    timeout=sub.request_timeout,
                )
            )
    return out


async def _db[T](fn: Callable[..., T], *args: Any) -> T:
    return await asyncio.wait_for(asyncio.to_thread(fn, *args), DB_TIMEOUT_S)


async def _nas[T](fn: Callable[..., T], *args: Any) -> T:
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), NAS_TIMEOUT_S)
    except TimeoutError as exc:  # the wait expired, or the OS reported ETIMEDOUT
        raise _NasTimeoutError(f"NAS operation timed out ({fn.__name__})") from exc


async def _library_ready(config: AppConfig) -> bool:
    try:
        return await _nas(library_mounted, config)
    except _NasTimeoutError:
        return False


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes; ours are all UTC."""

    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def cooldown_for(fail_count: int) -> timedelta:
    """1 day, 2, 4 ... up to the 30-day ceiling."""

    doublings = min(max(fail_count - 1, 0), 10)
    return min(COOLDOWN_BASE * (1 << doublings), COOLDOWN_MAX)


def _work_item(f: OwnedFile, t: Title, lang: str) -> _Work:
    return _Work(
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


def _collect_work(config: AppConfig, now: datetime) -> tuple[list[_Work], int, int]:
    """``(due items in order, pairs missing, pairs cooling down)``.

    Never-tried pairs come first, newest file first, then retries, longest
    waiting first.
    """

    languages = config.subtitles.languages
    missing = 0
    cooling = 0
    fresh: list[tuple[datetime, int, _Work]] = []
    retries: list[tuple[datetime, _Work]] = []
    with session_scope() as s:
        attempts = {
            (a.owned_file_id, a.lang): (_aware(a.last_tried), a.fail_count)
            for a in s.scalars(select(SubtitleAttempt))
        }
        for f, t in s.execute(
            select(OwnedFile, Title).join(Title, Title.id == OwnedFile.title_id)
        ).all():
            have = set(f.subtitle_langs or [])
            for lang in languages:
                if lang in have:
                    continue
                missing += 1
                item = _work_item(f, t, lang)
                attempt = attempts.get((f.id, lang))
                if attempt is None:
                    fresh.append((_aware(f.created_at), f.id, item))
                elif attempt[0] + cooldown_for(attempt[1]) > now:
                    cooling += 1
                else:
                    retries.append((attempt[0], item))
    fresh.sort(key=lambda row: row[:2], reverse=True)
    retries.sort(key=lambda row: row[0])
    return [w for *_, w in fresh] + [w for _, w in retries], missing, cooling


def _note_attempt(item: _Work, outcome: str) -> None:
    with session_scope() as s:
        if s.get(OwnedFile, item.owned_file_id) is None:
            return  # pruned by a scan meanwhile
        row = s.scalar(
            select(SubtitleAttempt).where(
                SubtitleAttempt.owned_file_id == item.owned_file_id,
                SubtitleAttempt.lang == item.lang,
            )
        )
        if row is None:
            row = SubtitleAttempt(owned_file_id=item.owned_file_id, lang=item.lang, fail_count=0)
            s.add(row)
        row.fail_count += 1
        row.last_tried = utcnow()
        row.last_outcome = outcome


async def _note_attempt_safely(item: _Work, outcome: str) -> None:
    """A failure to remember a failure must not stop the sweep either."""

    try:
        await _db(_note_attempt, item, outcome)
    except Exception as exc:
        log.warning("subtitle.attempt_not_recorded", title=item.title, detail=redact_exc(exc))


def _store_record(item: _Work, provider: str, dest: str) -> None:
    adopted = provider == EXISTING_PROVIDER
    with session_scope() as s:
        row = s.scalar(
            select(Subtitle).where(
                Subtitle.owned_file_id == item.owned_file_id, Subtitle.lang == item.lang
            )
        )
        if row is None:
            s.add(
                Subtitle(
                    title_id=item.title_id,
                    owned_file_id=item.owned_file_id,
                    lang=item.lang,
                    provider=provider,
                    path=dest,
                    status="adopted" if adopted else "downloaded",
                )
            )
        elif not adopted:  # adopting keeps the row's history of where the file came from
            row.provider = provider
            row.path = dest
            row.status = "downloaded"
        else:
            row.path = dest
        s.execute(
            delete(SubtitleAttempt).where(
                SubtitleAttempt.owned_file_id == item.owned_file_id,
                SubtitleAttempt.lang == item.lang,
            )
        )
        f = s.get(OwnedFile, item.owned_file_id)
        if f is not None:
            # Reassign a new list so SQLAlchemy flags the JSON column dirty.
            f.subtitle_langs = sorted({*(f.subtitle_langs or []), item.lang})


def _record(item: _Work, provider: str, dest: str) -> None:
    try:
        _store_record(item, provider, dest)
    except IntegrityError:  # an import fetched the same pair at the same moment
        _store_record(item, provider, dest)


def _inspect_media(
    local_media: str, item: _Work, subs_folder: str
) -> tuple[bool, str | None, str | None, int | None]:
    """All the NAS reads for one item: (present, adoptable sub, moviehash, size)."""

    if not media_present(local_media):
        return False, None, None, None
    existing = find_valid_subtitle(local_media, item.lang, subs_folder)
    if existing is not None:
        return True, existing, None, None
    return True, None, opensubtitles_hash(local_media), os.path.getsize(local_media)


def _search_query(
    item: _Work, local_media: str, moviehash: str | None, size: int | None
) -> SubtitleQuery:
    return SubtitleQuery(
        lang=item.lang,
        kind=item.kind,
        title=item.title,
        year=item.year,
        imdb_id=item.imdb_id,
        release_name=os.path.splitext(os.path.basename(local_media))[0],
        season=item.season,
        episode=item.episode,
        moviehash=moviehash,
        filesize=size,
    )


async def _prepare(config: AppConfig, item: _Work) -> _Prepared:
    """Everything decidable from the NAS alone, so no provider is spent on it."""

    local_media = resolve_local_media(item.media_path, config)
    present, existing, moviehash, size = await _nas(
        _inspect_media, local_media, item, config.organizer.subs_folder
    )
    if not present:
        return _Prepared(local_media, present=False)
    if existing is not None:
        return _Prepared(local_media, existing=existing)
    return _Prepared(local_media, query=_search_query(item, local_media, moviehash, size))


async def _download_valid(
    source: SubtitleSource, candidate: SubtitleResult, lang: str
) -> ValidatedSubtitle:
    raw = await asyncio.wait_for(source.download(candidate), PROVIDER_TIMEOUT_S)
    return await asyncio.to_thread(validate_subtitle, raw, lang)


async def _rejections(item: _Work) -> Rejections:
    """Best effort: losing this read must not stop subtitles being fetched."""

    try:
        return await _db(load_rejections, item.owned_file_id, item.lang)
    except Exception as exc:
        log.warning("subtitle.rejects_unread", title=item.title, detail=redact_exc(exc))
        return Rejections()


async def _note_choice(item: _Work, chosen: SubtitleResult) -> None:
    """Provenance for the timing check, which works without it: a subtitle it knows
    nothing about just gets checked."""

    try:
        await _db(
            record_choice,
            item.owned_file_id,
            item.lang,
            chosen.source,
            chosen.name,
            result_key(chosen),
            chosen.hash_match,
        )
    except Exception as exc:
        log.warning("subtitle.choice_not_recorded", title=item.title, detail=redact_exc(exc))


async def _search_and_place(
    config: AppConfig,
    sources: list[SubtitleSource],
    breaker: _SourceBreaker,
    item: _Work,
    prepared: _Prepared,
) -> _Attempt:
    """Walk the sources in order. Provider trouble moves on to the next source; a
    NAS or database failure while storing the subtitle propagates to the caller."""

    query = prepared.query
    assert query is not None
    errors: list[str] = []
    answered = False  # some source gave a definite answer
    saw_invalid = False
    rejected = await _rejections(item)
    for source in sources:
        if not breaker.is_live(source):
            continue
        try:
            results = await asyncio.wait_for(source.search(query), PROVIDER_TIMEOUT_S)
            ranked = await asyncio.to_thread(
                rank_results,
                results,
                query.release_name,
                hearing_impaired=config.subtitles.hearing_impaired,
            )
            allowed = [r for r in ranked if result_key(r) not in rejected.keys]
            valid: ValidatedSubtitle | None = None
            chosen: SubtitleResult | None = None
            invalid_here = False
            for candidate in allowed[:MAX_CANDIDATES_PER_SOURCE]:
                try:
                    fetched = await _download_valid(source, candidate, item.lang)
                    if content_hash(fetched.data) in rejected.hashes:
                        continue
                    valid, chosen = fetched, candidate
                    break
                except InvalidSubtitleError as exc:
                    invalid_here = True
                    log.warning(
                        "subtitle.invalid_payload",
                        source=source.name,
                        title=item.title,
                        lang=item.lang,
                        detail=redact_exc(exc),
                    )
            answered = True
        except Exception as exc:
            # A dead provider / exhausted quota must not abort the fallback chain
            # (e.g. OpenSubtitles.com 401 while ktuvit could serve it).
            detail = redact_exc(exc) or type(exc).__name__
            log.warning("subtitle.source_error", source=source.name, lang=item.lang, detail=detail)
            spent = isinstance(exc, SourceQuotaError)
            breaker.failed(source, detail, fatal=spent or isinstance(exc, SourceAuthError))
            if not spent:
                errors.append(f"{source.name}: {detail}")
            continue
        if valid is None:
            if invalid_here:
                saw_invalid = True
                breaker.failed(source, "served only invalid subtitles")
            else:
                breaker.succeeded(source)
            continue
        breaker.succeeded(source)
        dest = subtitle_dest(
            prepared.local_media, item.lang, config.organizer.subs_folder, valid.ext
        )
        await _nas(write_subtitle, dest, valid.data)
        await _db(_record, item, source.name, dest)
        if config.subtitles.sync.enabled and chosen is not None:
            await _note_choice(item, chosen)
        log.info(
            "subtitle.downloaded",
            title=item.title,
            lang=item.lang,
            source=source.name,
            dest=dest,
        )
        return _Attempt(_Outcome.downloaded, dest=dest, errors=tuple(errors))
    if saw_invalid:
        return _Attempt(_Outcome.invalid, errors=tuple(errors))
    if answered:
        return _Attempt(_Outcome.not_found, errors=tuple(errors))
    return _Attempt(_Outcome.error, errors=tuple(errors))


def _works_for_file(config: AppConfig, owned_file_id: int) -> list[_Work]:
    with session_scope() as s:
        f = s.get(OwnedFile, owned_file_id)
        if f is None:
            return []
        title = s.get(Title, f.title_id)
        if title is None:
            return []
        have = set(f.subtitle_langs or [])
        return [_work_item(f, title, x) for x in config.subtitles.languages if x not in have]


async def fetch_for_owned_file(config: AppConfig, owned_file_id: int) -> list[str]:
    """Fetch missing target-language subtitles for one owned file (used right after
    a torrent import so the pipeline completes without waiting for a full sweep).

    Best-effort. A freshly imported file ignores cooldowns, but failures are noted
    so the sweep doesn't immediately repeat them."""

    works = await _db(_works_for_file, config, owned_file_id)
    if not works:
        return []
    if not await _library_ready(config):
        log.warning("subtitle.library_unmounted", base=config.subtitles.library_base_dir)
        return []

    out: list[str] = []
    breaker = _SourceBreaker()
    async with httpx.AsyncClient(timeout=config.subtitles.request_timeout) as http:
        sources = _build_sources(config, http)
        for w in works:
            live = breaker.live(sources, w.lang)
            if not live:
                continue
            try:
                prepared = await _prepare(config, w)
                if prepared.existing is not None:
                    await _db(_record, w, EXISTING_PROVIDER, prepared.existing)
                    out.append(prepared.existing)
                elif prepared.present:
                    attempt = await _search_and_place(config, live, breaker, w, prepared)
                    if attempt.dest is not None:
                        out.append(attempt.dest)
                    else:
                        await _note_attempt_safely(w, attempt.outcome)
            except Exception as exc:
                log.warning(
                    "subtitle.fetch_failed", title=w.title, lang=w.lang, detail=redact_exc(exc)
                )
                await _note_attempt_safely(w, _Outcome.error)
    return out


def _tally(stats: NativeSweepStats, item: _Work, attempt: _Attempt) -> None:
    if attempt.outcome is _Outcome.downloaded:
        stats.downloaded += 1
    elif attempt.outcome is _Outcome.not_found:
        stats.not_found += 1
    elif attempt.outcome is _Outcome.invalid:
        stats.invalid += 1
    else:
        stats.failed += 1
    for error in attempt.errors:
        stats.add_error(f"{item.title} [{item.lang}]: {error}")


async def _process_queue(
    config: AppConfig,
    sources: list[SubtitleSource],
    queue: list[_Work],
    stats: NativeSweepStats,
) -> None:
    budget = config.subtitles.max_searches_per_sweep
    breaker = _SourceBreaker()
    attempted = 0
    nas_timeouts = 0
    try:
        for position, item in enumerate(queue):
            # Cap on searches *attempted*, not just successful downloads — otherwise
            # titles with no available subs burn quota unbounded.
            if attempted >= budget:
                stats.capped = len(queue) - position
                log.info("subtitle.capped", skipped=stats.capped, budget=budget)
                break
            if item.kind is TitleKind.series and (item.season is None or item.episode is None):
                stats.unidentified += 1
                continue
            # The NAS throws EIO when work overlaps an import. Bounded: a long copy
            # slows the sweep down, it doesn't stop it.
            await wait_for_import_gap(config, SUBTITLE_IMPORT_WAIT_S)
            live = breaker.live(sources, item.lang)
            if not live:
                if any(s.supports(item.lang) for s in sources):
                    stats.source_down += 1
                else:
                    stats.unsupported += 1
                continue
            try:
                prepared = await _prepare(config, item)
                if not prepared.present:
                    # With the mount gone every file looks absent.
                    if not await _library_ready(config):
                        raise LibraryUnavailableError("library mount lost during the sweep")
                    stats.absent += 1
                    continue
                if prepared.existing is not None:
                    await _db(_record, item, EXISTING_PROVIDER, prepared.existing)
                    stats.adopted += 1
                    continue
                attempted += 1
                attempt = await _search_and_place(config, live, breaker, item, prepared)
            except LibraryUnavailableError:
                raise
            except _NasTimeoutError as exc:
                nas_timeouts += 1
                stats.failed += 1
                stats.add_error(f"{item.title} [{item.lang}]: {redact_exc(exc)}")
                await _note_attempt_safely(item, _Outcome.error)
                if nas_timeouts >= MAX_NAS_TIMEOUTS:
                    raise LibraryUnavailableError(
                        f"NAS stopped responding ({nas_timeouts} timeouts in a row)"
                    ) from exc
                continue
            except Exception as exc:
                # One poisoned item (a busy file on the NAS, say) costs that item
                # its turn, never the rest of the sweep.
                stats.failed += 1
                detail = redact_exc(exc) or type(exc).__name__
                stats.add_error(f"{item.title} [{item.lang}]: {detail}")
                await _note_attempt_safely(item, _Outcome.error)
                continue
            nas_timeouts = 0
            _tally(stats, item, attempt)
            if attempt.dest is None:
                await _note_attempt_safely(item, attempt.outcome)
    finally:
        stats.disabled_sources = dict(breaker.disabled)


def _final_status(stats: NativeSweepStats, *, aborted: bool = False) -> RunStatus:
    """``success`` only if nothing went wrong; ``failed`` if nothing was achieved.

    A provider saying "no subtitle" is a clean result, however often. A spent quota
    is routine, so it only counts once it leaves work with no source at all.
    """

    troubled = aborted or stats.failed or stats.source_down or stats.errors
    if not troubled:
        return RunStatus.success
    achieved = stats.downloaded + stats.adopted + stats.not_found + stats.invalid
    return RunStatus.partial if achieved else RunStatus.failed


async def sweep_native(
    config: AppConfig, *, on_started: Callable[[], None] | None = None
) -> NativeSweepStats:
    """Fetch missing target-language subtitles for owned files (capped).

    Raises NotConfiguredError before anything is recorded when the sweep can't run
    at all; ``on_started`` fires once those checks have passed.
    """

    _require_sources(config)
    await _db(_require_library_path, config)
    if on_started is not None:
        on_started()

    run_id = await start_run()
    bind_run(run_id=run_id, job="subtitle")
    stats = NativeSweepStats()
    status = RunStatus.failed  # only a sweep that got to the end earns better
    try:
        if not await _library_ready(config):
            raise LibraryUnavailableError(
                f"subtitles.library_base_dir {config.subtitles.library_base_dir!r} is not "
                "mounted; nothing was searched."
            )
        async with httpx.AsyncClient(timeout=config.subtitles.request_timeout) as http:
            sources = _build_sources(config, http)
            queue, stats.considered, stats.cooling_down = await _db(_collect_work, config, utcnow())
            await _process_queue(config, sources, queue, stats)
        status = _final_status(stats)
        log.info("subtitle.done", status=status.value, **stats.as_dict())
    except asyncio.CancelledError:
        stats.add_error("sweep cancelled or timed out before finishing")
        raise
    except Exception as exc:
        status = _final_status(stats, aborted=True)
        stats.add_error(redact_exc(exc) or type(exc).__name__)
        log.error("subtitle.failed", error=redact_exc(exc))
    finally:
        try:
            await finish_run(run_id, status, stats.as_dict())
        finally:
            clear_run()

    if status is RunStatus.failed:
        raise RuntimeError(f"Native subtitle sweep failed: {stats.errors[:MAX_ERRORS_SHOWN]}")
    return stats
