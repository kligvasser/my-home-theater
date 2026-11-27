"""Library junk: what sits in the movie and TV folders that nothing plays or reads.

The library should hold videos, their subtitles and Kodi's own sidecars. Downloads
bring more than that, and so do we: release notes, screenshots, torrent padding,
subtitle archives, abandoned partial downloads, samples, and "subtitles" that are a
provider's over-the-limit advert. :func:`find_junk` names those; :func:`remove_junk`
sends them to the trash (:mod:`.trash`), so a wrong call can be put back. With
``trash.enabled: false`` they are deleted outright.

A file is junk only on positive evidence. Anything this module doesn't recognise is
reported as *unrecognised* and left where it is.

Unlike NAS extras (:mod:`.cleanup`), junk needs no review before it goes: none of it
is something to watch. The scheduled job still refuses a run larger than
``junk.auto_limit``, since that many at once means a rule is wrong or a folder isn't
what it looks like.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select

from .config import AppConfig
from .db.models import OwnedFile, Subtitle
from .db.session import session_scope
from .errors import redact_exc
from .logging_setup import get_logger
from .scanner.filesystem import IGNORED_DIRS
from .scanner.parse import is_media_file, is_subtitle_file
from .subtitles.native.payload import PLACEHOLDER_MAX_CHARS, is_placeholder_subtitle
from .trash import TrashError, move_to_trash

log = get_logger(__name__)

REASON_PLACEHOLDER = "placeholder subtitle"
REASON_RELEASE_NOTE = "release note"
REASON_IMAGE = "screenshot or stray image"
REASON_PADDING = "torrent padding"
REASON_ARCHIVE = "subtitle archive"
REASON_PARTIAL = "abandoned partial download"
REASON_SAMPLE = "sample or promo clip"
REASON_SAMPLE_SUBTITLE = "subtitle of a sample"
REASON_OS = "OS metadata"
REASON_ORPHAN = "left in a folder with no video"
REASON_STALE_NFO = "metadata of a removed video"
# What kodi_nfo stamps on the files it writes (kept in step by a test).
_OUR_NFO_MARKER = "<!-- written by my-home-theater -->"

# Removed outright: Finder writes these back whenever the share is browsed, so
# trashing them would fill the trash with files nobody could want restored.
_NO_TRASH_REASONS = frozenset({REASON_OS})

_OS_FILES = frozenset({".ds_store", "thumbs.db", "desktop.ini", ".localized"})
_NOTE_EXTENSIONS = frozenset(
    {".txt", ".url", ".website", ".lnk", ".htm", ".html", ".exe", ".sfv", ".md5", ".diz"}
)
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tbn"})
_ARCHIVE_EXTENSIONS = frozenset({".zip", ".rar", ".7z"})
_PARTIAL_EXTENSIONS = frozenset({".part", ".!qb", ".crdownload", ".aria2"})
# Subtitle formats the scanner doesn't count but players do read.
_EXTRA_SUBTITLE_EXTENSIONS = frozenset({".idx", ".sup"})
# Kodi reads local artwork by these names, alone ("poster.jpg") or as a suffix
# ("Movie (2020)-poster.jpg", "season01-poster.jpg").
_KODI_ART = (
    "poster",
    "fanart",
    "folder",
    "banner",
    "landscape",
    "clearlogo",
    "clearart",
    "discart",
    "thumb",
    "keyart",
    "logo",
    "cover",
)
_NFO_XML_MARKERS = ("<movie", "<tvshow", "<episodedetails", "<musicvideo")
_NFO_URL_MARKERS = ("themoviedb.org/", "imdb.com/title/", "thetvdb.com/")
_NFO_FOLDER_NAMES = frozenset({"movie", "tvshow"})
_PADDING_DIR = ".pad"
_PADDING_PREFIX = "_____padding_file_"
_SAMPLE_DIRS = frozenset({"sample", "samples"})
_SAMPLE_WORDS = frozenset({"sample"})

SAMPLE_MAX_BYTES = 500 * 1024 * 1024
# A video this small beside a full-length one is a tracker's promo clip.
PROMO_MAX_BYTES = 20 * 1024 * 1024
# The main video must be at least this many times larger than a sample or promo.
_MAIN_RATIO = 10
# Too new to judge: something may still be writing it.
MIN_AGE_SECONDS = 15 * 60
_NFO_SNIFF_BYTES = 8192
_UNRECOGNISED_SHOWN = 50
# Verdict for a small archive while ``junk.remove_archives`` is off: counted, not listed.
_KEPT_ARCHIVE = "kept archive"


@dataclass(frozen=True, slots=True)
class JunkFile:
    path: str  # absolute path on the mounted library
    size: int
    reason: str


@dataclass
class JunkPlan:
    files: list[JunkFile] = field(default_factory=list)
    empty_dirs: list[str] = field(default_factory=list)
    unrecognised: list[str] = field(default_factory=list)  # reported, never removed
    archives_kept: int = 0  # small archives left alone: junk.remove_archives is off
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    def by_reason(self) -> dict[str, tuple[int, int]]:
        """reason -> (file count, bytes), largest count first."""

        out: dict[str, tuple[int, int]] = {}
        for f in self.files:
            count, size = out.get(f.reason, (0, 0))
            out[f.reason] = (count + 1, size + f.size)
        return dict(sorted(out.items(), key=lambda kv: kv[1][0], reverse=True))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_bytes"] = self.total_bytes
        d["by_reason"] = {k: {"count": c, "bytes": b} for k, (c, b) in self.by_reason().items()}
        return d


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str
    rel: str  # relative to the library root (Movies or TV Shows)
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def top(self) -> str:
        """The movie or show folder; empty for a file directly in the root."""

        parts = self.rel.split(os.sep)
        return parts[0] if len(parts) > 1 else ""


def _read_head(path: str, limit: int) -> str:
    with open(path, "rb") as fh:
        return fh.read(limit).decode("utf-8", errors="replace")


def _is_kodi_art(name: str) -> bool:
    stem = os.path.splitext(name)[0].lower()
    return any(stem == art or stem.endswith(f"-{art}") for art in _KODI_ART)


def _nfo_verdict(path: str, *, named_for_video: bool) -> str | None:
    """A Kodi ``.nfo`` is XML, or a scraper URL in a file Kodi would look at (named
    after its video, or ``movie.nfo``/``tvshow.nfo``). Anything else is a release
    note. One of ours that has outlived its video is stale. Unreadable counts as
    Kodi's: keeping a release note costs nothing."""

    try:
        head = _read_head(path, _NFO_SNIFF_BYTES)
    except OSError:
        return None
    if _OUR_NFO_MARKER in head and not named_for_video:
        return REASON_STALE_NFO
    lowered = head.lower()
    if any(marker in lowered for marker in _NFO_XML_MARKERS):
        return None
    if named_for_video and any(marker in lowered for marker in _NFO_URL_MARKERS):
        return None
    return REASON_RELEASE_NOTE


