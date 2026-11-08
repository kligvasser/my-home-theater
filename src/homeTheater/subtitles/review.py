"""The per-file subtitle view: what was chosen, and approve, swap or revert.

Candidates are searched again at swap time instead of trusting anything the browser
sends back: a provider's ``ref`` can hold a download URL, so the browser only ever
holds an opaque key. The first subtitle a change replaces is kept as ``<name>.orig``,
so a revert also undoes a swap.
"""

from __future__ import annotations

import asyncio
import ntpath
import os
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import and_, delete, or_, select

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import OwnedFile, Subtitle, SubtitleReject, SubtitleSync, Title
from ..db.session import session_scope
from ..errors import JobBusyError, redact_exc
from ..locks import is_import_active, job_lock
from ..logging_setup import get_logger
from .choices import (
    STATE_APPROVED,
    STATE_CORRECTED,
    STATE_FAILED,
    STATE_REVERTED,
    Rejections,
    content_hash,
    load_rejections,
    record_choice,
    result_key,
)
from .native.base import InvalidSubtitleError, SubtitleResult, SubtitleSource, opensubtitles_hash
from .native.payload import MAX_DECOMPRESSED_BYTES, validate_subtitle
from .native.placement import (
    _remove_quietly,
    media_present,
    resolve_local_media,
    subtitle_dest,
    write_subtitle,
)
from .native.ranking import rank_results
from .native.service import (
    PROVIDER_TIMEOUT_S,
    _build_sources,
    _db,
    _library_ready,
    _nas,
    _require_sources,
    _search_query,
    _Work,
    _work_item,
)
from .sync import ORIGINAL_SUFFIX, SYNC_LOCK, original_path_for

log = get_logger(__name__)

ATTENTION_STATES = (STATE_CORRECTED, STATE_FAILED)
MAX_CANDIDATES = 20


class ReviewNotFoundError(LookupError):
    """No such file, or no subtitle on it in that language."""


class ReviewConflictError(RuntimeError):
    """The request can't be done as things stand; the message says why."""


@dataclass(frozen=True, slots=True)
class _Target:
    work: _Work
    subtitle_path: str | None
    state: str | None
    original_path: str | None
    result_key: str | None
    release: str | None


def _file_name(path: str) -> str:
    return ntpath.basename(path)  # handles UNC catalog paths and local ones


