"""Quality upgrades (F4): replace an owned movie with a clearly better release.

Movies only. Off unless ``upgrades.enabled``, and unless ``upgrades.auto`` every
proposal waits for a click.

A proposal is a normal :class:`Candidate` with ``source=upgrade``: it goes through the
same claim, screening, sync, import, Activity and history as any other, under the
same restart/cancel/imported guards. What differs:

* It pins one release (``features["upgrade"]``). A grab never searches again, so what
  downloads is what the reason said.
* It imports under a quality-tagged name beside the old file, so nothing is
  overwritten. The old file goes to the trash only after the new one is read back at
  its final path, through ``importer._may_replace`` with a grant for that exact file.
* It says nothing about taste: ``preferences`` skips it, and a rejected proposal is
  no dislike of the movie.

"Clearly better" is deliberately narrow: a release has to be provably better on one
axis (source tier or resolution) and no worse on the other. Unknown never counts in
a release's favour, and a same-tier, same-resolution swap never qualifies, however
many seeders or however modern the codec. The owned resolution and codec come from
the catalog, falling back to the release it was imported from when a scan of a
clean filename left the catalog blank. The catalog has no source column, so the
owned tier comes from that release name, then the file and folder names.
"""

from __future__ import annotations

import asyncio
import enum
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from guessit import guessit
from sqlalchemy import select, update

from .config import AppConfig
from .db.base import utcnow
from .db.models import (
    Candidate,
    CandidateSource,
    CandidateStatus,
    Download,
    OwnedFile,
    Subtitle,
    Title,
    TitleKind,
    UpgradeCheck,
)
from .db.session import session_scope
from .errors import InvalidTransitionError, NotConfiguredError, redact_exc
from .features import extract_features
from .locks import job_lock
from .logging_setup import get_logger

log = get_logger(__name__)

# A title with one of these is already being worked on.
_LIVE = (
    CandidateStatus.new,
    CandidateStatus.approved,
    CandidateStatus.queued,
    CandidateStatus.downloading,
)


class SourceTier(enum.IntEnum):
    CAM = 0  # cam, telesync, telecine, screener
    BROADCAST = 1  # TV and DVD captures: below WEB, above anything shot in a cinema
    WEB = 2
    BLURAY = 3
    REMUX = 4


_TIER_LABELS = {
    SourceTier.CAM: "CAM",
    SourceTier.BROADCAST: "TV",
    SourceTier.WEB: "WEB",
    SourceTier.BLURAY: "BluRay",
    SourceTier.REMUX: "Remux",
}
_GUESSIT_TIERS = {
    "Camera": SourceTier.CAM,
    "HD Camera": SourceTier.CAM,
    "Telesync": SourceTier.CAM,
    "HD Telesync": SourceTier.CAM,
    "Telecine": SourceTier.CAM,
    "HD Telecine": SourceTier.CAM,
    "Workprint": SourceTier.CAM,
    "Screener": SourceTier.CAM,
    "TV": SourceTier.BROADCAST,
    "HDTV": SourceTier.BROADCAST,
    "Ultra HDTV": SourceTier.BROADCAST,
    "Digital TV": SourceTier.BROADCAST,
    "Satellite": SourceTier.BROADCAST,
    "Pay-per-view": SourceTier.BROADCAST,
    "VHS": SourceTier.BROADCAST,
    "DVD": SourceTier.BROADCAST,
    "HD-DVD": SourceTier.BROADCAST,
    "Web": SourceTier.WEB,
    "Blu-ray": SourceTier.BLURAY,
    "Ultra HD Blu-ray": SourceTier.BLURAY,
}
_HEIGHT = re.compile(r"(\d{3,4})[pi]$")
# Not the movie you own, whatever the resolution says: a dub replaces the audio, burnt
# subtitles can't be turned off, an upscale has no more detail, 3D needs a 3D screen.
_DISQUALIFIED = re.compile(
    r"(?i)(?<![a-z0-9])(dub|dubbed|korsub|hardsub|hardcoded|hc|upscaled?|sbs|hsbs|3d)(?![a-z0-9])"
)


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def source_tier(name: str) -> SourceTier | None:
    """The source tier a release or file name proves, or None. With several sources
    named, the worst counts."""

    info = guessit(name)
    tiers = [_GUESSIT_TIERS[s] for s in _as_list(info.get("source")) if s in _GUESSIT_TIERS]
    tier = min(tiers) if tiers else None
    if "Remux" in _as_list(info.get("other")) and (tier is None or tier is SourceTier.BLURAY):
        return SourceTier.REMUX
    return tier