def _is_placeholder_file(entry: _Entry) -> bool:
    if entry.size > PLACEHOLDER_MAX_CHARS * 4:  # UTF-8 is at most 4 bytes a character
        return False
    try:
        return is_placeholder_subtitle(_read_head(entry.path, PLACEHOLDER_MAX_CHARS * 4))
    except OSError:
        return False


def _in_dir(entry: _Entry, names: frozenset[str] | set[str]) -> bool:
    return any(part.lower() in names for part in entry.rel.split(os.sep)[:-1])


def _classify_simple(
    entry: _Entry, config: AppConfig, now: float, video_stems: set[tuple[str, str]]
) -> str | None | bool:
    """A reason (junk), ``None`` (keep) or ``False`` (unrecognised). Videos and
    subtitles are settled by the caller, which can see their neighbours.
    ``video_stems`` holds (folder, lower-cased stem) for every video."""

    name = entry.name
    lowered = name.lower()
    ext = entry.ext
    if lowered in _OS_FILES or name.startswith("._"):
        return REASON_OS
    if _in_dir(entry, {_PADDING_DIR}) or lowered.startswith(_PADDING_PREFIX):
        return REASON_PADDING
    if name.startswith("."):
        return None  # our own temp files and probes
    if lowered.endswith(".part.meta"):
        return None  # the importer's resume stamp
    if ext in _PARTIAL_EXTENSIONS:
        if os.path.exists(entry.path + ".meta"):
            return None  # the importer's own copy, resumed on a later sync
        stale = now - entry.mtime >= config.junk.partial_days * 86400
        return REASON_PARTIAL if stale else None
    if ext in _EXTRA_SUBTITLE_EXTENSIONS:
        return None
    if ext in _NOTE_EXTENSIONS:
        return REASON_RELEASE_NOTE
    if ext == ".nfo":
        stem = os.path.splitext(lowered)[0]
        named = stem in _NFO_FOLDER_NAMES or (os.path.dirname(entry.path), stem) in video_stems
        return _nfo_verdict(entry.path, named_for_video=named)
    if ext in _IMAGE_EXTENSIONS:
        return None if _is_kodi_art(name) else REASON_IMAGE
    if ext in _ARCHIVE_EXTENSIONS:
        if entry.size > config.junk.archive_max_mb * 1024 * 1024:
            return False  # a big one may be the release itself
        return REASON_ARCHIVE if config.junk.remove_archives else _KEPT_ARCHIVE
    return False