def list_review(
    *, lang: str | None = None, state: str | None = None, q: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Subtitles with their timing state. ``state`` "all" or one state; the default is
    what wants a look."""

    query = (
        select(Subtitle, OwnedFile, Title, SubtitleSync)
        .join(OwnedFile, OwnedFile.id == Subtitle.owned_file_id)
        .join(Title, Title.id == OwnedFile.title_id)
        .outerjoin(
            SubtitleSync,
            and_(
                SubtitleSync.owned_file_id == Subtitle.owned_file_id,
                SubtitleSync.lang == Subtitle.lang,
            ),
        )
        .order_by(Subtitle.updated_at.desc(), Subtitle.id.desc())
        .limit(limit)
    )
    if lang:
        query = query.where(Subtitle.lang == lang)
    if q:
        query = query.where(Title.title.icontains(q, autoescape=True))
    if state is None:
        query = query.where(SubtitleSync.state.in_(ATTENTION_STATES))
    elif state != "all":
        query = query.where(SubtitleSync.state == state)
    with session_scope() as s:
        return [
            {
                "owned_file_id": owned.id,
                "lang": sub.lang,
                "title": title.title,
                "season": owned.season,
                "episode": owned.episode,
                "file": _file_name(owned.path),
                "source": (sync.source if sync else None) or sub.provider,
                "release": sync.release if sync else None,
                "hash_match": bool(sync and sync.hash_match),
                "state": sync.state if sync else "unchecked",
                "offset_seconds": sync.offset_seconds if sync else None,
                "can_revert": bool(sync and sync.original_path),
                "detail": sync.detail if sync else None,
            }
            for sub, owned, title, sync in s.execute(query).all()
        ]


def _load_target(owned_file_id: int, lang: str) -> _Target:
    with session_scope() as s:
        row = s.execute(
            select(Subtitle, OwnedFile, Title)
            .join(OwnedFile, OwnedFile.id == Subtitle.owned_file_id)
            .join(Title, Title.id == OwnedFile.title_id)
            .where(Subtitle.owned_file_id == owned_file_id, Subtitle.lang == lang)
        ).first()
        if row is None:
            raise ReviewNotFoundError(f"no {lang} subtitle on file {owned_file_id}")
        sub, owned, title = row
        sync = s.scalar(
            select(SubtitleSync).where(
                SubtitleSync.owned_file_id == owned_file_id, SubtitleSync.lang == lang
            )
        )
        return _Target(
            work=_work_item(owned, title, lang),
            subtitle_path=sub.path,
            state=sync.state if sync else None,
            original_path=sync.original_path if sync else None,
            result_key=sync.result_key if sync else None,
            release=sync.release if sync else None,
        )


def approve(config: AppConfig, owned_file_id: int, lang: str) -> None:
    """Keep what's there. It won't be checked again."""

    with job_lock(config, SYNC_LOCK), session_scope() as s:
        sub = s.scalar(
            select(Subtitle).where(Subtitle.owned_file_id == owned_file_id, Subtitle.lang == lang)
        )
        if sub is None:
            raise ReviewNotFoundError(f"no {lang} subtitle on file {owned_file_id}")
        row = s.scalar(
            select(SubtitleSync).where(
                SubtitleSync.owned_file_id == owned_file_id, SubtitleSync.lang == lang
            )
        )
        if row is None:
            row = SubtitleSync(
                owned_file_id=owned_file_id, lang=lang, source=sub.provider, fail_count=0
            )
            s.add(row)
        row.state = STATE_APPROVED
        row.detail = None
        row.next_check_at = None


async def _ready_for_nas_write(config: AppConfig) -> None:
    if is_import_active(config):
        raise JobBusyError("An import is copying to the NAS; try again when it has finished.")
    if not await _library_ready(config):
        raise ReviewConflictError("The library mount is unavailable.")


def _file_facts(local_media: str) -> tuple[bool, str | None, int | None]:
    if not media_present(local_media):
        return False, None, None
    return True, opensubtitles_hash(local_media), os.path.getsize(local_media)


async def _search_all(
    config: AppConfig, http: httpx.AsyncClient, target: _Target
) -> list[tuple[SubtitleSource, SubtitleResult]]:
    """Every source's results for this file, best first, leaving out rejected ones."""

    work = target.work
    if work.kind.value == "series" and (work.season is None or work.episode is None):
        raise ReviewConflictError("This file has no season/episode to search by.")
    local_media = resolve_local_media(work.media_path, config)
    present, moviehash, size = await _nas(_file_facts, local_media)
    if not present:
        raise ReviewConflictError("The media file is missing.")
    query = _search_query(work, local_media, moviehash, size)
    rejected = await _db(load_rejections, work.owned_file_id, work.lang)
    found: list[SubtitleResult] = []
    owner: dict[str, SubtitleSource] = {}
    for source in _build_sources(config, http):
        if not source.supports(work.lang):
            continue
        try:
            results = await asyncio.wait_for(source.search(query), PROVIDER_TIMEOUT_S)
        except Exception as exc:  # the other sources may still answer
            log.warning("subtitle.review_search_failed", source=source.name, detail=redact_exc(exc))
            continue
        owner[source.name] = source
        found += [r for r in results if result_key(r) not in rejected.keys]
    ranked = await asyncio.to_thread(
        rank_results,
        found,
        query.release_name,
        hearing_impaired=config.subtitles.hearing_impaired,
    )
    return [(owner[r.source], r) for r in ranked if r.source in owner]


async def list_candidates(config: AppConfig, owned_file_id: int, lang: str) -> list[dict[str, Any]]:
    _require_sources(config)
    target = await _db(_load_target, owned_file_id, lang)
    async with httpx.AsyncClient(timeout=config.subtitles.request_timeout) as http:
        ranked = await _search_all(config, http, target)
    return [
        {
            "id": result_key(r),
            "source": r.source,
            "name": r.name,
            "hash_match": r.hash_match,
            "hearing_impaired": r.hearing_impaired,
            "current": result_key(r) == target.result_key,
        }
        for _, r in ranked[:MAX_CANDIDATES]
    ]


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read(MAX_DECOMPRESSED_BYTES + 1)


def _current_hashes(target: _Target, current: str | None, lang: str) -> list[str]:
    """Content hashes that identify the subtitle being thrown out. A corrected file
    no longer matches what the provider serves; its original does."""

    paths = [current] if current else []
    if current and target.state == STATE_CORRECTED:
        paths.append(target.original_path or original_path_for(current))
    hashes: list[str] = []
    for path in paths:
        try:
            hashes.append(content_hash(validate_subtitle(_read(path), lang).data))
        except (OSError, InvalidSubtitleError):
            continue
    return hashes


def _place_swap(current: str | None, dest: str, data: bytes) -> str | None:
    """Replace the subtitle, keeping the first one. Returns the backup, if made now."""

    made: str | None = None
    if current is not None and os.path.exists(current):
        backup = original_path_for(current)
        if not os.path.exists(backup):
            write_subtitle(backup, _read(current))
            made = backup
    write_subtitle(dest, data)
    if current is not None and current != dest:
        _remove_quietly(current)  # a different format: don't leave two for one language
    return made


def _store_swap(
    target: _Target,
    chosen: SubtitleResult,
    dest: str,
    backup: str | None,
    hashes: list[str],
) -> None:
    work = target.work
    file_id, lang = work.owned_file_id, work.lang
    now = utcnow()
    with session_scope() as s:
        digests: list[str | None] = [*hashes] or [None]
        for digest in digests:
            s.add(
                SubtitleReject(
                    owned_file_id=file_id,
                    lang=lang,
                    result_key=target.result_key,
                    content_sha256=digest,
                    label=target.release,
                    rejected_at=now,
                )
            )
        row = s.scalar(
            select(SubtitleSync).where(
                SubtitleSync.owned_file_id == file_id, SubtitleSync.lang == lang
            )
        )
        sub = s.scalar(
            select(Subtitle).where(Subtitle.owned_file_id == file_id, Subtitle.lang == lang)
        )
        if backup is not None:
            if row is None:
                row = SubtitleSync(owned_file_id=file_id, lang=lang, fail_count=0)
                s.add(row)
            if row.original_path is None:
                row.original_path = backup
                row.original_source = row.source or (sub.provider if sub else None)
                row.original_release = row.release
                row.original_key = row.result_key
        if sub is not None:
            sub.provider = chosen.source
            sub.path = dest
            sub.status = "downloaded"
    record_choice(file_id, lang, chosen.source, chosen.name, result_key(chosen), chosen.hash_match)


async def swap(
    config: AppConfig, owned_file_id: int, lang: str, candidate_id: str
) -> dict[str, Any]:
    """Reject the current subtitle and put the chosen candidate in its place."""

    _require_sources(config)
    with job_lock(config, SYNC_LOCK):
        target = await _db(_load_target, owned_file_id, lang)
        await _ready_for_nas_write(config)
        work = target.work
        local_media = resolve_local_media(work.media_path, config)
        current = (
            resolve_local_media(target.subtitle_path, config) if target.subtitle_path else None
        )
        async with httpx.AsyncClient(timeout=config.subtitles.request_timeout) as http:
            ranked = await _search_all(config, http, target)
            match = next(((src, r) for src, r in ranked if result_key(r) == candidate_id), None)
            if match is None:
                raise ReviewConflictError("That subtitle is no longer on offer; search again.")
            source, chosen = match
            raw = await asyncio.wait_for(source.download(chosen), PROVIDER_TIMEOUT_S)
        try:
            valid = await asyncio.to_thread(validate_subtitle, raw, lang)
        except InvalidSubtitleError as exc:
            raise ReviewConflictError(f"That subtitle is unusable: {redact_exc(exc)}") from exc
        rejected: Rejections = await _db(load_rejections, owned_file_id, lang)
        if content_hash(valid.data) in rejected.hashes:
            raise ReviewConflictError("That is a subtitle you rejected, under a new id.")
        hashes = await _nas(_current_hashes, target, current, lang)
        if content_hash(valid.data) in hashes:
            raise ReviewConflictError("That is the subtitle already in place.")
        dest = subtitle_dest(local_media, lang, config.organizer.subs_folder, valid.ext)
        backup = await _nas(_place_swap, current, dest, valid.data)
        await _db(_store_swap, target, chosen, dest, backup, hashes)
    log.info("subtitle.swapped", title=work.title, lang=lang, source=chosen.source)
    return {"source": chosen.source, "name": chosen.name}


def _restore(original: str, current: str | None, lang: str) -> tuple[str, str]:
    """Put the backup back in place. Returns (restored path, its content hash)."""

    try:
        data = _read(original)
    except FileNotFoundError:
        raise ReviewConflictError("The original subtitle is gone from the NAS.") from None
    try:
        valid = validate_subtitle(data, lang)
    except InvalidSubtitleError as exc:
        raise ReviewConflictError(f"The original is unusable: {redact_exc(exc)}") from exc
    restored = original.removesuffix(ORIGINAL_SUFFIX)
    write_subtitle(restored, data)
    if current is not None and current != restored:
        _remove_quietly(current)
    return restored, content_hash(valid.data)


def _store_revert(file_id: int, lang: str, restored: str, digest: str) -> None:
    with session_scope() as s:
        row = s.scalar(
            select(SubtitleSync).where(
                SubtitleSync.owned_file_id == file_id, SubtitleSync.lang == lang
            )
        )
        sub = s.scalar(
            select(Subtitle).where(Subtitle.owned_file_id == file_id, Subtitle.lang == lang)
        )
        key = row.original_key if row else None
        # Going back to a subtitle is the owner taking its rejection back.
        s.execute(
            delete(SubtitleReject).where(
                SubtitleReject.owned_file_id == file_id,
                SubtitleReject.lang == lang,
                or_(
                    SubtitleReject.content_sha256 == digest,
                    and_(SubtitleReject.result_key.is_not(None), SubtitleReject.result_key == key),
                ),
            )
        )
        if sub is not None:
            sub.path = restored
            if row is not None and row.original_source:
                sub.provider = row.original_source
        if row is not None:
            row.state = STATE_REVERTED
            row.source = row.original_source
            row.release = row.original_release
            row.result_key = row.original_key
            row.offset_seconds = None
            row.detail = None
            row.next_check_at = None


async def revert(config: AppConfig, owned_file_id: int, lang: str) -> None:
    """Back to the original. The backup stays, and the timing check leaves it alone."""

    with job_lock(config, SYNC_LOCK):
        target = await _db(_load_target, owned_file_id, lang)
        if target.original_path is None or target.subtitle_path is None:
            raise ReviewConflictError("There is no original to go back to.")
        await _ready_for_nas_write(config)
        original = resolve_local_media(target.original_path, config)
        current = resolve_local_media(target.subtitle_path, config)
        restored, digest = await _nas(_restore, original, current, lang)
        await _db(_store_revert, owned_file_id, lang, restored, digest)
