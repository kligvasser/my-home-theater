"""F4: quality upgrades. The file that matters most is the one already in the library:
nothing here may lose it, and nothing may download without a click."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from homeTheater import trash as tr
from homeTheater import upgrades as up
from homeTheater.acquisition.torrent import importer
from homeTheater.acquisition.torrent import service as svc
from homeTheater.db.base import utcnow
from homeTheater.db.models import (
    MARKER_NEEDS_ATTENTION,
    Candidate,
    CandidateSource,
    CandidateStatus,
    Download,
    DownloadTracking,
    OwnedFile,
    Title,
    TitleKind,
    TrashItem,
    UpgradeCheck,
)
from homeTheater.db.session import session_scope
from homeTheater.errors import InvalidTransitionError, NotConfiguredError
from homeTheater.upgrades import Quality, SourceTier, source_tier, upgrade_reasons
from tests.test_acquisition_audit import (
    APIBAY,
    GB,
    HASH,
    OTHER,
    TRANSMISSION,
    _client,
    _indexer,
    _queue,
    _reset,
    _row,
    _sync,
)

RELEASE = "The Matrix 1999 1080p BluRay x264-GRP"
NEW_NAME = "The Matrix (1999) [1080p BluRay].mkv"
OLD_NAME = "The Matrix (1999).mkv"
OLD_BYTES = b"old movie"
NEW_BYTES = b"new, better movie"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from homeTheater.config import get_config
    from homeTheater.db import init_db

    path = tmp_path / "upgrades.yaml"
    path.write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'u.db'}'}}\n"
        "features: {dry_run: false, auto_approve: false}\n"
        "acquisition: {backend: torrent}\n"
        "upgrades: {enabled: true, min_seeders: 10, cooldown_days: 30}\n"
        "torrent:\n"
        "  enabled_sources: [piratebay]\n"
        "  min_seeders: 1\n"
        f"  piratebay_api_url: {APIBAY}\n"
        f"  library_base_dir: {tmp_path / 'lib'}\n"
        "  delete_local_after_import: true\n"
    )
    (tmp_path / "lib").mkdir()
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(path))
    monkeypatch.setenv("TRANSMISSION_URL", TRANSMISSION)
    _reset()
    init_db()
    monkeypatch.setattr(svc, "_METADATA_PROBE_DELAY", 0.0)
    yield get_config()
    _reset()


def _with(cfg: Any, **upgrades: Any) -> Any:
    return cfg.model_copy(update={"upgrades": cfg.upgrades.model_copy(update=upgrades)})


def _lib(cfg: Any) -> Path:
    return Path(cfg.torrent.library_base_dir)


def _owned(
    cfg: Any,
    *,
    title: str = "The Matrix",
    year: int | None = 1999,
    name: str = OLD_NAME,
    resolution: str | None = "720p",
    codec: str | None = "x264",
) -> tuple[int, int, Path]:
    """A movie in the library and the catalog: (title id, owned id, path)."""

    folder = f"{title} ({year})"
    path = _lib(cfg) / "Movies" / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(OLD_BYTES)
    with session_scope() as s:
        t = Title(
            tmdb_id=600 + s.query(Title).count(), title=title, year=year, kind=TitleKind.movie
        )
        s.add(t)
        s.flush()
        o = OwnedFile(
            path=str(path),
            title_id=t.id,
            kind=TitleKind.movie,
            resolution=resolution,
            codec=codec,
            size_bytes=len(OLD_BYTES),
        )
        s.add(o)
        s.flush()
        return t.id, o.id, path


def _proposal(
    title_id: int,
    owned_id: int,
    path: Path,
    *,
    status: CandidateStatus = CandidateStatus.approved,
    infohash: str = HASH,
    tag: str = "1080p BluRay",
    release: str = RELEASE,
) -> int:
    plan = up.UpgradePlan(
        owned_file_id=owned_id,
        owned_path=str(path),
        owned_size=len(OLD_BYTES),
        owned_label="720p x264",
        tag=tag,
        reasons=["resolution 720p -> 1080p"],
        release={
            "source": "piratebay",
            "title": release,
            "seeders": 50,
            "leechers": 1,
            "size_bytes": GB,
            "infohash": infohash,
            "magnet": None,
        },
    )
    with session_scope() as s:
        c = Candidate(
            title_id=title_id,
            source=CandidateSource.upgrade,
            status=status,
            reason="Upgrade: 720p x264 -> 1080p BluRay",
            features={"kind": "movie", "upgrade": plan.as_dict()},
        )
        s.add(c)
        s.flush()
        return c.id


def _download(cid: int, infohash: str = HASH, state: str = "downloading") -> None:
    with session_scope() as s:
        s.add(
            Download(
                candidate_id=cid, external_id=infohash, state=state, release=RELEASE, progress=0.5
            )
        )


def _finished_torrent(tmp_path: Path, data: bytes = NEW_BYTES, infohash: str = HASH) -> Any:
    """The downloaded file, and a client reporting it complete."""

    (tmp_path / "dl").mkdir(exist_ok=True)
    (tmp_path / "dl" / "movie.mkv").write_bytes(data)
    fake = _client()
    fake.add(
        infohash, percentDone=1.0, status=6, name="movie.mkv", downloadDir=str(tmp_path / "dl")
    )
    return fake


def _status(cid: int) -> CandidateStatus:
    with session_scope() as s:
        cand = s.get(Candidate, cid)
        assert cand is not None
        return cand.status


def _dl() -> Download:
    with session_scope() as s:
        row = s.query(Download).one()
        s.expunge(row)
        return row


def _trashed() -> list[str]:
    with session_scope() as s:
        return [i.original_path for i in s.query(TrashItem).filter_by(state=tr.STATE_TRASHED)]


def _end_backoff() -> None:
    with session_scope() as s:
        for track in s.query(DownloadTracking):
            track.next_import_at = None


# --- what counts as clearly better --------------------------------------------


@pytest.mark.parametrize(
    ("name", "tier"),
    [
        ("Movie.2020.HDCAM.x264", SourceTier.CAM),
        ("Movie.2020.HDTS.x264", SourceTier.CAM),
        ("Movie.2020.TELESYNC.x264", SourceTier.CAM),
        ("Movie.2020.HDTV.720p", SourceTier.BROADCAST),
        ("Movie.2020.1080p.WEB-DL.x264", SourceTier.WEB),
        ("Movie.2020.1080p.WEBRip.x264", SourceTier.WEB),
        ("Movie.2020.1080p.BluRay.x264", SourceTier.BLURAY),
        ("Movie.2020.1080p.BDRip.x264", SourceTier.BLURAY),
        ("Movie.2020.1080p.BluRay.REMUX.AVC", SourceTier.REMUX),
        ("Movie.2020.1080p.x264", None),
        ("Movie (2020).mkv", None),
    ],
)
def test_source_tier(name: str, tier: SourceTier | None) -> None:
    assert source_tier(name) is tier


def test_tiers_are_ordered_as_specified() -> None:
    assert SourceTier.CAM < SourceTier.WEB < SourceTier.BLURAY < SourceTier.REMUX


@pytest.mark.parametrize(
    ("value", "height"),
    [("1080p", 1080), ("2160p", 2160), ("4k", 2160), ("1080i", 1080), ("720P", 720), (None, None)],
)
def test_resolution_height(value: str | None, height: int | None) -> None:
    assert up.resolution_height(value) == height


WEB = SourceTier.WEB
BD = SourceTier.BLURAY
CAM = SourceTier.CAM
REMUX = SourceTier.REMUX


@pytest.mark.parametrize(
    ("owned", "new"),
    [
        (Quality("720p", WEB), Quality("1080p", WEB)),  # resolution jump
        (Quality("1080p", WEB), Quality("1080p", BD)),  # source jump
        (Quality("1080p", BD), Quality("1080p", REMUX)),
        (Quality("720p", WEB), Quality("1080p", BD)),  # both
        (Quality("720p", None), Quality("1080p", WEB)),  # unknown source can't be a drop proof
        (Quality(None, CAM), Quality("1080p", WEB)),  # a cam rip is the bottom regardless
    ],
)
def test_clearly_better(owned: Quality, new: Quality) -> None:
    assert upgrade_reasons(owned, new)


@pytest.mark.parametrize(
    ("owned", "new"),
    [
        (Quality("1080p", WEB, "x264"), Quality("1080p", WEB, "x265")),  # reshuffle
        (Quality("1080p", BD), Quality("1080p", BD)),
        (Quality("1080p", BD), Quality("2160p", WEB)),  # up in one, down in the other
        (Quality("1080p", WEB), Quality("720p", BD)),
        (Quality("1080p", WEB), Quality("1080p", None)),  # unknown never helps
        (Quality("720p", WEB), Quality("1080p", None)),
        (Quality("720p", WEB), Quality(None, BD)),
        (Quality("480p", None), Quality("1080p", CAM)),  # a cam is never the upgrade
        (Quality(None, WEB), Quality("1080p", BD)),  # might be hiding a resolution drop
        (Quality(None, None), Quality("2160p", REMUX)),
    ],
)
def test_not_clearly_better(owned: Quality, new: Quality) -> None:
    assert upgrade_reasons(owned, new) == []


def test_a_film_called_cam_isnt_a_cam_rip() -> None:
    assert up.release_quality("Cam 2020 1080p WEB-DL x264", "Cam").tier is SourceTier.WEB


def _target(**over: Any) -> Any:
    fields = {
        "title_id": 1,
        "title": "The Matrix",
        "year": 1999,
        "owned_file_id": 1,
        "owned_path": "/x",
        "owned_size": 1,
        "quality": Quality("720p", WEB),
        "last_checked": None,
        "seen_hashes": frozenset(),
    }
    return up._Target(**{**fields, **over})


def _rel(name: str, seeders: int = 50, infohash: str = HASH) -> Any:
    from homeTheater.acquisition.torrent.base import TorrentRelease

    return TorrentRelease("piratebay", name, seeders, 1, GB, infohash)


def _picked(cfg: Any, releases: list[Any], **target: Any) -> Any:
    empty: frozenset[str] = frozenset()
    picked = up.pick_upgrade(cfg, _target(**target), releases, banned=empty, dead=empty)
    return picked[0].title if picked else None


def test_pick_takes_a_clear_upgrade(cfg: Any) -> None:
    assert _picked(cfg, [_rel(RELEASE)]) == RELEASE


@pytest.mark.parametrize(
    "name",
    [
        "The Matrix 1999 720p WEB-DL x265-GRP",  # same tier, same resolution
        "The.Matrix.1080p.BluRay.x264-GRP",  # no year to prove which film
        "The Matrix 2021 1080p BluRay x264-GRP",  # another film
        "The Matrix Reloaded 2003 1080p BluRay x264-GRP",
        "The Matrix 1999 1080p BluRay x264 DUBBED-GRP",
        "The Matrix 1999 1080p BluRay x264 KORSUB-GRP",
        "The Matrix 1999 1080p BluRay 3D HSBS x264-GRP",
        "The Matrix 1999 2160p BluRay AI Upscaled x265-GRP",
        "The Matrix 1999 1080p HDCAM x264-GRP",
        "The Matrix 1999 1080p BluRay x264.exe",
    ],
)
def test_pick_refuses(cfg: Any, name: str) -> None:
    assert _picked(cfg, [_rel(name)]) is None


def test_pick_needs_the_seeder_bar_and_torrent_bar(cfg: Any) -> None:
    assert _picked(cfg, [_rel(RELEASE, seeders=9)]) is None
    assert _picked(cfg, [_rel(RELEASE, seeders=10)]) == RELEASE
    strict = cfg.model_copy(update={"torrent": cfg.torrent.model_copy(update={"min_seeders": 20})})
    assert _picked(strict, [_rel(RELEASE, seeders=10)]) is None


def test_pick_skips_seen_banned_and_dead_hashes(cfg: Any) -> None:
    assert _picked(cfg, [_rel(RELEASE)], seen_hashes=frozenset({HASH})) is None
    target = _target()
    empty: frozenset[str] = frozenset()
    hit = frozenset({HASH})
    assert up.pick_upgrade(cfg, target, [_rel(RELEASE)], banned=hit, dead=empty) is None
    assert up.pick_upgrade(cfg, target, [_rel(RELEASE)], banned=empty, dead=hit) is None


def test_owned_quality_uses_the_catalog_first(cfg: Any) -> None:
    title_id, _owned_id, path = _owned(cfg, name="The Matrix (1999) 1080p WEB.mkv")
    with session_scope() as s:
        owned = s.query(OwnedFile).one()
        quality = up._owned_quality(owned, "The Matrix", [])
    assert (quality.resolution, quality.tier, quality.codec) == ("720p", WEB, "x264")


def test_library_path_maps_unc_onto_the_mount(cfg: Any) -> None:
    unc = r"\\nas\T\Movies\X\x.mkv"
    assert up.library_path(cfg, unc) == str(_lib(cfg) / "Movies/X/x.mkv")
    assert up.library_path(cfg, str(_lib(cfg) / "a.mkv")) == str(_lib(cfg) / "a.mkv")
    assert up.library_path(cfg, "/elsewhere/a.mkv") is None
    assert up.library_path(cfg, r"\\nas\T") is None


# --- finding them -------------------------------------------------------------


@respx.mock
async def test_scan_proposes_and_downloads_nothing(cfg: Any) -> None:
    title_id, _owned_id, _path = _owned(cfg)
    asked = _indexer({"The Matrix 1999": [_row(RELEASE, seeders=50)]})
    fake = _client()

    stats = await up.scan_for_upgrades(cfg)

    assert stats.proposed == 1 and asked == ["The Matrix 1999"]
    assert "torrent-add" not in fake.calls
    with session_scope() as s:
        cand = s.query(Candidate).one()
        assert cand.source is CandidateSource.upgrade
        assert cand.status is CandidateStatus.new and cand.decided_at is None
        assert "720p x264 -> 1080p BluRay" in cand.reason
        assert "resolution 720p -> 1080p" in cand.reason and "50 seeders" in cand.reason
        assert cand.features["upgrade"]["release"]["infohash"] == HASH
        assert cand.features["kind"] == "movie"  # a real extract_features snapshot
        assert s.get(UpgradeCheck, title_id) is not None


@respx.mock
async def test_auto_approves_and_marks_it_a_rule_decision(cfg: Any) -> None:
    _owned(cfg)
    _indexer({"The Matrix 1999": [_row(RELEASE, seeders=50)]})
    await up.scan_for_upgrades(_with(cfg, auto=True))
    with session_scope() as s:
        cand = s.query(Candidate).one()
        assert cand.status is CandidateStatus.approved and cand.features["auto"] is True


async def test_scan_is_off_by_default(cfg: Any) -> None:
    from homeTheater.config.settings import Upgrades

    assert (Upgrades().enabled, Upgrades().auto) == (False, False)
    with pytest.raises(NotConfiguredError, match="enabled"):
        await up.scan_for_upgrades(_with(cfg, enabled=False))


@pytest.mark.parametrize(
    ("section", "update", "message"),
    [
        ("trash", {"enabled": False}, "trash"),
        ("torrent", {"library_base_dir": None}, "library_base_dir"),
        ("torrent", {"import_to_library": False}, "import_to_library"),
        ("acquisition", {"backend": "arr"}, "torrent"),
    ],
)
def test_upgrades_refuse_a_config_that_cant_keep_the_old_file(
    cfg: Any, section: str, update: dict[str, Any], message: str
) -> None:
    changed = cfg.model_copy(update={section: getattr(cfg, section).model_copy(update=update)})
    reason = up.blocked_reason(changed)
    assert reason is not None and message in reason


@respx.mock
async def test_scan_respects_the_cooldown(cfg: Any) -> None:
    _owned(cfg)
    asked = _indexer({"The Matrix 1999": [_row("Unrelated 2001")]})
    await up.scan_for_upgrades(cfg)
    with session_scope() as s:
        assert s.query(UpgradeCheck).count() == 1
    stats = await up.scan_for_upgrades(cfg)
    assert stats.checked == 0 and len(asked) == 1


@respx.mock
async def test_the_cooldown_expires(cfg: Any) -> None:
    from datetime import timedelta

    _owned(cfg)
    asked = _indexer({"The Matrix 1999": [_row("Unrelated 2001")]})
    await up.scan_for_upgrades(cfg)
    with session_scope() as s:
        s.query(UpgradeCheck).one().last_checked_at = utcnow() - timedelta(days=31)
    await up.scan_for_upgrades(cfg)
    assert len(asked) == 2


@respx.mock
async def test_a_tracker_outage_starts_no_cooldown(cfg: Any) -> None:
    _owned(cfg)
    _indexer({})
    stats = await up.scan_for_upgrades(cfg)
    assert stats.no_hits == 1
    with session_scope() as s:
        assert s.query(UpgradeCheck).count() == 0


@respx.mock
async def test_max_titles_caps_the_searches_and_oldest_goes_first(cfg: Any) -> None:
    _owned(cfg, title="Alpha", year=2001)
    _owned(cfg, title="Beta", year=2002)
    asked = _indexer({})
    await up.scan_for_upgrades(_with(cfg, max_titles_per_run=1))
    assert len(asked) == 1


@respx.mock
async def test_only_single_file_movies_with_known_quality_are_searched(cfg: Any) -> None:
    title_id, _o, path = _owned(cfg)  # two files: no single file to replace
    with session_scope() as s:
        s.add(
            OwnedFile(
                path=str(path) + ".cd2.mkv", title_id=title_id, kind=TitleKind.movie, size_bytes=1
            )
        )
    _owned(cfg, title="Unknown", year=2001, resolution=None)  # nothing proves its quality
    with session_scope() as s:  # a series isn't a movie
        show = Title(tmdb_id=9, title="Show", year=2001, kind=TitleKind.series)
        s.add(show)
        s.flush()
        s.add(
            OwnedFile(
                path=str(_lib(cfg) / "TV/s.mkv"),
                title_id=show.id,
                kind=TitleKind.series,
                resolution="480p",
                size_bytes=1,
            )
        )
    asked = _indexer({})
    stats = await up.scan_for_upgrades(cfg)
    assert asked == [] and stats.unknown_quality == 1


@respx.mock
async def test_blank_catalog_falls_back_to_the_imported_release(cfg: Any) -> None:
    """A scan of a clean filename blanks owned.resolution: it reads the name, and the
    importer's names carry no tags. The Download row still knows the release."""

    title_id, _o, path = _owned(cfg, resolution=None)
    with session_scope() as s:
        c = Candidate(
            title_id=title_id, source=CandidateSource.manual, status=CandidateStatus.imported
        )
        s.add(c)
        s.flush()
        s.add(
            Download(
                candidate_id=c.id,
                external_id=OTHER,
                state="imported",
                release="The Matrix 1999 720p WEBRip x264-OLD",
                save_path=str(path),
            )
        )
    _indexer({"The Matrix 1999": [_row(RELEASE, seeders=50)]})
    stats = await up.scan_for_upgrades(cfg)
    assert stats.proposed == 1
    with session_scope() as s:
        assert (
            "720p WEB x264 ->"
            in s.query(Candidate).filter_by(source=CandidateSource.upgrade).one().reason
        )