def _words(text: str) -> set[str]:
    return set("".join(c if c.isalnum() else " " for c in text.lower()).split())


def _sample_reason(entry: _Entry, main_size: int) -> str | None:
    """Movies only: a small video beside a much larger one, named or filed as a sample,
    or too small to be anything but a promo clip."""

    if entry.size > SAMPLE_MAX_BYTES or entry.size * _MAIN_RATIO > main_size:
        return None
    named = bool((_words(entry.name) - _words(entry.top)) & _SAMPLE_WORDS) or _in_dir(
        entry, _SAMPLE_DIRS
    )
    return REASON_SAMPLE if named or entry.size <= PROMO_MAX_BYTES else None


def _walk(root: str, notes: list[str]) -> tuple[list[_Entry], list[str]]:
    """Every file under ``root`` (bar ignored folders) and every empty folder."""

    entries: list[_Entry] = []
    empty: list[str] = []
    errors: list[OSError] = []
    for dirpath, dirs, files in os.walk(root, onerror=errors.append):
        dirs[:] = [
            d
            for d in dirs
            if d.lower() == _PADDING_DIR or not (d.startswith(".") or d in IGNORED_DIRS)
        ]
        if not dirs and not files and dirpath != root:
            empty.append(dirpath)
        for name in files:
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            entries.append(_Entry(full, os.path.relpath(full, root), st.st_size, st.st_mtime))
    if errors:
        notes.append(f"{len(errors)} directory read error(s) while walking {root!r}.")
    return entries, empty


def _classify_root(
    entries: list[_Entry], config: AppConfig, now: float, *, movies: bool
) -> tuple[list[JunkFile], list[str], int]:
    junk: dict[str, JunkFile] = {}
    unrecognised: list[str] = []
    archives_kept = 0

    def mark(entry: _Entry, reason: str) -> None:
        junk[entry.path] = JunkFile(entry.path, entry.size, reason)

    videos = [e for e in entries if is_media_file(e.name)]
    main_size: dict[str, int] = {}
    for e in videos:
        main_size[e.top] = max(main_size.get(e.top, 0), e.size)
    fresh = {e.top for e in entries if now - e.mtime < MIN_AGE_SECONDS}
    video_stems = {(os.path.dirname(e.path), os.path.splitext(e.name)[0].lower()) for e in videos}

    sample_stems: dict[str, set[str]] = {}  # folder -> stems of its junk videos
    for e in entries:
        if now - e.mtime < MIN_AGE_SECONDS:
            continue
        if is_media_file(e.name):
            reason = _sample_reason(e, main_size.get(e.top, 0)) if movies and e.top else None
            if reason:
                mark(e, reason)
                sample_stems.setdefault(e.top, set()).add(os.path.splitext(e.name)[0].lower())
            continue
        if is_subtitle_file(e.name):
            if e.ext == ".srt" and _is_placeholder_file(e):
                mark(e, REASON_PLACEHOLDER)
            continue
        verdict = _classify_simple(e, config, now, video_stems)
        if verdict is False:
            unrecognised.append(e.path)
        elif verdict == _KEPT_ARCHIVE:
            archives_kept += 1
        elif isinstance(verdict, str):
            mark(e, verdict)

    for e in entries:
        if e.path in junk or not is_subtitle_file(e.name):
            continue
        lowered = e.name.lower()
        if any(lowered.startswith(stem + ".") for stem in sample_stems.get(e.top, ())):
            mark(e, REASON_SAMPLE_SUBTITLE)

    # A movie or show folder whose video is gone: what's left serves nothing. Folders
    # with anything recent in them, or a partial download still in play, are skipped.
    kept_partials = {e.top for e in entries if e.ext in _PARTIAL_EXTENSIONS and e.path not in junk}
    for e in entries:
        if not e.top or e.path in junk or e.name.startswith("."):
            continue
        if e.top in main_size or e.top in fresh or e.top in kept_partials:
            continue
        mark(e, REASON_ORPHAN)
    return list(junk.values()), [p for p in unrecognised if p not in junk], archives_kept