def resolution_height(resolution: str | None) -> int | None:
    """Lines of vertical resolution: '1080p' -> 1080, '4k' -> 2160."""

    text = (resolution or "").strip().lower()
    if text in {"4k", "uhd"}:
        return 2160
    match = _HEIGHT.fullmatch(text)
    return int(match.group(1)) if match else None


@dataclass(frozen=True, slots=True)
class Quality:
    resolution: str | None = None
    tier: SourceTier | None = None
    codec: str | None = None

    @property
    def height(self) -> int | None:
        return resolution_height(self.resolution)

    def label(self) -> str:
        tier = _TIER_LABELS[self.tier] if self.tier is not None else None
        return " ".join(p for p in (self.resolution, tier, self.codec) if p) or "unknown quality"

    def tag(self) -> str:
        tier = _TIER_LABELS[self.tier] if self.tier is not None else None
        return " ".join(p for p in (self.resolution, tier) if p)


def upgrade_reasons(owned: Quality, new: Quality) -> list[str]:
    """Why ``new`` is clearly better than ``owned``; empty if it isn't.

    The release must prove both axes. A step up on one mustn't be a step down on the
    other, and a source jump needs the owned resolution known too, so it can't be
    hiding a drop (a CAM counts as the bottom whatever its name leaves out).
    """

    height, owned_height = new.height, owned.height
    if height is None or new.tier is None or new.tier is SourceTier.CAM:
        return []
    if owned.tier is not None and new.tier < owned.tier:
        return []
    if owned_height is not None and height < owned_height:
        return []
    reasons: list[str] = []
    resolution_known = owned_height is not None or owned.tier is SourceTier.CAM
    if owned.tier is not None and new.tier > owned.tier and resolution_known:
        reasons.append(f"source {_TIER_LABELS[owned.tier]} -> {_TIER_LABELS[new.tier]}")
    if owned_height is not None and height > owned_height:
        reasons.append(f"resolution {owned.resolution} -> {new.resolution}")
    return reasons


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """What a proposal pins, stored as ``Candidate.features["upgrade"]``."""

    owned_file_id: int
    owned_path: str  # as the catalog has it
    owned_size: int
    owned_label: str
    tag: str  # names the new file: "1080p BluRay"
    reasons: list[str]
    release: dict[str, Any]  # the TorrentRelease fields

    def torrent_release(self) -> Any:
        from .acquisition.torrent.base import TorrentRelease

        return TorrentRelease(**self.release)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_plan(candidate_id: int) -> UpgradePlan | None:
    """The pinned plan of an upgrade candidate; None for any other candidate. An upgrade
    with no usable plan raises: falling through to a normal grab would pick its own
    release for a movie that's already in the library."""

    with session_scope() as s:
        cand = s.get(Candidate, candidate_id)
        if cand is None or cand.source is not CandidateSource.upgrade:
            return None
        raw: Any = (cand.features or {}).get("upgrade")
    try:
        return UpgradePlan(**raw)
    except TypeError:
        raise InvalidTransitionError(
            f"upgrade candidate {candidate_id} has no recorded release"
        ) from None


def library_path(config: AppConfig, catalog_path: str) -> str | None:
    """Where a catalog path is on the mount, or None if it isn't under it."""

    base = config.torrent.library_base_dir
    if not base:
        return None
    if catalog_path.startswith("\\\\"):
        from .nas_paths import MountPathError, unc_to_mount

        try:
            return unc_to_mount(catalog_path, base)
        except MountPathError:
            return None
    root = os.path.normpath(base) + os.sep
    return catalog_path if os.path.normpath(catalog_path).startswith(root) else None


def grant_for(config: AppConfig, plan: UpgradePlan) -> Any:
    """The importer's permission to supersede this plan's file."""

    from .acquisition.torrent.importer import ReplacementGrant

    local = library_path(config, plan.owned_path)
    if local is None:
        raise NotConfiguredError("the file being upgraded isn't under torrent.library_base_dir")
    return ReplacementGrant(path=local, size=plan.owned_size, tag=plan.tag)


