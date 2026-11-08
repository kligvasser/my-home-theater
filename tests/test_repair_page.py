"""F5: the dashboard's catalog repair. Reviewed sets only, locks and backup always."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from homeTheater.db import init_db, session_scope
from homeTheater.db.base import utcnow
from homeTheater.db.models import (
    Candidate,
    CandidateSource,
    CandidateStatus,
    OwnedFile,
    Subtitle,
    Title,
    TitleKind,
)

AUTH = {"X-Auth-Token": "test-token"}
ROOT = "//nas/share/TV Shows"


@pytest.fixture
def client(config_file: Path) -> Iterator[TestClient]:
    from homeTheater.api.app import create_app

    init_db()
    with TestClient(create_app()) as c:
        yield c


def _title(name: str, *paths: str, kind: TitleKind = TitleKind.series, **fields: Any) -> int:
    with session_scope() as s:
        title = Title(title=name, kind=kind, **fields)
        title.owned_files = [OwnedFile(path=p, kind=kind) for p in paths]
        s.add(title)
        s.flush()
        return title.id


def _candidate(title_id: int, status: CandidateStatus) -> None:
    with session_scope() as s:
        s.add(Candidate(title_id=title_id, source=CandidateSource.discovery, status=status))


def _subtitle(title_id: int) -> None:
    with session_scope() as s:
        owned = s.scalars(select(OwnedFile).where(OwnedFile.title_id == title_id)).first()
        assert owned is not None
        s.add(Subtitle(title_id=title_id, owned_file_id=owned.id, lang="he", status="downloaded"))


def _split() -> tuple[int, int]:
    good = _title(
        "The Killing", f"{ROOT}/The.Killing/Season 02/the.killing.s02e01.mkv", tmdb_id=34415
    )
    bkd = _title("bkd-tk", f"{ROOT}/The.Killing/Season 01/bkd-tk.s01e06.mkv", followed=True)
    _candidate(bkd, CandidateStatus.rejected)
    _subtitle(bkd)
    return good, bkd


def _titles() -> set[int]:
    with session_scope() as s:
        return set(s.scalars(select(Title.id)))


@pytest.fixture
def backups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from homeTheater.config import get_config
    from homeTheater.locks import is_job_running

    calls: list[str] = []

    def fake_backup(cfg: Any) -> None:
        cfg = get_config()
        assert is_job_running(cfg, "scan") and is_job_running(cfg, "enrich")
        calls.append("backup")

    monkeypatch.setattr("homeTheater.backup.backup_database", fake_backup)
    return calls


def _reviewed(client: TestClient) -> list[dict[str, Any]]:
    return client.get("/api/repair/preview").json()["repairs"]


def _apply(client: TestClient, repairs: list[dict[str, Any]]) -> Any:
    body = {"repairs": [{"folder": r["folder"], "token": r["token"]} for r in repairs]}
    return client.post("/api/repair/apply", json=body, headers=AUTH)


def test_preview_changes_nothing_and_page_renders(client: TestClient) -> None:
    good, bkd = _split()
    assert 'id="repair-splits"' in client.get("/repair").text
    data = client.get("/api/repair/preview").json()
    [repair] = data["repairs"]
    assert repair["folder"] == "The.Killing" and repair["keep"]["id"] == good
    assert [r["id"] for r in repair["merge"]] == [bkd]
    assert _titles() == {good, bkd}


def test_writes_need_the_token(client: TestClient) -> None:
    for url, body in (
        ("/api/repair/apply", {"repairs": []}),
        ("/api/repair/merge/preview", {"dup_id": 1, "keep_id": 2}),
        ("/api/repair/merge", {"dup_id": 1, "keep_id": 2, "token": "x"}),
        ("/api/repair/rematch/preview", {"title_id": 1, "tmdb_id": 2}),
        ("/api/repair/rematch", {"title_id": 1, "tmdb_id": 2, "token": "x"}),
    ):
        assert client.post(url, json=body).status_code == 401, url


def test_apply_folds_the_split_and_everything_survives(
    client: TestClient, backups: list[str]
) -> None:
    good, bkd = _split()
    out = _apply(client, _reviewed(client)).json()
    assert out == {"applied": ["The.Killing"], "outdated": [], "errors": []}
    assert _titles() == {good} and backups == ["backup"]
    with session_scope() as s:
        title = s.get(Title, good)
        assert title is not None and title.followed is True
        assert s.scalar(select(Candidate.status)) is CandidateStatus.rejected
        assert s.scalar(select(Candidate.title_id)) == good
        assert s.scalar(select(Subtitle.title_id)) == good


def test_apply_touches_only_the_selected_folders(client: TestClient, backups: list[str]) -> None:
    good, bkd = _split()
    _title("Show", f"{ROOT}/Show/Season 01/show.s01e01.mkv", tmdb_id=9)
    other = _title("zzz-x", f"{ROOT}/Show/Season 02/zzz-x.s02e01.mkv")
    repairs = _reviewed(client)
    assert len(repairs) == 2
    chosen = [r for r in repairs if r["folder"] == "Show"]
    assert _apply(client, chosen).json()["applied"] == ["Show"]
    assert bkd in _titles() and other not in _titles()


def test_a_folder_changed_since_review_is_left_alone(
    client: TestClient, backups: list[str]
) -> None:
    good, bkd = _split()
    reviewed = _reviewed(client)
    # A third title lands in the folder after the review: not what was approved.
    newcomer = _title("pfa-tk", f"{ROOT}/The.Killing/Season 03/pfa-tk.s03e01.mkv")
    out = _apply(client, reviewed).json()
    assert out["applied"] == [] and out["outdated"] == ["The.Killing"]
    assert _titles() == {good, bkd, newcomer}


def test_a_repair_that_no_longer_exists_is_reported(client: TestClient, backups: list[str]) -> None:
    _split()
    reviewed = _reviewed(client)
    assert _apply(client, reviewed).json()["applied"]
    assert _apply(client, reviewed).json()["outdated"] == ["The.Killing"]


def test_nothing_selected_is_refused(client: TestClient) -> None:
    assert client.post("/api/repair/apply", json={"repairs": []}, headers=AUTH).status_code == 422


def test_apply_waits_for_a_scan_or_enrich(client: TestClient, backups: list[str]) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock

    good, bkd = _split()
    reviewed = _reviewed(client)
    for lock in ("scan", "enrich"):
        with job_lock(get_config(), lock):
            assert _apply(client, reviewed).status_code == 409
    assert backups == [] and _titles() == {good, bkd}


def test_no_apply_without_a_backup(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.errors import NotConfiguredError

    good, bkd = _split()

    def no_backup(cfg: Any) -> None:
        raise NotConfiguredError("nothing to back up")

    monkeypatch.setattr("homeTheater.backup.backup_database", no_backup)
    assert _apply(client, _reviewed(client)).status_code == 503
    assert _titles() == {good, bkd}


def test_one_bad_folder_does_not_stop_the_rest(
    client: TestClient, backups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.scanner import repair as module

    _split()
    _title("Show", f"{ROOT}/Show/Season 01/show.s01e01.mkv", tmdb_id=9)
    _title("zzz-x", f"{ROOT}/Show/Season 02/zzz-x.s02e01.mkv")
    real = module._apply_folder

    def flaky(repair: Any, tv_root: str) -> None:
        if repair.folder == "Show":
            raise RuntimeError("boom api_key=sekret")
        real(repair, tv_root)

    monkeypatch.setattr(module, "_apply_folder", flaky)
    out = _apply(client, _reviewed(client)).json()
    assert out["applied"] == ["The.Killing"]
    assert "Show" in out["errors"][0] and "sekret" not in out["errors"][0]


# --- manual merge ------------------------------------------------------------------


def _merge_preview(client: TestClient, dup: int, keep: int) -> Any:
    body = {"dup_id": dup, "keep_id": keep}
    return client.post("/api/repair/merge/preview", json=body, headers=AUTH)


def test_manual_merge_shows_the_stakes_then_keeps_them(
    client: TestClient, backups: list[str]
) -> None:
    keep = _title("Keeper", "/m/a.mkv", kind=TitleKind.movie, tmdb_id=1)
    dup = _title("Dupe", "/m/b.mkv", kind=TitleKind.movie, followed=True, imdb_rating=7.5)
    _candidate(dup, CandidateStatus.rejected)
    _subtitle(dup)

    view = _merge_preview(client, dup, keep).json()
    assert view["dup"]["candidates"] == {"rejected": 1} and view["dup"]["subtitles"] == 1
    assert view["dup"]["followed"] is True

    body = {"dup_id": dup, "keep_id": keep, "token": view["token"]}
    assert client.post("/api/repair/merge", json=body, headers=AUTH).status_code == 200
    assert _titles() == {keep} and backups == ["backup"]
    with session_scope() as s:
        title = s.get(Title, keep)
        assert title is not None
        assert title.followed is True and title.imdb_rating == 7.5
        assert len(title.owned_files) == 2
        assert s.scalar(select(Candidate.status)) is CandidateStatus.rejected
        assert s.scalar(select(Subtitle.title_id)) == keep


def test_merge_across_kinds_is_refused(client: TestClient, backups: list[str]) -> None:
    movie = _title("A", "/m/a.mkv", kind=TitleKind.movie, tmdb_id=5)
    show = _title("A", "/t/a.mkv", kind=TitleKind.series, tmdb_id=5)
    out = _merge_preview(client, movie, show)
    assert out.status_code == 409 and "can't be merged" in out.json()["detail"]
    body = {"dup_id": movie, "keep_id": show, "token": "x"}
    assert client.post("/api/repair/merge", json=body, headers=AUTH).status_code == 409
    assert _titles() == {movie, show}


def test_merge_of_a_title_with_itself_or_nothing(client: TestClient) -> None:
    keep = _title("A", "/m/a.mkv", kind=TitleKind.movie)
    assert _merge_preview(client, keep, keep).status_code == 409
    assert _merge_preview(client, keep, keep + 99).status_code == 404


def test_merge_refuses_what_changed_after_review(client: TestClient, backups: list[str]) -> None:
    keep = _title("Keeper", "/m/a.mkv", kind=TitleKind.movie)
    dup = _title("Dupe", "/m/b.mkv", kind=TitleKind.movie)
    token = _merge_preview(client, dup, keep).json()["token"]
    _candidate(dup, CandidateStatus.rejected)
    body = {"dup_id": dup, "keep_id": keep, "token": token}
    assert client.post("/api/repair/merge", json=body, headers=AUTH).status_code == 409
    assert _titles() == {keep, dup}


def test_merging_two_identities_drops_the_loser_metadata(
    client: TestClient, backups: list[str]
) -> None:
    keep = _title("Keeper", "/m/a.mkv", kind=TitleKind.movie, tmdb_id=1)
    dup = _title("Other", "/m/b.mkv", kind=TitleKind.movie, tmdb_id=2, overview="Other film")
    token = _merge_preview(client, dup, keep).json()["token"]
    body = {"dup_id": dup, "keep_id": keep, "token": token}
    assert client.post("/api/repair/merge", json=body, headers=AUTH).status_code == 200
    with session_scope() as s:
        title = s.get(Title, keep)
        assert title is not None and title.overview is None and title.tmdb_id == 1


# --- re-match ----------------------------------------------------------------------


def _tmdb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.config import get_config

    monkeypatch.setenv("TMDB_API_KEY", "k")
    get_config.cache_clear()


def _details(tmdb_id: int = 603, *, path: str = "movie", **fields: Any) -> respx.Route:
    payload = {
        "id": tmdb_id,
        "title": "The Matrix",
        "name": "The Matrix",
        "release_date": "1999-03-31",
        "first_air_date": "1999-03-31",
        "overview": "Right film",
        "external_ids": {"imdb_id": "tt0133093"},
        "genres": [{"id": 1, "name": "Action"}],
        **fields,
    }
    url = f"https://api.themoviedb.org/3/{path}/{tmdb_id}"
    return respx.get(url).mock(return_value=httpx.Response(200, json=payload))


def _rematch_preview(client: TestClient, title: int, tmdb_id: int = 603) -> Any:
    body = {"title_id": title, "tmdb_id": tmdb_id}
    return client.post("/api/repair/rematch/preview", json=body, headers=AUTH)


def _rematch(client: TestClient, title: int, token: str, tmdb_id: int = 603) -> Any:
    body = {"title_id": title, "tmdb_id": tmdb_id, "token": token}
    return client.post("/api/repair/rematch", json=body, headers=AUTH)


@respx.mock
def test_rematch_replaces_the_wrong_identity_whole(
    client: TestClient, backups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tmdb_env(monkeypatch)
    _details()
    wrong = _title(
        "Matrix Reloaded",
        "/m/a.mkv",
        kind=TitleKind.movie,
        tmdb_id=604,
        imdb_id="tt0234215",
        imdb_rating=7.2,
        imdb_votes=9,
        year=2003,
        overview="Wrong film",
        collection_name="Wrong collection",
        followed=True,
        last_enriched_at=utcnow(),
    )
    _candidate(wrong, CandidateStatus.rejected)

    view = _rematch_preview(client, wrong).json()
    assert view["match"]["title"] == "The Matrix" and view["merges_into"] is None
    assert _rematch(client, wrong, view["token"]).status_code == 200

    assert backups == ["backup"]
    with session_scope() as s:
        title = s.get(Title, wrong)
        assert title is not None
        assert (title.tmdb_id, title.imdb_id) == (603, "tt0133093")
        assert (title.title, title.year) == ("The Matrix", 1999)
        assert title.overview == "Right film"
        assert title.imdb_rating is None and title.imdb_votes is None  # wrong film's
        assert title.collection_name is None
        assert title.last_enriched_at is None  # so ratings get fetched for the new id
        assert title.followed is True
        assert s.scalar(select(Candidate.status)) is CandidateStatus.rejected


@respx.mock
def test_rematch_looks_up_the_title_s_own_kind(
    client: TestClient, backups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tmdb_env(monkeypatch)
    tv = _details(path="tv")
    show = _title("Show", "/t/a.mkv")
    assert _rematch_preview(client, show).status_code == 200
    assert tv.called


@respx.mock
def test_rematch_onto_an_id_held_by_the_other_kind_is_no_collision(
    client: TestClient, backups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tmdb_env(monkeypatch)
    _details()
    _title("Series 603", "/t/s.mkv", kind=TitleKind.series, tmdb_id=603)
    movie = _title("Film", "/m/a.mkv", kind=TitleKind.movie)
    view = _rematch_preview(client, movie).json()
    assert view["merges_into"] is None
    assert _rematch(client, movie, view["token"]).status_code == 200


@respx.mock
def test_rematch_onto_a_title_in_the_catalog_folds_into_it(
    client: TestClient, backups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tmdb_env(monkeypatch)
    _details()
    holder = _title("The Matrix", "/m/a.mkv", kind=TitleKind.movie, tmdb_id=603, year=1999)
    wrong = _title(
        "Mtrx", "/m/b.mkv", kind=TitleKind.movie, overview="Wrong", followed=True, year=2010
    )
    _candidate(wrong, CandidateStatus.rejected)

    view = _rematch_preview(client, wrong).json()
    assert view["merges_into"]["id"] == holder
    assert _rematch(client, wrong, view["token"]).status_code == 200

    assert _titles() == {holder}
    with session_scope() as s:
        title = s.get(Title, holder)
        assert title is not None and title.followed is True
        assert title.overview == "Right film" and title.year == 1999
        assert len(title.owned_files) == 2
        assert s.scalar(select(Candidate.title_id)) == holder


@respx.mock
def test_rematch_refuses_what_changed_after_review(
    client: TestClient, backups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _tmdb_env(monkeypatch)
    _details()
    title = _title("Film", "/m/a.mkv", kind=TitleKind.movie)
    token = _rematch_preview(client, title).json()["token"]
    other = _title("The Matrix", "/m/b.mkv", kind=TitleKind.movie, tmdb_id=603)
    assert _rematch(client, title, token).status_code == 409
    assert _titles() == {title, other}


@respx.mock
def test_rematch_to_the_same_id_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tmdb_env(monkeypatch)
    _details()
    title = _title("The Matrix", "/m/a.mkv", kind=TitleKind.movie, tmdb_id=603)
    assert _rematch_preview(client, title).status_code == 409


@respx.mock
def test_rematch_errors_are_plain(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _tmdb_env(monkeypatch)
    title = _title("Film", "/m/a.mkv", kind=TitleKind.movie)
    route = respx.get("https://api.themoviedb.org/3/movie/603")
    route.mock(return_value=httpx.Response(404, json={}))
    out = _rematch_preview(client, title)
    assert out.status_code == 404 and "no movie with id 603" in out.json()["detail"]

    route.mock(side_effect=httpx.ConnectError("https://x/?api_key=sekret"))
    out = _rematch_preview(client, title)
    assert out.status_code == 502 and "sekret" not in out.text
    assert _rematch_preview(client, title + 9).status_code == 404


def test_rematch_without_a_tmdb_key(client: TestClient) -> None:
    title = _title("Film", "/m/a.mkv", kind=TitleKind.movie)
    assert _rematch_preview(client, title).status_code == 503
