"""F6: the per-file view over HTTP: approve, swap, revert."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homeTheater.db.models import (
    OwnedFile,
    Subtitle,
    SubtitleReject,
    SubtitleSync,
    Title,
    TitleKind,
)
from homeTheater.subtitles import review
from homeTheater.subtitles.choices import result_key
from homeTheater.subtitles.native.base import SubtitleResult

HEADERS = {"X-Auth-Token": "tok"}
OLD = b"1\n00:00:01,000 --> 00:00:02,000\nold\n"
RETIMED = b"1\n00:00:03,000 --> 00:00:04,000\nold\n"
NEW = b"1\n00:00:05,000 --> 00:00:06,000\nnew\n"
ASS = b"[Script Info]\nScriptType: v4.00+\nDialogue: 0,0:00:01.00,0:00:02.00,D,,0,0,0,,hi\n"


class Source:
    name = "fake"

    def __init__(self) -> None:
        self.downloads = 0
        self.payload = NEW

    def supports(self, _lang: str) -> bool:
        return True

    async def search(self, _query: Any) -> list[SubtitleResult]:
        return [self.result()]

    def result(self) -> SubtitleResult:
        return SubtitleResult(
            source="fake",
            lang="en",
            name="Better.Release",
            score=1,
            ref={"link": "https://internal.example/secret-download"},
        )

    async def download(self, _result: SubtitleResult) -> bytes:
        self.downloads += 1
        return self.payload


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> Source:
    fake = Source()
    monkeypatch.setattr(review, "_build_sources", lambda _c, _h: [fake])
    monkeypatch.setattr(review, "_require_sources", lambda _c: None)
    return fake


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from homeTheater.api import create_app
    from homeTheater.config import loader
    from homeTheater.db import init_db
    from homeTheater.db import session as db_session

    (tmp_path / "subs.yaml").write_text(
        "nas: {share: T, movies_root: Movies, tv_root: TV Shows}\n"
        f"database: {{url: 'sqlite:///{tmp_path / 'subs.db'}'}}\n"
        "subtitles: {backend: native, sources: [opensubtitles], languages: [en],"
        " sync: {enabled: true}}\n"
    )
    monkeypatch.setenv("HOME_THEATER_CONFIG", str(tmp_path / "subs.yaml"))
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()
    with TestClient(create_app()) as c:
        yield c


def _add(tmp_path: Path, *, state: str = "pending", corrected: bool = False) -> tuple[int, Path]:
    from homeTheater.db import session_scope

    folder = tmp_path / "lib"
    (folder / "Subs").mkdir(parents=True)
    media = folder / "Film.mkv"
    media.write_bytes(b"m" * 1000)
    path = folder / "Subs" / "Film.en.srt"
    path.write_bytes(RETIMED if corrected else OLD)
    backup = None
    if corrected:
        backup = str(path) + ".orig"
        Path(backup).write_bytes(OLD)
    with session_scope() as s:
        title = Title(title="Film", kind=TitleKind.movie)
        s.add(title)
        s.flush()
        owned = OwnedFile(title_id=title.id, path=str(media), kind=TitleKind.movie)
        s.add(owned)
        s.flush()
        s.add(
            Subtitle(
                title_id=title.id,
                owned_file_id=owned.id,
                lang="en",
                provider="ktuvit",
                path=str(path),
                status="downloaded",
            )
        )
        s.add(
            SubtitleSync(
                owned_file_id=owned.id,
                lang="en",
                state=state,
                source="ktuvit",
                release="Old.Release",
                result_key="k" * 64,
                hash_match=False,
                fail_count=0,
                offset_seconds=2.0 if corrected else None,
                original_path=backup,
                original_source="ktuvit" if corrected else None,
                original_release="Old.Release" if corrected else None,
                original_key="k" * 64 if corrected else None,
            )
        )
        return owned.id, path


def _row(owned_id: int) -> tuple[SubtitleSync, Subtitle]:
    from homeTheater.db import session_scope

    with session_scope() as s:
        sync = s.query(SubtitleSync).filter_by(owned_file_id=owned_id).one()
        sub = s.query(Subtitle).filter_by(owned_file_id=owned_id).one()
        s.expunge_all()
        return sync, sub


def _rejects() -> int:
    from homeTheater.db import session_scope

    with session_scope() as s:
        return s.query(SubtitleReject).count()


def test_review_lists_what_wants_a_look_with_its_source(client: Any, tmp_path: Path) -> None:
    _add(tmp_path, state="corrected", corrected=True)

    body = client.get("/api/subtitles/review").json()

    item = body["items"][0]
    assert body["enabled"] is True
    assert item["file"] == "Film.mkv" and item["source"] == "ktuvit"
    assert item["release"] == "Old.Release" and item["state"] == "corrected"
    assert item["offset_seconds"] == 2.0 and item["can_revert"] is True


def test_review_default_hides_settled_rows_but_all_shows_them(client: Any, tmp_path: Path) -> None:
    _add(tmp_path, state="in_sync")

    assert client.get("/api/subtitles/review").json()["items"] == []
    assert len(client.get("/api/subtitles/review?state=all").json()["items"]) == 1
    assert client.get("/api/subtitles/review?state=all&q=nothing").json()["items"] == []


def test_review_reports_why_the_check_cannot_run(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.subtitles import sync

    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    note = client.get("/api/subtitles/review").json()["note"]
    assert "ffsubsync and ffmpeg not found on PATH" in note


@pytest.mark.parametrize(
    "url", ["approve", "revert", "candidates", "swap"], ids=lambda u: f"{u}-needs-token"
)
def test_mutating_routes_are_gated(client: Any, tmp_path: Path, url: str) -> None:
    owned_id, path = _add(tmp_path, state="corrected", corrected=True)

    response = client.post(f"/api/subtitles/files/{owned_id}/en/{url}", json={"candidate": "x"})

    assert response.status_code == 401
    assert path.read_bytes() == RETIMED


def test_sync_check_is_gated(client: Any) -> None:
    assert client.post("/api/subtitles/sync-check").status_code == 401


def test_sync_check_without_the_tools_is_503(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.subtitles import sync

    monkeypatch.setattr(sync.shutil, "which", lambda _name: None)
    response = client.post("/api/subtitles/sync-check", headers=HEADERS)
    assert response.status_code == 503 and "not found on PATH" in response.json()["detail"]


def test_approve_settles_the_row(client: Any, tmp_path: Path) -> None:
    owned_id, path = _add(tmp_path, state="corrected", corrected=True)

    assert client.post(f"/api/subtitles/files/{owned_id}/en/approve", headers=HEADERS).is_success

    assert _row(owned_id)[0].state == "approved"
    assert path.read_bytes() == RETIMED


def test_unknown_file_is_404_and_a_bad_lang_is_refused(client: Any) -> None:
    assert client.post("/api/subtitles/files/99/en/approve", headers=HEADERS).status_code == 404
    assert client.post("/api/subtitles/files/99/EN!/approve", headers=HEADERS).status_code == 422


def test_revert_restores_the_original_and_keeps_the_backup(client: Any, tmp_path: Path) -> None:
    owned_id, path = _add(tmp_path, state="corrected", corrected=True)

    assert client.post(f"/api/subtitles/files/{owned_id}/en/revert", headers=HEADERS).is_success

    assert path.read_bytes() == OLD
    assert Path(str(path) + ".orig").read_bytes() == OLD
    sync, _ = _row(owned_id)
    assert sync.state == "reverted" and sync.offset_seconds is None


def test_revert_with_no_original_is_a_conflict(client: Any, tmp_path: Path) -> None:
    owned_id, path = _add(tmp_path)

    response = client.post(f"/api/subtitles/files/{owned_id}/en/revert", headers=HEADERS)

    assert response.status_code == 409 and path.read_bytes() == OLD


def test_revert_refuses_a_ruined_backup(client: Any, tmp_path: Path) -> None:
    owned_id, path = _add(tmp_path, state="corrected", corrected=True)
    Path(str(path) + ".orig").write_bytes(b"<html>nope</html>")

    response = client.post(f"/api/subtitles/files/{owned_id}/en/revert", headers=HEADERS)

    assert response.status_code == 409 and path.read_bytes() == RETIMED


def test_writes_wait_for_an_import(client: Any, tmp_path: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import import_active

    owned_id, path = _add(tmp_path, state="corrected", corrected=True)

    with import_active(get_config()):
        response = client.post(f"/api/subtitles/files/{owned_id}/en/revert", headers=HEADERS)

    assert response.status_code == 409 and path.read_bytes() == RETIMED


def test_a_running_check_blocks_changes(client: Any, tmp_path: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock
    from homeTheater.subtitles.sync import SYNC_LOCK

    owned_id, _ = _add(tmp_path)

    with job_lock(get_config(), SYNC_LOCK):
        response = client.post(f"/api/subtitles/files/{owned_id}/en/approve", headers=HEADERS)

    assert response.status_code == 409


def test_candidates_never_show_the_browser_a_provider_ref(
    client: Any, tmp_path: Path, source: Source
) -> None:
    owned_id, _ = _add(tmp_path)

    response = client.post(f"/api/subtitles/files/{owned_id}/en/candidates", headers=HEADERS)

    assert response.status_code == 200
    assert "internal.example" not in response.text
    assert response.json()["items"][0]["id"] == result_key(source.result())


def test_candidates_leave_out_what_was_rejected(
    client: Any, tmp_path: Path, source: Source
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.db.base import utcnow

    owned_id, _ = _add(tmp_path)
    with session_scope() as s:
        s.add(
            SubtitleReject(
                owned_file_id=owned_id,
                lang="en",
                result_key=result_key(source.result()),
                rejected_at=utcnow(),
            )
        )

    url = f"/api/subtitles/files/{owned_id}/en/candidates"
    assert client.post(url, headers=HEADERS).json()["items"] == []


def _swap(client: Any, owned_id: int, candidate: str) -> Any:
    return client.post(
        f"/api/subtitles/files/{owned_id}/en/swap", headers=HEADERS, json={"candidate": candidate}
    )


def test_swap_places_the_candidate_and_rejects_the_old_one(
    client: Any, tmp_path: Path, source: Source
) -> None:
    owned_id, path = _add(tmp_path)

    response = _swap(client, owned_id, result_key(source.result()))

    assert response.status_code == 200
    assert path.read_bytes() == NEW
    assert Path(str(path) + ".orig").read_bytes() == OLD
    sync, sub = _row(owned_id)
    assert sub.provider == "fake" and sync.source == "fake"
    assert sync.release == "Better.Release" and sync.state == "pending"
    assert sync.original_source == "ktuvit"
    assert _rejects() == 1


def test_swap_of_a_corrected_subtitle_rejects_what_the_provider_actually_serves(
    client: Any, tmp_path: Path, source: Source
) -> None:
    from homeTheater.db import session_scope
    from homeTheater.subtitles.choices import content_hash

    owned_id, _ = _add(tmp_path, state="corrected", corrected=True)

    assert _swap(client, owned_id, result_key(source.result())).status_code == 200

    with session_scope() as s:
        hashes = {r.content_sha256 for r in s.query(SubtitleReject)}
    assert content_hash(OLD) in hashes and content_hash(RETIMED) in hashes


def test_swap_only_takes_ids_the_server_just_offered(
    client: Any, tmp_path: Path, source: Source
) -> None:
    owned_id, path = _add(tmp_path)

    response = _swap(client, owned_id, "https://evil.example/x.srt")

    assert response.status_code == 409
    assert source.downloads == 0 and path.read_bytes() == OLD


def test_swap_refuses_an_invalid_download(client: Any, tmp_path: Path, source: Source) -> None:
    owned_id, path = _add(tmp_path)
    source.payload = b"<html>quota</html>"

    response = _swap(client, owned_id, result_key(source.result()))

    assert response.status_code == 409
    assert path.read_bytes() == OLD and _rejects() == 0


def test_swap_to_another_format_leaves_one_subtitle_and_revert_undoes_it(
    client: Any, tmp_path: Path, source: Source
) -> None:
    owned_id, path = _add(tmp_path)
    source.payload = ASS

    assert _swap(client, owned_id, result_key(source.result())).status_code == 200
    ass = path.with_suffix(".ass")
    assert ass.exists() and not path.exists()

    assert client.post(f"/api/subtitles/files/{owned_id}/en/revert", headers=HEADERS).is_success

    assert path.read_bytes() == OLD and not ass.exists()
    sync, sub = _row(owned_id)
    assert sub.path == str(path) and sub.provider == "ktuvit"
    assert sync.source == "ktuvit" and sync.release == "Old.Release"
    assert _rejects() == 0  # going back takes the rejection back


def test_page_has_the_review_hooks(client: Any) -> None:
    html = client.get("/subtitles").text
    assert 'id="sub-review"' in html and 'data-action="sub-sync-now"' in html
    assert "<script>" not in html.split('<script src="/static/app.js')[0]