def find_junk(config: AppConfig) -> JunkPlan:
    """Dry run: what :func:`remove_junk` would take. Needs the library mount."""

    from .acquisition.torrent.importer import ensure_mounted

    plan = JunkPlan()
    base = config.torrent.library_base_dir
    if not base:
        plan.notes.append("Junk skipped: torrent.library_base_dir (the mount) is not set.")
        return plan
    try:
        ensure_mounted(base, None)
    except Exception as exc:
        plan.notes.append(f"Junk skipped: {redact_exc(exc)}")
        return plan

    now = time.time()
    unrecognised: list[str] = []
    for rel_root, movies in ((config.nas.movies_root, True), (config.nas.tv_root, False)):
        root = os.path.join(base, *rel_root.split("/"))
        if not os.path.isdir(root):
            plan.notes.append(f"Junk skipped for {root!r}: not found.")
            continue
        entries, empty = _walk(root, plan.notes)
        files, unknown, kept = _classify_root(entries, config, now, movies=movies)
        plan.files.extend(files)
        plan.archives_kept += kept
        plan.empty_dirs.extend(empty)
        unrecognised.extend(unknown)
    plan.files.sort(key=lambda f: (f.reason, f.path))
    plan.unrecognised = sorted(unrecognised)[:_UNRECOGNISED_SHOWN]
    if plan.archives_kept:
        plan.notes.append(
            f"{plan.archives_kept} small archive(s) kept: junk.remove_archives is off."
        )
    if len(unrecognised) > _UNRECOGNISED_SHOWN:
        plan.notes.append(
            f"{len(unrecognised)} unrecognised file(s); the first {_UNRECOGNISED_SHOWN} are listed."
        )
    return plan


def _forget_subtitles(paths: list[str]) -> None:
    """Drop the catalog's claim to subtitles that turned out to be placeholders, so
    the next sweep looks for real ones instead of waiting for a scan to notice."""

    if not paths:
        return
    with session_scope() as s:
        for row in s.scalars(select(Subtitle).where(Subtitle.path.in_(paths))):
            if row.owned_file_id is not None:
                owned = s.get(OwnedFile, row.owned_file_id)
                if owned is not None and owned.subtitle_langs:
                    owned.subtitle_langs = [
                        lang for lang in owned.subtitle_langs if lang != row.lang
                    ] or None
            s.delete(row)


def _library_roots(config: AppConfig) -> list[str]:
    base = config.torrent.library_base_dir or ""
    return [
        os.path.join(base, *rel.split("/")) for rel in (config.nas.movies_root, config.nas.tv_root)
    ]


def _prune_empty(start: str, roots: list[str]) -> int:
    """Remove ``start`` and each parent left empty, stopping at a library root.
    ``rmdir`` only ever takes an empty folder, so a wrong guess is a no-op."""

    removed = 0
    current = os.path.normpath(start)
    normal_roots = [os.path.normpath(r) for r in roots]
    while not any(current == r for r in normal_roots) and any(
        current.startswith(r + os.sep) for r in normal_roots
    ):
        try:
            os.rmdir(current)
        except OSError:
            break
        removed += 1
        current = os.path.dirname(current)
    return removed