def blocked_reason(config: AppConfig) -> str | None:
    """Why upgrades can't run with this config, or None. The trash is required, not
    optional: without it nothing can honour "the old file is never destroyed"."""

    if not config.upgrades.enabled:
        return "upgrades.enabled is off"
    if config.acquisition.backend != "torrent":
        return "upgrades need acquisition.backend: torrent"
    if not config.torrent.library_base_dir:
        return "upgrades need torrent.library_base_dir (the NAS mount)"
    if not config.torrent.import_to_library:
        return "upgrades need torrent.import_to_library"
    if not config.trash.enabled:
        return "upgrades need trash.enabled"
    return None


def stale_reason(plan: UpgradePlan) -> str | None:
    """Why a proposal no longer applies: the file it was judged against has changed."""

    with session_scope() as s:
        owned = s.get(OwnedFile, plan.owned_file_id)
        if owned is None or owned.path != plan.owned_path:
            return "the owned file is no longer in the catalog"
        if owned.size_bytes != plan.owned_size:
            return "the owned file changed since this was proposed"
    return None


def withdraw(candidate_id: int, reason: str) -> None:
    """Cancel a proposal that no longer applies. ``cancelled`` isn't a label, and a
    rejected or finished candidate stays exactly what it is."""

    with session_scope() as s:
        s.execute(
            update(Candidate)
            .where(
                Candidate.id == candidate_id,
                Candidate.status.in_(
                    (
                        CandidateStatus.new,
                        CandidateStatus.approved,
                        CandidateStatus.failed,
                        CandidateStatus.queued,
                    )
                ),
            )
            .values(status=CandidateStatus.cancelled, decided_at=utcnow())
        )
    log.info("upgrades.withdrawn", candidate=candidate_id, reason=reason)


def finish_upgrade(plan: UpgradePlan, title_id: int) -> None:
    """Catalog side of a finished swap: drop the old file's row, the way a scan's
    prune would, and restart the title's cooldown."""

    with session_scope() as s:
        owned = s.get(OwnedFile, plan.owned_file_id)
        if owned is not None and owned.path == plan.owned_path:
            s.execute(
                update(Subtitle)
                .where(Subtitle.owned_file_id == owned.id)
                .values(owned_file_id=None)
            )
            s.delete(owned)
        _stamp(s, title_id)


def _stamp(s: Any, title_id: int) -> None:
    check = s.get(UpgradeCheck, title_id)
    if check is None:
        s.add(UpgradeCheck(title_id=title_id, last_checked_at=utcnow()))
    else:
        check.last_checked_at = utcnow()


@dataclass(frozen=True, slots=True)
class _Target:
    title_id: int
    title: str
    year: int
    owned_file_id: int
    owned_path: str
    owned_size: int
    quality: Quality
    last_checked: datetime | None
    seen_hashes: frozenset[str]


@dataclass
class UpgradeStats:
    checked: int = 0
    proposed: int = 0
    no_hits: int = 0
    unknown_quality: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _owned_quality(owned: OwnedFile, title: str, release_names: Iterable[str]) -> Quality:
    from .acquisition.torrent.select import _quality_text, detect_resolution

    segments = re.split(r"[\\/]", owned.path)
    names = [_quality_text(n, title) for n in (*release_names, *reversed(segments[-2:])) if n]
    resolution = owned.resolution or next(filter(None, map(detect_resolution, names)), None)
    tier = next((t for t in map(source_tier, names) if t is not None), None)
    return Quality(resolution=resolution, tier=tier, codec=owned.codec)


def _seen_hashes(s: Any, title_id: int) -> tuple[frozenset[str], dict[str, list[str]]]:
    """Every hash this title has been offered or downloaded under, so a dismissed or
    failed release isn't proposed again, plus the release names by saved path."""

    rows = s.execute(
        select(Candidate.features, Download.external_id, Download.release, Download.save_path)
        .select_from(Candidate)
        .outerjoin(Download, Download.candidate_id == Candidate.id)
        .where(Candidate.title_id == title_id)
    ).all()
    hashes: set[str] = set()
    releases: dict[str, list[str]] = {}
    for features, external_id, release, save_path in rows:
        pinned = ((features or {}).get("upgrade") or {}).get("release") or {}
        for value in (pinned.get("infohash"), external_id):
            if value:
                hashes.add(str(value).lower())
        if release and save_path:
            releases.setdefault(save_path, []).append(release)
    return frozenset(hashes), releases


