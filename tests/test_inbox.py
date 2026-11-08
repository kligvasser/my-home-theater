"""F3: the attention inbox, and accepting a refused prune."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from homeTheater.db import init_db, session_scope
from homeTheater.db.models import (
    MARKER_HELD,
    MARKER_NEEDS_ATTENTION,
    MARKER_STALLED,
    Candidate,
    CandidateSource,
    CandidateStatus,
    Download,
    JobRun,
    OwnedFile,
    RunStatus,
    Title,
    TitleKind,
)
from homeTheater.scanner.filesystem import FileEntry
from homeTheater.scanner.service import prune_fingerprint, scan_library

AUTH = {"X-Auth-Token": "test-token"}


@pytest.fixture
def client(config_file: Path) -> Iterator[TestClient]:
    from homeTheater.api.app import create_app

    init_db()
    with TestClient(create_app()) as c:
        yield c


def _grab(
    name: str,
    status: CandidateStatus,
    *downloads: tuple[str, str | None],
    kind: TitleKind = TitleKind.movie,
) -> int:
    with session_scope() as s:
        title = Title(title=name, kind=kind, tmdb_id=abs(hash(name)) % 10**6, year=2021)
        s.add(title)
        s.flush()
        cand = Candidate(title_id=title.id, source=CandidateSource.manual, status=status)
        s.add(cand)
        s.flush()
        for n, (state, error) in enumerate(downloads):
            s.add(
                Download(
                    candidate_id=cand.id, external_id=f"h{cand.id}{n}", state=state, error=error
                )
            )
        return cand.id


def _items(client: TestClient) -> list[dict[str, Any]]:
    return client.get("/api/inbox").json()["items"]


def _by_kind(client: TestClient, kind: str) -> list[dict[str, Any]]:
    return [i for i in _items(client) if i["kind"] == kind]


def test_empty_inbox(client: TestClient) -> None:
    data = client.get("/api/inbox").json()
    assert data == {"items": [], "hidden": 0, "total": 0}
    assert client.get("/api/inbox/count").json() == {"count": 0}


def test_page_and_nav_hook_render(client: TestClient) -> None:
    page = client.get("/inbox")
    assert page.status_code == 200
    assert 'id="inbox-list"' in page.text and 'id="inbox-badge"' in page.text
    assert "/static/ops.js" in page.text


def test_parked_import_offers_the_existing_retry_endpoint(client: TestClient) -> None:
    cid = _grab(
        "Parked",
        CandidateStatus.downloading,
        ("completed", f"{MARKER_NEEDS_ATTENTION} import failed: disk full"),
    )
    [item] = _by_kind(client, "import")
    assert "disk full" in item["hint"] and "Retry import" in item["hint"]
    action = item["actions"][0]
    assert action["url"] == f"/api/candidates/{cid}/retry-import"

    assert client.post(action["url"], headers=AUTH).json() == {"retrying": 1}
    assert _by_kind(client, "import") == []


def test_library_conflict_is_called_out(client: TestClient) -> None:
    from homeTheater.acquisition.torrent.importer import _conflict_same_size

    message = f"{MARKER_NEEDS_ATTENTION} import failed: {_conflict_same_size('/x/a.mkv')}"
    _grab("Clash", CandidateStatus.downloading, ("completed", message))
    [item] = _by_kind(client, "conflict")
    assert item["label"] == "Library conflict" and item["actions"][0]["label"] == "Retry import"


def test_held_item_says_what_to_do(client: TestClient) -> None:
    _grab(
        "Held",
        CandidateStatus.downloading,
        ("completed", f"{MARKER_HELD} rename an episode to SxxEyy"),
    )
    [item] = _by_kind(client, "held")
    assert item["hint"] == "rename an episode to SxxEyy"
    assert item["actions"][0]["href"] == "/activity"


def test_failed_and_stalled_offer_retry(client: TestClient) -> None:
    failed = _grab("Dead", CandidateStatus.failed, ("failed", "no release"))
    stalled = _grab("Slow", CandidateStatus.failed, ("failed", f"{MARKER_STALLED} no progress"))
    assert _by_kind(client, "failed")[0]["actions"][0]["url"] == (
        f"/api/candidates/{failed}/restart"
    )
    assert _by_kind(client, "stalled")[0]["actions"][0]["url"] == (
        f"/api/candidates/{stalled}/restart"
    )


def test_a_parked_episode_on_a_top_up_season_shows_though_activity_hides_it(
    client: TestClient,
) -> None:
    """A season being topped up sits at ``approved`` or ``imported``, neither of which
    is on Activity's list."""

    _grab(
        "Top Up",
        CandidateStatus.approved,
        ("completed", f"{MARKER_NEEDS_ATTENTION} import failed: x"),
        kind=TitleKind.series,
    )
    assert len(_by_kind(client, "import")) == 1


