"""Preference classifier: training, prediction, explanation, API + discovery blend."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homeTheater.db.models import CandidateSource, CandidateStatus, TitleKind

TOKEN = {"X-Auth-Token": "test-token"}


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _scifi(i: int) -> dict:
    return {
        "kind": "movie",
        "genres": ["Science Fiction", "Thriller"],
        "keywords": ["dystopia", f"tag{i % 3}"],
        "original_language": "en",
        "decade": 2010,
        "imdb_rating": 7.5 + (i % 3) * 0.3,
        "imdb_votes": 100_000,
        "runtime": 120,
    }


def _romcom(i: int) -> dict:
    return {
        "kind": "movie",
        "genres": ["Romance", "Comedy"],
        "keywords": ["christmas", f"tag{i % 3}"],
        "original_language": "en",
        "decade": 2000,
        "imdb_rating": 7.0 + (i % 3) * 0.3,
        "imdb_votes": 90_000,
        "runtime": 100,
    }


def _seed_decisions(n_each: int = 15) -> None:
    """One shared title; the classifier only reads Candidate.features/status."""

    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=1, title="anchor", kind=TitleKind.movie)
        s.add(t)
        s.flush()
        for i in range(n_each):
            s.add(
                Candidate(
                    title_id=t.id,
                    source=CandidateSource.discovery,
                    status=CandidateStatus.approved,
                    features=_scifi(i),
                )
            )
            s.add(
                Candidate(
                    title_id=t.id,
                    source=CandidateSource.discovery,
                    status=CandidateStatus.rejected,
                    features=_romcom(i),
                )
            )


def test_train_refuses_without_labels(config_file: Path) -> None:
    _reset()
    from homeTheater.config import get_config
    from homeTheater.db import init_db
    from homeTheater.preferences import predict, train

    init_db()
    stats = train(get_config(), bootstrap=False)
    assert not stats.trained and "not enough decisions" in stats.message
    assert predict(get_config(), _scifi(0)) is None  # no model file


def test_train_learns_the_preference(config_file: Path) -> None:
    _reset()
    _seed_decisions()
    from homeTheater.config import get_config
    from homeTheater.preferences import model_info, predict, train

    cfg = get_config()
    stats = train(cfg, bootstrap=False)
    assert stats.trained and stats.n_positive == 15 and stats.n_negative == 15
    assert stats.auc is not None and stats.auc > 0.9  # cleanly separable

    p_scifi = predict(cfg, _scifi(99))
    p_romcom = predict(cfg, _romcom(99))
    assert p_scifi is not None and p_romcom is not None
    assert p_scifi > 0.7 > 0.3 > p_romcom

    info = model_info(cfg)
    assert info is not None and info.n_positive == 15
    positive_tokens = [tok for tok, _ in info.top_positive]
    assert any("science fiction" in tok for tok in positive_tokens)


def test_train_api_gated_and_reports(config_file: Path) -> None:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as client:
        assert client.post("/api/preferences/train").status_code == 401
        r = client.post("/api/preferences/train", headers=TOKEN)
        assert r.status_code == 200
        assert r.json()["trained"] is False  # empty DB -> no labels

        page = client.get("/insights")
        assert page.status_code == 200 and "Preference model" in page.text