def _targets(config: AppConfig, stats: UpgradeStats) -> list[_Target]:
    """Movies due a look, longest-unchecked first. One owned file only: a title in two
    parts, or with duplicates, has no single file to replace. A movie already at
    the top of what's allowed costs no search."""

    allowed = config.torrent.resolutions or config.thresholds.allowed_resolutions
    best = max((resolution_height(r) or 0 for r in allowed), default=0)
    cutoff = utcnow() - timedelta(days=config.upgrades.cooldown_days)
    out: list[_Target] = []
    with session_scope() as s:
        files: dict[int, list[OwnedFile]] = {}
        for owned in s.scalars(select(OwnedFile).where(OwnedFile.kind == TitleKind.movie)):
            files.setdefault(owned.title_id, []).append(owned)
        busy = set(s.scalars(select(Candidate.title_id).where(Candidate.status.in_(_LIVE))).all())
        checks = {c.title_id: c.last_checked_at for c in s.scalars(select(UpgradeCheck))}
        for title_id, owned_files in files.items():
            checked = checks.get(title_id)
            if checked is not None and checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
            if len(owned_files) != 1 or title_id in busy or (checked and checked > cutoff):
                continue
            owned = owned_files[0]
            title = s.get(Title, title_id)
            local = library_path(config, owned.path)
            if title is None or not title.year or not owned.size_bytes or local is None:
                continue
            seen, releases = _seen_hashes(s, title_id)
            quality = _owned_quality(owned, title.title, releases.get(local, []))
            if quality.height is None and quality.tier is not SourceTier.CAM:
                stats.unknown_quality += 1
                continue
            if quality.tier is SourceTier.REMUX and (quality.height or 0) >= best:
                continue
            out.append(
                _Target(
                    title_id=title_id,
                    title=title.title,
                    year=title.year,
                    owned_file_id=owned.id,
                    owned_path=owned.path,
                    owned_size=owned.size_bytes,
                    quality=quality,
                    last_checked=checked,
                    seen_hashes=seen,
                )
            )
    out.sort(key=lambda t: t.last_checked or datetime.min.replace(tzinfo=UTC))
    return out[: config.upgrades.max_titles_per_run]


def release_quality(name: str, title: str) -> Quality:
    from .acquisition.torrent.select import _quality_text, detect_resolution

    text = _quality_text(name, title)
    return Quality(
        resolution=detect_resolution(text),
        tier=source_tier(text),
        codec=guessit(text).get("video_codec"),
    )


def pick_upgrade(
    config: AppConfig,
    target: _Target,
    releases: list[Any],
    *,
    banned: frozenset[str],
    dead: frozenset[str],
) -> tuple[Any, Quality, list[str]] | None:
    """The best release that's clearly better than the owned copy, or None. Stricter
    than a normal grab, which may settle: the year has to be read from the name, and
    a hash that died before is out rather than last in line."""

    from .acquisition.torrent.select import _quality_text, _release_year, select_release

    floor = max(config.upgrades.min_seeders, config.torrent.min_seeders)
    judged: dict[str, tuple[Quality, list[str]]] = {}
    eligible: list[Any] = []
    for rel in releases:
        infohash = rel.btih()
        if not infohash or infohash in target.seen_hashes or infohash in dead:
            continue
        if _DISQUALIFIED.search(_quality_text(rel.title, target.title)):
            continue
        year = _release_year(rel.title, target.title)
        if year is None or abs(year - target.year) > 1:
            continue
        quality = release_quality(rel.title, target.title)
        reasons = upgrade_reasons(target.quality, quality)
        if reasons:
            judged[infohash] = (quality, reasons)
            eligible.append(rel)
    chosen = select_release(
        eligible,
        allowed_resolutions=config.torrent.resolutions or config.thresholds.allowed_resolutions,
        min_seeders=floor,
        banned=banned,
        require_hd=True,
        movie=True,
        expected_title=target.title,
        expected_year=target.year,
    )
    if chosen is None:
        return None
    quality, reasons = judged[str(chosen.btih())]
    return chosen, quality, reasons


