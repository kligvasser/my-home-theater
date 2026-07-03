"""Health/readiness endpoints boot against a real (temp) SQLite DB."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from homeTheater.api import create_app


def test_health_and_ready(config_file: Path) -> None:
    # Reset cached config/engine so the test's temp DB is used.
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None

    with TestClient(create_app()) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        r = client.get("/ready")
        assert r.status_code == 200
        assert r.json()["database"] == "ok"