def test_stopped_candidates_and_retrying_ones_stay_out(client: TestClient) -> None:
    _grab(
        "Rejected",
        CandidateStatus.rejected,
        ("completed", f"{MARKER_NEEDS_ATTENTION} import failed: x"),
    )
    # Going round again by itself: an old stalled attempt isn't the owner's problem.
    _grab("Regrabbing", CandidateStatus.approved, ("failed", f"{MARKER_STALLED} no progress"))
    assert _items(client) == []


def test_badge_counts_what_the_page_lists_without_probing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _grab("One", CandidateStatus.failed, ("failed", "x"))

    async def no_probe(config: Any) -> list[Any]:
        raise AssertionError("the badge must not probe providers")

    monkeypatch.setattr("homeTheater.api.inbox.check_all", no_probe)
    assert client.get("/api/inbox/count").json() == {"count": 1}


def test_provider_problems_use_the_last_probe_for_the_badge(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.health import ProviderStatus, checks

    monkeypatch.setattr(
        checks,
        "_cache",
        (
            0.0,
            "key",
            [
                ProviderStatus("tmdb", True, False, "HTTP 401"),
                ProviderStatus("ktuvit", False, None, "no account"),
                ProviderStatus("omdb", True, True, "ok"),
            ],
        ),
    )
    assert client.get("/api/inbox/count").json() == {"count": 1}


def test_provider_problem_item(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.health import ProviderStatus

    async def probe(config: Any) -> list[ProviderStatus]:
        return [ProviderStatus("tmdb", True, False, "HTTP 401")]

    monkeypatch.setattr("homeTheater.api.inbox.check_all", probe)
    [item] = _by_kind(client, "provider")
    assert item["title"] == "tmdb" and item["detail"] == "HTTP 401"


def test_stale_job_offers_run_now_or_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scheduler.catchup import JobFreshness

    rows = [
        JobFreshness("scan", 360, None, None, True),
        JobFreshness("discovery", 720, "t", 6000, True),
        JobFreshness("backup", 1440, "t", 9000, True),
        JobFreshness("sync", 10, "t", 1, False),
    ]
    monkeypatch.setattr("homeTheater.inbox.job_freshness", lambda cfg: rows)
    items = {i["title"]: i for i in _by_kind(client, "job")}
    assert set(items) == {"scan", "discovery", "backup"}
    assert items["scan"]["actions"][0]["url"] == "/api/pipeline/scan"
    assert items["discovery"]["actions"][0]["body"] == {}
    assert items["discovery"]["detail"].startswith("Last success 100 h ago")
    assert items["backup"]["actions"][0]["href"] == "/status"


def test_unmatched_titles_with_files_link_to_repair(client: TestClient) -> None:
    from homeTheater import inbox

    with session_scope() as s:
        lost = Title(title="Lost", kind=TitleKind.movie, year=1999)
        s.add(lost)
        s.add(Title(title="No files", kind=TitleKind.movie))
        s.flush()
        s.add(OwnedFile(title_id=lost.id, path="/m/lost.mkv", kind=TitleKind.movie))
        lost_id = lost.id
    [item] = _by_kind(client, "unmatched")
    assert item["title"] == "Lost (1999)"
    assert item["actions"][0]["href"] == f"/repair?title={lost_id}"
    assert inbox.UNMATCHED_SHOWN >= 1


def test_many_unmatched_titles_count_in_full(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("homeTheater.inbox.UNMATCHED_SHOWN", 2)
    with session_scope() as s:
        for n in range(5):
            title = Title(title=f"T{n}", kind=TitleKind.movie)
            s.add(title)
            s.flush()
            s.add(OwnedFile(title_id=title.id, path=f"/m/{n}.mkv", kind=TitleKind.movie))
    data = client.get("/api/inbox").json()
    assert len(data["items"]) == 2 and data["hidden"] == 3 and data["total"] == 5
    assert client.get("/api/inbox/count").json()["count"] == 5


# --- refused prunes ---------------------------------------------------------------


class ListFS:
    def __init__(self, base: str, paths: dict[str, list[str]]) -> None:
        self.base, self.paths = base, paths

    def resolve(self, root: str) -> str:
        return f"{self.base}/{root}"

    def walk(self, root: str) -> Iterator[FileEntry]:
        for path in self.paths[root]:
            parent, _, name = path.rpartition("/")
            yield FileEntry(path, name, parent, 1)


BASE = "/nas"


def _seed(root: str, count: int) -> list[str]:
    paths = [f"{BASE}/{root}/Film {n:02d} (2000).mkv" for n in range(count)]
    with session_scope() as s:
        title = Title(title=root, kind=TitleKind.movie)
        s.add(title)
        s.flush()
        title.owned_files = [OwnedFile(path=p, kind=TitleKind.movie) for p in paths]
    return paths


def _refuse(paths: list[str], root: str = "Movies", keep: int = 14) -> None:
    fs = ListFS(BASE, {root: paths[:keep]})
    with pytest.raises(RuntimeError, match="Scan failed"):
        scan_library(fs, {TitleKind.movie: root})


def _rows() -> set[str]:
    with session_scope() as s:
        return set(s.scalars(select(OwnedFile.path)))


def test_refused_scan_shows_count_sample_and_limit(client: TestClient) -> None:
    paths = _seed("Movies", 20)
    _refuse(paths)
    [item] = _by_kind(client, "prune")
    assert "6 of 20 catalog rows under Movies (30%)" in item["detail"]
    assert "5% or 100 rows" in item["detail"]
    assert item["sample"][0] == "Film 14 (2000).mkv"  # relative: no NAS paths on the LAN
    action = item["actions"][0]
    assert action["label"] == "Accept this removal" and "Film 14" in action["confirm"]
    assert "5% or 100 rows" in action["confirm"]
    assert action["body"]["fingerprint"] == prune_fingerprint(paths[14:])


def test_sample_is_bounded_and_the_rest_are_counted(client: TestClient) -> None:
    paths = _seed("Movies", 200)
    _refuse(paths, keep=20)
    [item] = _by_kind(client, "prune")
    assert len(item["sample"]) == 10
    assert "…and 170 more" in item["actions"][0]["confirm"]


def test_an_empty_root_warns_it_is_probably_the_nas(client: TestClient) -> None:
    paths = _seed("Movies", 20)
    _refuse(paths, keep=0)
    [item] = _by_kind(client, "prune")
    assert "Check the NAS" in item["hint"]
    assert "usually means the NAS dropped" in item["actions"][0]["confirm"]


def test_a_clean_scan_clears_the_refusal(client: TestClient) -> None:
    paths = _seed("Movies", 20)
    _refuse(paths)
    scan_library(ListFS(BASE, {"Movies": paths}), {TitleKind.movie: "Movies"})
    assert _by_kind(client, "prune") == []


def test_a_running_scan_does_not_hide_the_refusal(client: TestClient) -> None:
    from homeTheater.db.base import utcnow

    paths = _seed("Movies", 20)
    _refuse(paths)
    with session_scope() as s:
        s.add(JobRun(kind="scan", started_at=utcnow(), status=RunStatus.running))
    assert len(_by_kind(client, "prune")) == 1


def _accept_body(client: TestClient) -> dict[str, str]:
    return _by_kind(client, "prune")[0]["actions"][0]["body"]


def test_force_prune_needs_the_token(client: TestClient) -> None:
    body = {"root": "Movies", "fingerprint": "x"}
    assert client.post("/api/inbox/force-prune", json=body).status_code == 401


def test_force_prune_runs_a_real_scan_under_the_lock(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import is_job_running

    paths = _seed("Movies", 20)
    _refuse(paths)
    held: list[bool] = []

    class Recording(ListFS):
        def walk(self, root: str) -> Iterator[FileEntry]:
            held.append(is_job_running(get_config(), "scan"))
            return super().walk(root)

    fs = Recording(BASE, {"Movies": paths[:14]})
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: fs)
    monkeypatch.setattr("homeTheater.scanner.config_roots", lambda cfg: {TitleKind.movie: "Movies"})

    out = client.post("/api/inbox/force-prune", json=_accept_body(client), headers=AUTH)
    assert out.json()["started"] is True
    assert held == [True]
    assert _rows() == set(paths[:14])
    assert _by_kind(client, "prune") == []


def test_force_prune_refuses_a_set_that_grew_after_review(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NAS degrades between the review and the rescan: what was accepted was six
    rows, not whatever the walk now misses."""

    paths = _seed("Movies", 20)
    _refuse(paths)
    worse = ListFS(BASE, {"Movies": paths[:2]})
    monkeypatch.setattr("homeTheater.scanner.build_filesystem", lambda cfg: worse)
    monkeypatch.setattr("homeTheater.scanner.config_roots", lambda cfg: {TitleKind.movie: "Movies"})

    client.post("/api/inbox/force-prune", json=_accept_body(client), headers=AUTH)
    assert _rows() == set(paths)
    [item] = _by_kind(client, "prune")
    assert "18 of 20" in item["detail"]  # back for a new decision


def test_force_prune_leaves_other_roots_guarded(client: TestClient) -> None:
    from homeTheater.scanner import service

    movies, shows = _seed("Movies", 20), _seed("Shows", 20)
    roots = {TitleKind.movie: "Movies", TitleKind.series: "Shows"}
    fs = ListFS(BASE, {"Movies": movies[:14], "Shows": shows[:14]})
    with pytest.raises(RuntimeError):
        service.scan_library(fs, roots)

    with pytest.raises(RuntimeError, match="refusing to prune"):
        service.scan_library(
            fs,
            roots,
            force_prune=True,
            accepted_prunes={"Movies": prune_fingerprint(movies[14:])},
        )
    assert _rows() == {*movies[:14], *shows}


def test_force_prune_rejects_an_out_of_date_review(client: TestClient) -> None:
    paths = _seed("Movies", 20)
    _refuse(paths)
    body = {"root": "Movies", "fingerprint": "not-what-was-refused"}
    out = client.post("/api/inbox/force-prune", json=body, headers=AUTH)
    assert out.status_code == 409 and "out of date" in out.json()["detail"]
    assert _rows() == set(paths)


def test_force_prune_waits_for_a_running_scan_or_import(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import import_active, job_lock

    paths = _seed("Movies", 20)
    _refuse(paths)
    body = _accept_body(client)
    with job_lock(get_config(), "scan"):
        assert client.post("/api/inbox/force-prune", json=body, headers=AUTH).status_code == 409
    with import_active(get_config()):
        out = client.post("/api/inbox/force-prune", json=body, headers=AUTH)
    assert out.status_code == 409 and "import" in out.json()["detail"]


def test_cli_force_prune_still_lifts_everything(client: TestClient) -> None:
    paths = _seed("Movies", 20)
    stats = scan_library(
        ListFS(BASE, {"Movies": paths[:2]}), {TitleKind.movie: "Movies"}, force_prune=True
    )
    assert stats.files_pruned == 18