def _propose(config: AppConfig, target: _Target, rel: Any, new: Quality, why: list[str]) -> bool:
    """Create the proposal, unless something else took the title meanwhile."""

    plan = UpgradePlan(
        owned_file_id=target.owned_file_id,
        owned_path=target.owned_path,
        owned_size=target.owned_size,
        owned_label=target.quality.label(),
        tag=new.tag(),
        reasons=why,
        release={
            "source": rel.source,
            "title": rel.title,
            "seeders": rel.seeders,
            "leechers": rel.leechers,
            "size_bytes": rel.size_bytes,
            "infohash": rel.btih(),
            "magnet": rel.magnet_uri(),
        },
    )
    auto = config.upgrades.auto
    reason = (
        f"Upgrade: {target.quality.label()} -> {new.label()} ({'; '.join(why)}). "
        f"{rel.seeders} seeders on {rel.source}: {rel.title}"
    )
    with session_scope() as s:
        title = s.get(Title, target.title_id)
        live = s.scalar(
            select(Candidate.id).where(
                Candidate.title_id == target.title_id, Candidate.status.in_(_LIVE)
            )
        )
        if title is None or live is not None:
            return False
        features = {**extract_features(title), "upgrade": plan.as_dict()}
        if auto:
            features["auto"] = True
        s.add(
            Candidate(
                title_id=target.title_id,
                source=CandidateSource.upgrade,
                status=CandidateStatus.approved if auto else CandidateStatus.new,
                reason=reason,
                features=features,
                decided_at=utcnow() if auto else None,
            )
        )
    log.info("upgrades.proposed", title=target.title, reasons=why, auto=auto)
    return True


async def scan_for_upgrades(config: AppConfig) -> UpgradeStats:
    """Look for better copies of owned movies and propose them. Searches trackers and
    writes candidates; nothing downloads here. JobBusyError if one is running."""

    with job_lock(config, "upgrades"):
        return await _scan(config)


async def _scan(config: AppConfig) -> UpgradeStats:
    from .acquisition.torrent.select import build_query
    from .acquisition.torrent.service import (
        _build_sources,
        _dead_hashes,
        _quarantined_hashes,
        _search_all,
    )

    blocked = blocked_reason(config)
    if blocked is not None:
        raise NotConfiguredError(blocked)

    stats = UpgradeStats()
    targets = await asyncio.to_thread(_targets, config, stats)
    if not targets:
        return stats
    banned, dead = _quarantined_hashes(), _dead_hashes()
    async with httpx.AsyncClient(timeout=config.torrent.request_timeout) as http:
        sources = _build_sources(config, http)
        if not sources:
            raise NotConfiguredError("No torrent sources enabled; set torrent.enabled_sources.")
        for target in targets:
            stats.checked += 1
            query = build_query(target.title, target.year, TitleKind.movie)
            try:
                releases = await _search_all(config, sources, query, TitleKind.movie)
                if not releases:  # a tracker outage looks like this: don't start a cooldown
                    stats.no_hits += 1
                    continue
                picked = pick_upgrade(config, target, releases, banned=banned, dead=dead)
                with session_scope() as s:
                    _stamp(s, target.title_id)
                if picked is not None and _propose(config, target, *picked):
                    stats.proposed += 1
            except Exception as exc:  # one bad title must not sink the sweep
                stats.errors.append(f"{target.title}: {redact_exc(exc)}")
                log.warning("upgrades.title_failed", title=target.title, detail=redact_exc(exc))
    log.info("upgrades.done", **stats.as_dict())
    return stats


@dataclass(frozen=True, slots=True)
class UpgradeProposal:
    id: int
    title: str
    year: int | None
    reason: str | None


def list_proposals() -> list[UpgradeProposal]:
    """Upgrades waiting for a click, newest first."""

    with session_scope() as s:
        rows = s.execute(
            select(Candidate.id, Title.title, Title.year, Candidate.reason)
            .join(Title, Title.id == Candidate.title_id)
            .where(
                Candidate.source == CandidateSource.upgrade,
                Candidate.status == CandidateStatus.new,
            )
            .order_by(Candidate.id.desc())
        ).all()
    return [UpgradeProposal(*row) for row in rows]