def remove_junk(
    config: AppConfig, plan: JunkPlan, cancel: threading.Event | None = None
) -> dict[str, Any]:
    """Trash the plan's files and remove the folders that leaves empty.

    Stops between files once ``cancel`` is set. A file the trash can't take stays
    put and is reported; only OS metadata and ``trash.enabled: false`` delete outright.
    """

    removed = 0
    freed = 0
    errors: list[str] = []
    placeholders: list[str] = []
    parents: set[str] = set(plan.empty_dirs)
    for item in plan.files:
        if cancel is not None and cancel.is_set():
            break
        try:
            if config.trash.enabled and item.reason not in _NO_TRASH_REASONS:
                move_to_trash(config, item.path, reason=f"junk: {item.reason}")
            else:
                os.remove(item.path)
        except FileNotFoundError:
            pass  # already gone: the desired end state
        except (OSError, TrashError) as exc:
            errors.append(f"{os.path.basename(item.path)}: {redact_exc(exc)}")
            log.warning("junk.remove_failed", path=item.path, detail=redact_exc(exc))
            continue
        removed += 1
        freed += item.size
        parents.add(os.path.dirname(item.path))
        if item.reason == REASON_PLACEHOLDER:
            placeholders.append(item.path)

    try:
        _forget_subtitles(placeholders)
    except Exception as exc:  # the files are gone either way; the next scan catches up
        log.warning("junk.catalog_update_failed", detail=redact_exc(exc))

    roots = _library_roots(config)
    dirs_removed = 0
    for parent in sorted(parents, key=len, reverse=True):  # deepest first
        if cancel is not None and cancel.is_set():
            break
        dirs_removed += _prune_empty(parent, roots)

    log.info("junk.removed", files=removed, bytes=freed, dirs=dirs_removed, errors=len(errors))
    return {
        "junk_removed": removed,
        "junk_bytes": freed,
        "dirs_removed": dirs_removed,
        "errors": errors,
        "notes": list(plan.notes),
    }


_FIND_TIMEOUT_SECONDS = 600.0
_REMOVE_TIMEOUT_SECONDS = 1800.0


async def plan_junk(config: AppConfig) -> JunkPlan:
    """Dry run under the ``sync`` lock: the WD MyCloud throws EIO when NAS work
    overlaps an import. Raises JobBusyError if one is running."""

    from .cleanup import _on_nas_thread
    from .locks import job_lock

    with job_lock(config, "sync"):
        return await _on_nas_thread(_FIND_TIMEOUT_SECONDS, find_junk, config)


async def apply_junk(
    config: AppConfig, *, limit: int | None = None, cancel: threading.Event | None = None
) -> dict[str, Any]:
    """Find and remove junk under the ``sync`` lock. With ``limit``, a plan holding
    more files than that is left alone and reported in ``refused``."""

    from .cleanup import _on_nas_thread
    from .locks import job_lock

    cancel = cancel or threading.Event()
    with job_lock(config, "sync"):
        plan = await _on_nas_thread(_FIND_TIMEOUT_SECONDS, find_junk, config)
        if limit is not None and len(plan.files) > limit:
            return {
                "junk_removed": 0,
                "junk_bytes": 0,
                "dirs_removed": 0,
                "errors": [],
                "notes": list(plan.notes),
                "refused": len(plan.files),
            }
        try:
            return await _on_nas_thread(_REMOVE_TIMEOUT_SECONDS, remove_junk, config, plan, cancel)
        except TimeoutError:
            cancel.set()
            return {
                "junk_removed": 0,
                "junk_bytes": 0,
                "dirs_removed": 0,
                "errors": [
                    f"junk removal gave up after {_REMOVE_TIMEOUT_SECONDS:.0f}s "
                    "(is the mount wedged?); some files may already be in the trash."
                ],
                "notes": list(plan.notes),
            }
