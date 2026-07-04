"""Runtime overrides + interactive dashboard APIs (settings, delete, search)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from homeTheater.db.models import TitleKind

TOKEN = {"X-Auth-Token": "test-token"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def test_overrides_roundtrip_and_effective(config_file: Path) -> None:
    _reset()
    from homeTheater.config import effective_config, load_overrides, save_overrides
    from homeTheater.db import init_db

    init_db()
    assert load_overrides() == {}
    assert effective_config().thresholds.min_imdb_rating == 7.0

    save_overrides(
        {
            "thresholds": {"min_imdb_rating": 6.5, "series": {"min_imdb_votes": 1234}},
            "features": {"auto_approve": True},
        }
    )
    eff = effective_config()
    assert eff.thresholds.min_imdb_rating == 6.5
    assert eff.thresholds.for_kind("series").min_imdb_votes == 1234
    assert eff.thresholds.for_kind("movie").min_imdb_votes == 25_000  # untouched
    assert eff.features.auto_approve is True
    assert eff.features.dry_run is True  # file value, not overridable

    save_overrides({})  # reset
    assert effective_config().thresholds.min_imdb_rating == 7.0


def test_overrides_reject_forbidden_and_invalid(config_file: Path) -> None:
    _reset()
    from homeTheater.config import OverrideError, save_overrides
    from homeTheater.db import init_db

    init_db()
    with pytest.raises(OverrideError, match="cannot be overridden"):
        save_overrides({"database": {"url": "sqlite:///pwned.db"}})
    with pytest.raises(OverrideError, match="features.dry_run"):
        save_overrides({"features": {"dry_run": False}})
    with pytest.raises(OverrideError, match="invalid override values"):
        save_overrides({"thresholds": {"min_imdb_rating": 42}})  # > 10


def test_settings_api(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        r = client.get("/api/settings")  # read is open
        assert r.status_code == 200
        assert r.json()["effective"]["thresholds"]["min_imdb_rating"] == 7.0

        body = {"thresholds": {"min_imdb_rating": 6.0}}
        assert client.put("/api/settings", json=body).status_code == 401  # gated
        r = client.put("/api/settings", json=body, headers=TOKEN)
        assert r.status_code == 200
        assert r.json()["effective"]["thresholds"]["min_imdb_rating"] == 6.0
        assert r.json()["overrides"] == {"thresholds": {"min_imdb_rating": 6.0}}

        r = client.put("/api/settings", json={"schedule": {"enabled": True}}, headers=TOKEN)
        assert r.status_code == 422

        # settings page renders with the override marked
        page = client.get("/settings")
        assert page.status_code == 200 and "Save overrides" in page.text


def test_delete_title_api(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import (
        Candidate,
        CandidateSource,
        Download,
        OwnedFile,
        Subtitle,
        Title,
    )

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie)
        t.owned_files = [OwnedFile(path="/m.mkv", kind=TitleKind.movie)]
        s.add(t)
        s.flush()
        c = Candidate(title_id=t.id, source=CandidateSource.discovery)
        s.add(c)
        s.flush()
        s.add(Download(candidate_id=c.id, external_id="42"))
        s.add(Subtitle(title_id=t.id, lang="he"))
        tid = t.id

    with TestClient(create_app()) as client:
        assert client.delete(f"/api/titles/{tid}").status_code == 401  # gated
        r = client.delete(f"/api/titles/{tid}", headers=TOKEN)
        assert r.status_code == 200 and r.json()["title"] == "The Matrix"
        assert client.delete(f"/api/titles/{tid}", headers=TOKEN).status_code == 404

    with session_scope() as s:
        for model in (Title, OwnedFile, Candidate, Download, Subtitle):
            assert s.query(model).count() == 0, model.__name__


def test_search_and_discover_endpoints(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _reset()
    monkeypatch.setenv("TMDB_API_KEY", "k")
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        assert client.get("/api/candidates/search?q=matrix").status_code == 401  # gated

        with respx.mock:
            respx.get("https://api.themoviedb.org/3/search/movie").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "id": 603,
                                "title": "The Matrix",
                                "release_date": "1999-03-30",
                                "vote_average": 8.2,
                                "poster_path": "/m.jpg",
                            }
                        ]
                    },
                )
            )
            r = client.get("/api/candidates/search?q=matrix", headers=TOKEN)
        assert r.status_code == 200
        items = r.json()["items"]
        assert items[0]["tmdb_id"] == 603 and items[0]["year"] == 1999

        # discover: gated; runs run_discovery in the background with the boost
        calls: list[int] = []

        async def fake_run(cfg):  # noqa: ANN001
            calls.append(cfg.discovery.max_per_source)

        import homeTheater.discovery as discovery_mod

        monkeypatch.setattr(discovery_mod, "run_discovery", fake_run)
        assert client.post("/api/candidates/discover", json={}).status_code == 401
        r = client.post("/api/candidates/discover", json={"max_per_source": 50}, headers=TOKEN)
        assert r.status_code == 200 and r.json() == {"started": True, "max_per_source": 50}
        assert calls == [50]  # TestClient runs background tasks before returning


def test_library_sort(config_file: Path) -> None:
    _reset()
    from homeTheater.dashboard import list_titles
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Title

    init_db()
    with session_scope() as s:
        s.add(Title(title="Old", year=1990, kind=TitleKind.movie, imdb_rating=9.0))
    with session_scope() as s:
        s.add(Title(title="New", year=2024, kind=TitleKind.movie, imdb_rating=6.0))

    by_added, _ = list_titles(sort="added")
    assert [t.title for t in by_added] == ["New", "Old"]  # newest catalog entry first
    by_rating, _ = list_titles(sort="rating")
    assert [t.title for t in by_rating] == ["Old", "New"]
    by_title, _ = list_titles(sort="title")
    assert [t.title for t in by_title] == ["New", "Old"]


def test_candidate_sort_and_kind_filter(config_file: Path) -> None:
    _reset()
    from homeTheater.dashboard import list_candidates
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, Title

    init_db()
    with session_scope() as s:
        movie = Title(
            tmdb_id=1, title="Old Movie", year=1999, kind=TitleKind.movie, imdb_rating=9.0
        )
        series = Title(
            tmdb_id=1, title="New Series", year=2024, kind=TitleKind.series, imdb_rating=7.0
        )
        s.add_all([movie, series])
        s.flush()
        s.add(
            Candidate(
                title_id=movie.id,
                source=CandidateSource.discovery,
                score=10.0,
                features={"taste": {"score": 0.2, "like": []}},
            )
        )
        s.add(
            Candidate(
                title_id=series.id,
                source=CandidateSource.discovery,
                score=20.0,
                features={"taste": {"score": 0.9, "like": []}},
            )
        )

    def titles(**kw: object) -> list[str]:
        rows, _ = list_candidates(**kw)  # type: ignore[arg-type]
        return [c.title for c in rows]

    assert titles(sort="score") == ["New Series", "Old Movie"]
    assert titles(sort="year") == ["New Series", "Old Movie"]
    assert titles(sort="rating") == ["Old Movie", "New Series"]
    assert titles(sort="taste") == ["New Series", "Old Movie"]
    assert titles(kind="movie") == ["Old Movie"]
    assert titles(kind="series", sort="year") == ["New Series"]