@respx.mock
async def test_a_title_already_in_the_pipeline_is_left_alone(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    _proposal(title_id, owned_id, path, status=CandidateStatus.new)
    asked = _indexer({})
    await up.scan_for_upgrades(cfg)
    assert asked == []


@respx.mock
async def test_a_rejected_release_isnt_proposed_again(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    rejected = _proposal(title_id, owned_id, path, status=CandidateStatus.rejected)
    _indexer({"The Matrix 1999": [_row(RELEASE, seeders=50)]})
    assert (await up.scan_for_upgrades(cfg)).proposed == 0
    assert _status(rejected) is CandidateStatus.rejected


@respx.mock
async def test_the_scan_never_touches_an_existing_rejection(cfg: Any) -> None:
    title_id, _o, _p = _owned(cfg)
    with session_scope() as s:
        c = Candidate(
            title_id=title_id, source=CandidateSource.discovery, status=CandidateStatus.rejected
        )
        s.add(c)
        s.flush()
        old = c.id
    _indexer({"The Matrix 1999": [_row(RELEASE, seeders=50)]})
    await up.scan_for_upgrades(cfg)
    assert _status(old) is CandidateStatus.rejected


async def test_job_skips_quietly_when_off(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.config.settings import Schedule
    from homeTheater.scheduler import jobs
    from homeTheater.scheduler.catchup import job_intervals
    from homeTheater.scheduler.scheduler import _JOBS

    assert job_intervals(Schedule())["upgrades"] == 1440
    assert _JOBS["upgrades"] is jobs.run_upgrades_job
    off = _with(cfg, enabled=False)

    async def config_for_run() -> Any:
        return off

    monkeypatch.setattr(jobs, "_config_for_run", config_for_run)
    await jobs.run_upgrades_job(off)
    assert "upgrades.enabled" in jobs.skip_reasons["upgrades"]


# --- grabbing -----------------------------------------------------------------


@respx.mock
async def test_nothing_downloads_without_a_click(cfg: Any) -> None:
    from homeTheater.acquisition import queue_approved

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.new)
    fake = _client()
    await queue_approved(cfg)
    assert "torrent-add" not in fake.calls and _status(cid) is CandidateStatus.new


@respx.mock
async def test_grab_takes_the_pinned_release_and_never_searches(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    asked = _indexer({"The Matrix 1999": [_row("The Matrix 1999 2160p BluRay", OTHER, 999)]})
    fake = _client()

    outcome = await _queue(cfg, cid)

    assert outcome.queued and asked == []
    assert HASH in fake.torrents and OTHER not in fake.torrents
    assert _status(cid) is CandidateStatus.queued
    assert _dl().external_id == HASH


@respx.mock
async def test_grab_needs_no_sources(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    bare = cfg.model_copy(
        update={"torrent": cfg.torrent.model_copy(update={"enabled_sources": []})}
    )
    _client()
    assert (await _queue(bare, cid)).queued


@respx.mock
async def test_dry_run_gates_an_upgrade(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    fake = _client()
    dry = cfg.model_copy(update={"features": cfg.features.model_copy(update={"dry_run": True})})
    outcome = await _queue(dry, cid)
    assert outcome.dry_run and "would grab upgrade" in outcome.message
    assert "torrent-add" not in fake.calls and _status(cid) is CandidateStatus.approved


@respx.mock
async def test_a_grab_with_upgrades_switched_off_is_refused(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    fake = _client()
    outcome = await _queue(_with(cfg, enabled=False), cid)
    assert not outcome.queued and "torrent-add" not in fake.calls
    assert _status(cid) is CandidateStatus.approved


@respx.mock
async def test_a_proposal_whose_file_changed_is_withdrawn(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    with session_scope() as s:
        owned = s.get(OwnedFile, owned_id)
        assert owned is not None
        owned.size_bytes = 12345
    fake = _client()
    outcome = await _queue(cfg, cid)
    assert not outcome.queued and "withdrawn" in outcome.message
    assert "torrent-add" not in fake.calls and _status(cid) is CandidateStatus.cancelled


@respx.mock
async def test_a_quarantined_hash_is_never_grabbed(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    other = _proposal(title_id, owned_id, path, status=CandidateStatus.cancelled)
    with session_scope() as s:
        s.add(
            Download(candidate_id=other, external_id=HASH, state="failed", error="removed: malware")
        )
    fake = _client()
    assert not (await _queue(cfg, cid)).queued
    assert "torrent-add" not in fake.calls


@respx.mock
async def test_a_plan_lost_mid_flight_parks_and_keeps_the_original(
    cfg: Any, tmp_path: Path
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _finished_torrent(tmp_path)
    with session_scope() as s:
        cand = s.get(Candidate, cid)
        assert cand is not None
        cand.features = {}

    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES and HASH in fake.torrents
    dl = _dl()
    assert dl.state == "completed" and (dl.error or "").startswith(MARKER_NEEDS_ATTENTION)


async def test_an_upgrade_with_no_plan_cant_fall_through_to_a_normal_grab(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    with session_scope() as s:
        cand = s.get(Candidate, cid)
        assert cand is not None
        cand.features = {"kind": "movie"}
    with pytest.raises(InvalidTransitionError):
        await _queue(cfg, cid)


async def test_the_arr_backend_refuses_an_upgrade(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path)
    arr = cfg.model_copy(
        update={"acquisition": cfg.acquisition.model_copy(update={"backend": "arr"})}
    )
    with pytest.raises(InvalidTransitionError):
        await _queue(arr, cid)


# --- W2: the guards still hold ------------------------------------------------


@pytest.mark.parametrize(
    "status", [CandidateStatus.imported, CandidateStatus.rejected, CandidateStatus.cancelled]
)
async def test_restart_guard_holds(cfg: Any, status: CandidateStatus) -> None:
    from homeTheater.acquisition import restart_candidate

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=status)
    with pytest.raises(InvalidTransitionError):
        await restart_candidate(cfg, cid)
    assert _status(cid) is status


@pytest.mark.parametrize("status", [CandidateStatus.imported, CandidateStatus.rejected])
async def test_cancel_guard_holds(cfg: Any, status: CandidateStatus) -> None:
    from homeTheater.acquisition import cancel_candidate

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=status)
    with pytest.raises(InvalidTransitionError):
        await cancel_candidate(cfg, cid)


async def test_restart_and_cancel_keep_off_a_running_import(cfg: Any) -> None:
    from homeTheater.acquisition import cancel_candidate, restart_candidate

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid, state="importing")
    with pytest.raises(InvalidTransitionError):
        await restart_candidate(cfg, cid)
    with pytest.raises(InvalidTransitionError):
        await cancel_candidate(cfg, cid)


async def test_queueing_a_rejected_upgrade_is_refused(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.rejected)
    with pytest.raises(InvalidTransitionError):
        await _queue(cfg, cid)
    assert _status(cid) is CandidateStatus.rejected


@respx.mock
async def test_queueing_an_imported_upgrade_is_a_no_op(cfg: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.imported)
    fake = _client()
    assert not (await _queue(cfg, cid)).queued
    assert "torrent-add" not in fake.calls


@respx.mock
async def test_restart_regrabs_the_same_pinned_release(cfg: Any) -> None:
    from homeTheater.acquisition import restart_candidate

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.failed)
    _download(cid, state="failed")
    asked = _indexer({})
    fake = _client()
    assert (await restart_candidate(cfg, cid)).queued
    assert asked == [] and HASH in fake.torrents


# --- replacing ----------------------------------------------------------------


@respx.mock
async def test_a_finished_upgrade_replaces_the_old_file(cfg: Any, tmp_path: Path) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _finished_torrent(tmp_path)

    await _sync(cfg)

    new = path.with_name(NEW_NAME)
    assert new.read_bytes() == NEW_BYTES
    assert not path.exists()
    assert _status(cid) is CandidateStatus.imported
    assert _trashed() == [f"Movies/The Matrix (1999)/{OLD_NAME}"]
    with session_scope() as s:
        trash_item = s.query(TrashItem).one()
        assert (_lib(cfg) / tr.TRASH_DIRNAME / trash_item.trash_path).read_bytes() == OLD_BYTES
        assert NEW_NAME in trash_item.reason
        rows = s.query(OwnedFile).all()
        assert [r.path for r in rows] == [str(new)]  # old row gone, new one registered
        assert s.get(UpgradeCheck, title_id) is not None  # the cooldown restarted
    assert HASH not in fake.torrents  # local copy goes only now


@respx.mock
async def test_the_old_file_is_restorable_after_a_swap(cfg: Any, tmp_path: Path) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)
    await _sync(cfg)
    with session_scope() as s:
        item_id = s.query(TrashItem).one().id
    tr.restore(cfg, item_id)
    assert path.read_bytes() == OLD_BYTES and path.with_name(NEW_NAME).exists()


@respx.mock
async def test_old_file_leaves_only_after_the_new_one_is_proven_in_place(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)
    seen: list[tuple[bytes, bytes, bool]] = []
    real = tr.move_to_trash

    def spy(config: Any, target: str, *, reason: str) -> int:
        new = path.with_name(NEW_NAME)
        part = Path(str(new) + ".part")
        seen.append((new.read_bytes(), Path(target).read_bytes(), part.exists()))
        return real(config, target, reason=reason)

    monkeypatch.setattr(tr, "move_to_trash", spy)
    await _sync(cfg)
    assert seen == [(NEW_BYTES, OLD_BYTES, False)]


@respx.mock
async def test_a_bad_copy_leaves_the_original_alone(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _finished_torrent(tmp_path)
    monkeypatch.setattr(importer, "_copy_matches", lambda *_a: False)

    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES and _trashed() == []
    dl = _dl()
    assert dl.state == "completed" and "read-back" in (dl.error or "")
    assert HASH in fake.torrents  # the local data stays too
    assert _status(cid) is not CandidateStatus.imported
    with session_scope() as s:
        assert [o.path for o in s.query(OwnedFile)] == [str(path)]


@respx.mock
async def test_a_mismatch_after_the_rename_removes_only_the_new_file(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)
    real = importer._copy_matches
    monkeypatch.setattr(
        importer,
        "_copy_matches",
        lambda src, copy, size: real(src, copy, size) and not copy.endswith(NEW_NAME),
    )
    await _sync(cfg)
    assert path.read_bytes() == OLD_BYTES and not path.with_name(NEW_NAME).exists()
    assert _trashed() == []


@respx.mock
async def test_a_failed_trash_keeps_both_and_a_retry_finishes(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _finished_torrent(tmp_path)
    real_rename = tr.os.rename

    def refuse(src: str, dst: str) -> None:
        if tr.TRASH_DIRNAME in dst:
            raise OSError(errno.EACCES, "denied")
        real_rename(src, dst)

    monkeypatch.setattr(tr.os, "rename", refuse)
    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES
    assert path.with_name(NEW_NAME).read_bytes() == NEW_BYTES
    assert _dl().state == "completed" and HASH in fake.torrents

    monkeypatch.setattr(tr.os, "rename", real_rename)
    _end_backoff()
    await _sync(cfg)

    assert not path.exists() and path.with_name(NEW_NAME).exists()
    assert _status(cid) is CandidateStatus.imported and HASH not in fake.torrents


@respx.mock
async def test_mount_gone_at_the_swap_backs_off_and_keeps_both(
    cfg: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)

    def gone(*_a: Any, **_k: Any) -> int:
        raise tr.TrashUnavailableError("NAS mount dropped")

    monkeypatch.setattr(tr, "move_to_trash", gone)
    await _sync(cfg)
    dl = _dl()
    assert path.read_bytes() == OLD_BYTES
    assert dl.state == "completed" and (dl.error or "").startswith("NAS unavailable")


@respx.mock
async def test_a_file_that_changed_since_approval_is_kept_and_parked(
    cfg: Any, tmp_path: Path
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)
    path.write_bytes(b"someone replaced this by hand")

    await _sync(cfg)

    assert path.read_bytes() == b"someone replaced this by hand" and _trashed() == []
    dl = _dl()
    assert dl.state == "completed" and (dl.error or "").startswith(MARKER_NEEDS_ATTENTION)


@respx.mock
async def test_a_held_video_keeps_the_old_file(cfg: Any, tmp_path: Path) -> None:
    """A two-part release: the part imported isn't the whole movie."""

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    folder = tmp_path / "dl" / "Release"
    folder.mkdir(parents=True)
    (folder / "cd1.mkv").write_bytes(b"a" * 100)
    (folder / "cd2.mkv").write_bytes(b"b" * 90)
    fake = _client()
    fake.add(HASH, percentDone=1.0, status=6, name="Release", downloadDir=str(tmp_path / "dl"))

    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES and _trashed() == []
    assert _dl().state == "completed" and HASH in fake.torrents
    with session_scope() as s:
        assert str(path) in [o.path for o in s.query(OwnedFile)]


@respx.mock
async def test_a_taken_name_is_a_conflict_and_nothing_moves(cfg: Any, tmp_path: Path) -> None:
    title_id, owned_id, path = _owned(cfg, name=NEW_NAME)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)

    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES and _trashed() == []
    assert (_dl().error or "").startswith(MARKER_NEEDS_ATTENTION)


@respx.mock
async def test_identical_old_and_new_file_is_never_trashed(cfg: Any, tmp_path: Path) -> None:
    """The release is byte-for-byte the file it's "replacing", under the same name:
    the import passes as already in place, and old and new are one file."""

    title_id, owned_id, path = _owned(cfg, name=NEW_NAME)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _finished_torrent(tmp_path, data=OLD_BYTES)

    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES and _trashed() == []
    assert HASH in fake.torrents


@respx.mock
async def test_turning_the_trash_off_keeps_both(cfg: Any, tmp_path: Path) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    _finished_torrent(tmp_path)
    off = cfg.model_copy(update={"trash": cfg.trash.model_copy(update={"enabled": False})})

    await _sync(off)

    assert path.read_bytes() == OLD_BYTES and path.with_name(NEW_NAME).exists()
    assert (_dl().error or "").startswith(MARKER_NEEDS_ATTENTION)


@respx.mock
async def test_import_switched_off_never_marks_an_upgrade_imported(
    cfg: Any, tmp_path: Path
) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _finished_torrent(tmp_path)
    off = cfg.model_copy(
        update={"torrent": cfg.torrent.model_copy(update={"import_to_library": False})}
    )

    await _sync(off)

    assert _status(cid) is not CandidateStatus.imported and HASH in fake.torrents
    assert path.read_bytes() == OLD_BYTES


@respx.mock
async def test_a_plain_movie_still_refuses_to_replace(cfg: Any, tmp_path: Path) -> None:
    """A7 is unchanged for everything that isn't an approved upgrade."""

    title_id, _o, path = _owned(cfg)
    with session_scope() as s:
        c = Candidate(
            title_id=title_id, source=CandidateSource.manual, status=CandidateStatus.downloading
        )
        s.add(c)
        s.flush()
        cid = c.id
    _download(cid)
    _finished_torrent(tmp_path)

    await _sync(cfg)

    assert path.read_bytes() == OLD_BYTES and _trashed() == []
    assert (_dl().error or "").startswith(MARKER_NEEDS_ATTENTION)


# --- the importer seam --------------------------------------------------------


def test_may_replace_needs_a_grant_for_this_exact_size() -> None:
    grant = importer.ReplacementGrant(path="/x", size=100, tag="t")
    assert importer._may_replace(0)
    assert not importer._may_replace(100)
    assert importer._may_replace(100, grant)
    assert not importer._may_replace(101, grant)


def test_an_upgrade_name_is_tagged_and_stable() -> None:
    assert importer._movie_dir_and_file("X", 2000, ".mkv", "1080p BluRay") == (
        "X (2000)",
        "X (2000) [1080p BluRay].mkv",
    )
    assert importer._movie_dir_and_file("X", 2000, ".mkv") == ("X (2000)", "X (2000).mkv")


def test_the_scanner_reads_an_upgrade_name_as_the_same_movie() -> None:
    from homeTheater.scanner.parse import parse_media

    parsed = parse_media("The Matrix (1999) [1080p BluRay].mkv", kind_hint=TitleKind.movie)
    assert parsed is not None
    assert (parsed.title, parsed.year, parsed.resolution) == ("The Matrix", 1999, "1080p")
    assert source_tier("The Matrix (1999) [1080p BluRay].mkv") is SourceTier.BLURAY


def test_busy_fallback_never_removes_a_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.replace fails (the NAS holds a stale handle) and a file appeared after the
    guard: the remove-then-rename fallback must refuse, not delete it."""

    import errno

    dest = tmp_path / "x.mkv"
    tmp = tmp_path / "x.mkv.part"
    dest.write_bytes(b"real library file")
    tmp.write_bytes(b"new")

    def busy(_a: str, _b: str) -> None:
        raise OSError(errno.EBUSY, "busy")

    monkeypatch.setattr(importer.os, "replace", busy)
    with pytest.raises(importer.LibraryConflictError):
        importer._finalize_local(str(tmp), str(dest))
    assert dest.read_bytes() == b"real library file"


# --- no training label --------------------------------------------------------


def _with_features(cid: int) -> None:
    with session_scope() as s:
        cand = s.get(Candidate, cid)
        assert cand is not None
        cand.features = {**(cand.features or {}), "genres": ["Action"]}
        cand.decided_at = utcnow()


@pytest.mark.parametrize(
    "status",
    [
        CandidateStatus.rejected,
        CandidateStatus.approved,
        CandidateStatus.queued,
        CandidateStatus.imported,
    ],
)
def test_an_upgrade_is_never_a_training_label(cfg: Any, status: CandidateStatus) -> None:
    from homeTheater.preferences import _training_rows

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=status)
    _with_features(cid)
    rows, _ = _training_rows(bootstrap=False)
    assert rows == []


def test_an_upgrade_doesnt_bury_a_real_decision(cfg: Any) -> None:
    from homeTheater.preferences import _training_rows

    title_id, owned_id, path = _owned(cfg)
    with session_scope() as s:
        s.add(
            Candidate(
                title_id=title_id,
                source=CandidateSource.manual,
                status=CandidateStatus.imported,
                features={"kind": "movie"},
                decided_at=utcnow(),
            )
        )
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.rejected)
    _with_features(cid)
    rows, _ = _training_rows(bootstrap=False)
    assert [r.y for r in rows] == [1]


def test_an_owned_movie_stays_a_weak_positive_after_a_rejected_upgrade(cfg: Any) -> None:
    from homeTheater.preferences import BOOTSTRAP_WEIGHT, _training_rows

    title_id, owned_id, path = _owned(cfg)
    _with_features(_proposal(title_id, owned_id, path, status=CandidateStatus.rejected))
    rows, n_bootstrap = _training_rows(bootstrap=True)
    assert n_bootstrap == 1 and [(r.y, r.weight) for r in rows] == [(1, BOOTSTRAP_WEIGHT)]


def test_reconcile_cant_complete_an_upgrade(cfg: Any) -> None:
    """The arr reporting an owned movie says nothing about the better copy."""

    from homeTheater.reconcile.service import _mark_candidate_imported

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    with session_scope() as s:
        assert _mark_candidate_imported(s, title_id) is False
    assert _status(cid) is CandidateStatus.downloading


# --- dashboard ----------------------------------------------------------------


@pytest.fixture
def client(cfg: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("DASHBOARD_TOKEN", "test-token")
    _reset()
    from homeTheater.api.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_candidates_page_shows_proposals_with_reason_and_buttons(cfg: Any, client: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.new)

    html = client.get("/candidates").text

    assert "Proposed quality upgrades" in html and "Upgrade: 720p x264 -&gt; 1080p BluRay" in html
    assert f'data-verb="approve" data-id="{cid}"' in html
    assert html.count(f'data-verb="approve" data-id="{cid}"') == 1  # the panel, not the grid too


def test_approving_through_the_api_only_schedules_it(cfg: Any, client: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.new)
    r = client.post(f"/api/candidates/{cid}/approve", headers={"X-Auth-Token": "test-token"})
    assert r.status_code == 200 and _status(cid) is CandidateStatus.approved
    assert up.list_proposals() == []


def test_history_shows_an_upgrade_with_its_reason(cfg: Any, client: Any) -> None:
    title_id, owned_id, path = _owned(cfg)
    _proposal(title_id, owned_id, path, status=CandidateStatus.imported)
    html = client.get("/candidates?status=imported").text
    assert "⬆ upgrade" in html and "Upgrade: 720p x264" in html


@respx.mock
async def test_activity_tracks_an_upgrade(cfg: Any) -> None:
    from homeTheater.pipeline import activity

    title_id, owned_id, path = _owned(cfg)
    cid = _proposal(title_id, owned_id, path, status=CandidateStatus.downloading)
    _download(cid)
    fake = _client()
    fake.add(HASH, percentDone=0.4, status=4, name="movie.mkv")
    states = await activity(cfg)
    assert [s.candidate_id for s in states] == [cid]
    assert states[0].release == RELEASE


def test_cli_reports_a_disabled_feature(
    cfg: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from homeTheater import main as cli

    monkeypatch.setattr("homeTheater.config.effective_config", lambda: _with(cfg, enabled=False))
    monkeypatch.setattr(sys, "argv", ["home-theater", "upgrades"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "Upgrades skipped" in capsys.readouterr().out
