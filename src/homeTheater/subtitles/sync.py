"""Subtitle timing check: run ffsubsync over subtitles chosen without a hash match.

ffsubsync and ffmpeg are optional programs found on PATH. Without them the check is a
skip with a reason, never a failure. ffsubsync reads the whole film over the NAS, so:
one file at a time (``job_lock``), in a subprocess with a hard wall-clock limit, in a
worker thread, and never next to an import: the NAS throws EIO when work overlaps one,
so a run stops when an import starts and picks the file up next time.

Nothing reaches the library until it has passed: ffsubsync works on a temporary copy,
its output must pass ``validate_subtitle`` and keep every cue, and the shift is
measured from the two files rather than read from ffsubsync's log. Only then is the
original kept beside the subtitle as ``<name>.orig`` (never overwritten, so it stays
the true original) and the corrected file put in its place. Neither the scanner nor
Kodi reads the ``.orig`` extension as a subtitle.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import statistics
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, select

from ..config import AppConfig
from ..db.base import utcnow
from ..db.models import OwnedFile, Subtitle, SubtitleSync, Title
from ..db.session import session_scope
from ..errors import NotConfiguredError, redact_exc
from ..locks import is_import_active, job_lock
from ..logging_setup import get_logger
from .choices import (
    STATE_CORRECTED,
    STATE_FAILED,
    STATE_IN_SYNC,
    STATE_PENDING,
)
from .native.base import InvalidSubtitleError
from .native.payload import MAX_DECOMPRESSED_BYTES, validate_subtitle
from .native.placement import media_present, resolve_local_media, write_subtitle
from .native.service import _aware, _db, _library_ready, _nas, _NasTimeoutError, cooldown_for

log = get_logger(__name__)

SYNC_LOCK = "subtitle-sync"
ORIGINAL_SUFFIX = ".orig"
FFSUBSYNC = "ffsubsync"
FFMPEG = "ffmpeg"
POLL_S = 2.0  # how often a running ffsubsync is checked on
KILL_WAIT_S = 5.0
MAX_STORED_ERRORS = 20
_CHECKABLE = (STATE_PENDING, STATE_FAILED)

_SRT_START_RE = re.compile(r"^\s*(\d{1,3}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->", re.MULTILINE)
_ASS_START_RE = re.compile(
    r"^\s*Dialogue\s*:\s*[^,]*,\s*(\d+):(\d{2}):(\d{2})[.:](\d{1,3})\s*,",
    re.MULTILINE | re.IGNORECASE,
)


class _SyncFailedError(RuntimeError):
    """This file couldn't be checked. Its subtitle stays as it is."""


class _DeferredError(RuntimeError):
    """An import started; this file waits for the next run."""


class _StuckError(RuntimeError):
    """ffsubsync ignored SIGKILL (blocked on the NAS): nothing more should start."""


@dataclass
class SyncStats:
    considered: int = 0
    checked: int = 0
    in_sync: int = 0
    corrected: int = 0
    failed: int = 0
    capped: int = 0
    deferred: bool = False  # an import stopped the run early
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add_error(self, message: str) -> None:
        if len(self.errors) < MAX_STORED_ERRORS:
            self.errors.append(message)


@dataclass(frozen=True, slots=True)
class _Item:
    owned_file_id: int
    lang: str
    title: str
    media_path: str
    subtitle_path: str


def missing_tools() -> list[str]:
    return [tool for tool in (FFSUBSYNC, FFMPEG) if shutil.which(tool) is None]


def unavailable_reason(config: AppConfig) -> str | None:
    """Why the check can't run, in words for Status and the Subtitles page."""

    if not config.subtitles.sync.enabled:
        return "subtitles.sync.enabled is off"
    if config.subtitles.backend != "native":
        return "the timing check needs subtitles.backend: native"
    missing = missing_tools()
    if missing:
        return f"{' and '.join(missing)} not found on PATH; timing check skipped"
    return None


def original_path_for(subtitle_path: str) -> str:
    return subtitle_path + ORIGINAL_SUFFIX


def _seconds(match: re.Match[str]) -> float:
    hours, minutes, seconds, fraction = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + float(f"0.{fraction}")


def cue_starts(text: str) -> list[float]:
    found = [_seconds(m) for m in _SRT_START_RE.finditer(text)]
    return found or [_seconds(m) for m in _ASS_START_RE.finditer(text)]


def measure_shift(before: bytes, after: bytes) -> tuple[float, float]:
    """(median shift, spread) of the cues, in seconds. Spread is the drift a constant
    offset can't explain, as from a frame-rate mismatch."""

    old = cue_starts(before.decode("utf-8"))
    new = cue_starts(after.decode("utf-8"))
    if not old or len(old) != len(new):
        raise _SyncFailedError(f"retimed subtitle has {len(new)} cues, expected {len(old)}")
    shifts = [b - a for a, b in zip(old, new, strict=True)]
    return statistics.median(shifts), max(shifts) - min(shifts)


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill ffsubsync and the ffmpeg it started: they share a session."""

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        raise _StuckError("ffsubsync would not die") from None


def _run_ffsubsync(
    config: AppConfig, media: str, source: str, output: str, cancel: threading.Event
) -> None:
    """Run ffsubsync to completion, or kill it. Output is discarded, not parsed."""

    limit = config.subtitles.sync.timeout_minutes * 60
    deadline = time.time() + limit  # wall clock: a sleeping Mac stops the monotonic one
    proc = subprocess.Popen(  # noqa: S603 - fixed program, list argv, no shell
        [FFSUBSYNC, media, "-i", source, "-o", output],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    while True:
        try:
            code = proc.wait(timeout=POLL_S)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel.is_set():
            _kill(proc)
            raise _DeferredError("cancelled")
        if is_import_active(config):
            _kill(proc)
            raise _DeferredError("an import started")
        if time.time() >= deadline:
            _kill(proc)
            raise _SyncFailedError(f"ffsubsync timed out after {limit // 60} min")
    if code != 0:
        raise _SyncFailedError(f"ffsubsync exited with status {code}")


def _retime(config: AppConfig, media: str, data: bytes, ext: str, cancel: threading.Event) -> bytes:
    """ffsubsync on a temporary copy; the corrected bytes, unvalidated."""

    with tempfile.TemporaryDirectory(prefix="ht-subsync-") as tmp:
        source = os.path.join(tmp, f"input{ext}")
        output = os.path.join(tmp, f"output{ext}")
        with open(source, "wb") as f:
            f.write(data)
        _run_ffsubsync(config, media, source, output, cancel)
        try:
            with open(output, "rb") as f:
                out = f.read(MAX_DECOMPRESSED_BYTES + 1)
        except FileNotFoundError:
            raise _SyncFailedError("ffsubsync wrote no output") from None
    if len(out) > MAX_DECOMPRESSED_BYTES:
        raise _SyncFailedError("ffsubsync output is too large")
    return out


def _read_inputs(media: str, subtitle: str) -> bytes:
    if not media_present(media):
        raise _SyncFailedError("media file is missing")
    try:
        with open(subtitle, "rb") as f:
            raw = f.read(MAX_DECOMPRESSED_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError):
        raise _SyncFailedError("subtitle file is missing") from None
    if len(raw) > MAX_DECOMPRESSED_BYTES:
        raise _SyncFailedError("subtitle file is too large")
    return raw


def _store_corrected(subtitle: str, original: bytes, corrected: bytes) -> str:
    """Backup first: if that fails, the subtitle is untouched."""

    backup = original_path_for(subtitle)
    if not os.path.exists(backup):
        write_subtitle(backup, original)
    write_subtitle(subtitle, corrected)
    return backup


def _collect(now: datetime) -> list[_Item]:
    """Due items, newest subtitle first. No row means nothing is known about how the
    subtitle was chosen, so it is checked as well."""

    out: list[_Item] = []
    with session_scope() as s:
        rows = s.execute(
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
            .where(Subtitle.path.is_not(None))
            .order_by(Subtitle.updated_at.desc(), Subtitle.id.desc())
        ).all()
        for sub, owned, title, sync in rows:
            if sync is not None:
                if sync.state not in _CHECKABLE:
                    continue
                if sync.next_check_at is not None and _aware(sync.next_check_at) > now:
                    continue
            assert sub.path is not None
            out.append(_Item(owned.id, sub.lang, title.title, owned.path, sub.path))
    return out


def _mark(
    item: _Item,
    state: str,
    *,
    offset: float | None = None,
    detail: str | None = None,
    backup: str | None = None,
) -> None:
    with session_scope() as s:
        if s.get(OwnedFile, item.owned_file_id) is None:
            return  # pruned by a scan meanwhile
        row = s.scalar(
            select(SubtitleSync).where(
                SubtitleSync.owned_file_id == item.owned_file_id, SubtitleSync.lang == item.lang
            )
        )
        if row is None:
            sub = s.scalar(
                select(Subtitle).where(
                    Subtitle.owned_file_id == item.owned_file_id, Subtitle.lang == item.lang
                )
            )
            row = SubtitleSync(
                owned_file_id=item.owned_file_id,
                lang=item.lang,
                source=sub.provider if sub else None,
                fail_count=0,
            )
            s.add(row)
        now = utcnow()
        row.state = state
        row.detail = detail
        row.checked_at = now
        if offset is not None:
            row.offset_seconds = offset
        if state == STATE_FAILED:
            row.fail_count += 1
            row.next_check_at = now + cooldown_for(row.fail_count)
        else:
            row.fail_count = 0
            row.next_check_at = None
        if backup is not None and row.original_path is None:
            row.original_path = backup
            row.original_source = row.source
            row.original_release = row.release
            row.original_key = row.result_key


async def _check_item(
    config: AppConfig, item: _Item, stats: SyncStats, cancel: threading.Event
) -> None:
    media = resolve_local_media(item.media_path, config)
    subtitle = resolve_local_media(item.subtitle_path, config)
    settings = config.subtitles.sync
    raw = await _nas(_read_inputs, media, subtitle)
    try:
        current = await asyncio.to_thread(validate_subtitle, raw, item.lang)
    except InvalidSubtitleError as exc:
        raise _SyncFailedError(f"current subtitle is unusable: {redact_exc(exc)}") from exc
    try:
        out = await asyncio.to_thread(_retime, config, media, current.data, current.ext, cancel)
    except asyncio.CancelledError:
        cancel.set()  # or the abandoned thread outlives the lock
        raise
    try:
        corrected = await asyncio.to_thread(validate_subtitle, out, item.lang)
    except InvalidSubtitleError as exc:
        raise _SyncFailedError(f"ffsubsync output is invalid: {redact_exc(exc)}") from exc
    if corrected.ext != current.ext:
        raise _SyncFailedError(f"ffsubsync changed the format to {corrected.ext}")
    shift, spread = measure_shift(current.data, corrected.data)
    if max(abs(shift), spread) > settings.max_offset_seconds:
        raise _SyncFailedError(f"implausible shift of {shift:.1f}s; subtitle left alone")
    stats.checked += 1
    if abs(shift) < settings.min_offset_seconds and spread < settings.min_offset_seconds:
        await _db(_mark, item, STATE_IN_SYNC)
        stats.in_sync += 1
        return
    backup = await _nas(_store_corrected, subtitle, raw, corrected.data)
    await _db(_note_correction, item, shift, backup)
    stats.corrected += 1
    log.info("subtitle.retimed", title=item.title, lang=item.lang, shift=round(shift, 3))


def _note_correction(item: _Item, shift: float, backup: str) -> None:
    _mark(item, STATE_CORRECTED, offset=shift, backup=backup)


def _store_failure(item: _Item, detail: str) -> None:
    _mark(item, STATE_FAILED, detail=detail)


async def _note_failure(item: _Item, stats: SyncStats, exc: Exception) -> None:
    stats.failed += 1
    detail = redact_exc(exc)
    stats.add_error(f"{item.title} [{item.lang}]: {detail}")
    try:
        await _db(_store_failure, item, detail)
    except Exception as db_exc:  # the rest of the queue still gets its turn
        log.warning("subtitle.sync_not_recorded", title=item.title, detail=redact_exc(db_exc))


def _require_ready(config: AppConfig) -> None:
    reason = unavailable_reason(config)
    if reason is not None:
        raise NotConfiguredError(reason)


async def run_sync_check(
    config: AppConfig, *, on_started: Callable[[], None] | None = None
) -> SyncStats:
    """Check up to ``subtitles.sync.max_per_run`` subtitles, one at a time.

    Raises NotConfiguredError when it can't run at all, JobBusyError when it already
    is. An import stops it early with ``stats.deferred`` set.
    """

    _require_ready(config)
    with job_lock(config, SYNC_LOCK):
        if not await _library_ready(config):
            raise NotConfiguredError("library mount is unavailable; timing check skipped")
        if on_started is not None:
            on_started()
        stats = SyncStats()
        queue = await _db(_collect, utcnow())
        stats.considered = len(queue)
        budget = config.subtitles.sync.max_per_run
        cancel = threading.Event()
        for position, item in enumerate(queue):
            if position >= budget:
                stats.capped = len(queue) - position
                break
            if is_import_active(config):
                stats.deferred = True
                break
            try:
                await _check_item(config, item, stats, cancel)
            except _DeferredError:
                stats.deferred = True
                break
            except (_StuckError, _NasTimeoutError) as exc:
                await _note_failure(item, stats, exc)
                break  # the share is the problem; don't leave more threads on it
            except Exception as exc:
                await _note_failure(item, stats, exc)
    log.info("subtitle.sync_done", **stats.as_dict())
    return stats
