"""W11/W12/W14/W18/W19: what the HTTP surface refuses, hides and reports honestly."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from homeTheater.api import security
from homeTheater.config.settings import Server

TOKEN = {"X-Auth-Token": "test-token"}
WEB_DIR = Path(security.__file__).resolve().parent.parent / "web"


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


@pytest.fixture
def client(config_file: Path) -> Iterator[TestClient]:
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as c:
        yield c


# --- W14: Host allowlist -----------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "localhost:8000",
        "LOCALHOST:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
        "kligvassers-macbook-air.local:8000",
        "192.168.1.20:8000",  # a phone on the LAN
        "10.168.168.40:8000",  # the owner's actual subnet
        "172.20.0.3",
        "169.254.1.1",
        "100.101.102.103:8000",  # Tailscale
    ],
)
def test_default_allowlist_does_not_lock_the_owner_out(client: TestClient, host: str) -> None:
    assert client.get("/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host", ["evil.example", "evil.example:8000", "localhost.evil.example", "8.8.8.8", "local"]
)
def test_foreign_hosts_are_refused(client: TestClient, host: str) -> None:
    """A DNS-rebinding page reaches the open port under its own domain name."""

    r = client.get("/", headers={"Host": host})
    assert r.status_code == 400 and "server.allowed_hosts" in r.text


def test_refused_host_cannot_reach_the_api(client: TestClient) -> None:
    r = client.put("/api/settings", json={}, headers={**TOKEN, "Host": "evil.example"})
    assert r.status_code == 400


def test_machine_hostname_is_allowed_however_it_is_cased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(security.socket, "gethostname", lambda: "kligvassers-Air")
    security.machine_names.cache_clear()
    try:
        monkeypatch.delenv(security.EXTRA_HOSTS_ENV)
        assert security.host_allowed("kligvassers-air", Server(allowed_hosts=[]))
        assert security.host_allowed("kligvassers-air.local", Server(allowed_hosts=[]))
    finally:
        security.machine_names.cache_clear()


def test_allowlist_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(security.EXTRA_HOSTS_ENV)
    custom = Server(allowed_hosts=["theater.home", "*.ts.net"])
    assert security.host_allowed("theater.home", custom)
    assert security.host_allowed("mac.tail1.ts.net", custom)
    assert not security.host_allowed("evil.net", custom)
    assert not security.host_allowed("", custom)

    no_lan = Server(allow_private_ips=False)
    assert not security.host_allowed("192.168.1.20", no_lan)
    assert security.host_allowed("127.0.0.1", no_lan)  # still on the list by name

    assert security.host_allowed("anything.example", Server(allowed_hosts=["*"]))

    monkeypatch.setenv(security.EXTRA_HOSTS_ENV, "theater.home, other.home")
    assert security.host_allowed("other.home", Server())


# --- W14: headers ------------------------------------------------------------


def test_pages_carry_the_security_headers(client: TestClient) -> None:
    for path in ("/", "/api/settings", "/health", "/static/app.js", "/no-such-page"):
        r = client.get(path)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"], path
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["x-frame-options"] == "DENY"


def _sources() -> list[Path]:
    return [*(WEB_DIR / "templates").glob("*.html"), WEB_DIR / "static" / "app.js"]


def test_the_policy_allows_everything_the_pages_use() -> None:
    """A CSP that blocks the app's own script is a dead dashboard. Script stays
    'self'-only, so inline script or handlers anywhere would silently stop working."""

    policy = security.CONTENT_SECURITY_POLICY
    assert "script-src 'self'" in policy and "unsafe-inline' script" not in policy
    assert re.search(r"style-src 'self'(;|$)", policy)
    assert "style-src-attr 'unsafe-inline'" in policy

    assert _sources()
    for path in _sources():
        text = path.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path
        assert not re.search(r"<style\b", text), path
        assert not re.search(r"\son[a-z]+\s*=\s*[\"']", text), path
        assert "javascript:" not in text, path
        assert not re.search(r"\beval\(|new Function\(", text), path
        for url in re.findall(r"(?:src|href)=\"(https?://[^\"]+)", text):
            raise AssertionError(f"{path}: loads {url}")


def test_style_sheet_needs_nothing_external() -> None:
    assert not re.search(
        r"@import|url\(\s*['\"]?https?:", (WEB_DIR / "static/style.css").read_text()
    )


def test_poster_host_is_the_one_the_policy_allows() -> None:
    from homeTheater.metadata.tmdb import IMAGE_BASE

    assert IMAGE_BASE.startswith("https://image.tmdb.org/")
    assert "https://image.tmdb.org" in security.CONTENT_SECURITY_POLICY


def test_docs_are_off_by_default(client: TestClient) -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_docs_can_be_switched_on(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME_THEATER_DOCS", "1")
    _reset()
    from homeTheater.api import create_app

    with TestClient(create_app()) as c:
        r = c.get("/docs")
    assert r.status_code == 200 and "content-security-policy" not in r.headers


def test_activity_feed_has_no_local_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class State:
        def as_dict(self) -> dict[str, Any]:
            return {
                "title": "Film",
                "save_path": "/Users/me/HomeTheaterDownloads",
                "downloads": [{"name": "x", "save_path": "/Users/me/x"}],
            }

    async def fake_activity(cfg: object) -> list[State]:
        return [State()]

    monkeypatch.setattr("homeTheater.api.pipeline.activity", fake_activity)
    r = client.get("/api/activity")
    assert r.status_code == 200 and "save_path" not in r.text and "/Users/" not in r.text
    assert r.json()["items"][0]["downloads"] == [{"name": "x"}]


# --- W18: triggers tell the truth --------------------------------------------


@pytest.mark.parametrize(
    ("path", "lock", "service"),
    [
        ("/api/pipeline/acquire-now", "acquire", "homeTheater.acquisition.queue_approved"),
        ("/api/pipeline/sync", "sync", "homeTheater.acquisition.sync_downloads"),
        ("/api/pipeline/scan", "scan", "homeTheater.scanner.run_scan"),
        ("/api/pipeline/enrich", "enrich", "homeTheater.metadata.service.enrich_catalog"),
    ],
)
def test_busy_trigger_says_so_and_starts_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, lock: str, service: str
) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock

    def must_not_run(*a: object, **k: object) -> None:
        raise AssertionError("started while busy")

    monkeypatch.setattr(service, must_not_run)
    with job_lock(get_config(), lock):
        r = client.post(path, headers=TOKEN)
    assert r.status_code == 200
    assert r.json()["started"] is False and "already running" in r.json()["message"]


def test_enrich_takes_its_lock_while_running(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import get_config
    from homeTheater.locks import is_job_running

    held: list[bool] = []

    async def fake_enrich(cfg: object) -> None:
        held.append(is_job_running(get_config(), "enrich"))

    monkeypatch.setattr("homeTheater.metadata.service.enrich_catalog", fake_enrich)
    r = client.post("/api/pipeline/enrich", headers=TOKEN)
    assert r.json() == {"started": True} and held == [True]


def test_enrich_cli_takes_the_same_lock(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater import main
    from homeTheater.config import get_config
    from homeTheater.locks import job_lock

    _reset()

    async def must_not_run(*a: object, **k: object) -> None:
        raise AssertionError("two enrich runs at once")

    monkeypatch.setattr("homeTheater.metadata.enrich_catalog", must_not_run)
    with job_lock(get_config(), "enrich"), pytest.raises(SystemExit) as stopped:
        main.enrich()
    assert stopped.value.code == 2


def test_manual_trigger_uses_dashboard_overrides(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import save_overrides

    save_overrides({"features": {"auto_approve": True}})
    seen: list[bool] = []

    async def fake_queue(cfg: Any) -> None:
        seen.append(cfg.features.auto_approve)

    monkeypatch.setattr("homeTheater.acquisition.queue_approved", fake_queue)
    client.post("/api/pipeline/acquire-now", headers=TOKEN)
    assert seen == [True]


def test_routes_read_effective_subtitle_languages(client: TestClient) -> None:
    from homeTheater.config import save_overrides
    from homeTheater.db import session_scope
    from homeTheater.db.models import OwnedFile, Title, TitleKind

    with session_scope() as s:
        title = Title(title="Film", year=2001, kind=TitleKind.movie)
        title.owned_files = [OwnedFile(path="/m/f.mkv", kind=TitleKind.movie)]
        s.add(title)
    save_overrides({"subtitles": {"languages": ["zz"]}})
    assert "zz" in client.get("/library").text
    assert "zz subs" in client.get("/status").text


# --- W19: startup refusal ----------------------------------------------------


def test_require_auth_without_a_token_refuses_to_start(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_REQUIRE_AUTH", "true")
    monkeypatch.delenv("DASHBOARD_TOKEN")
    _reset()
    from homeTheater.api import create_app
    from homeTheater.config import ConfigError

    with pytest.raises(ConfigError, match="DASHBOARD_TOKEN"), TestClient(create_app()):
        pass


def test_gate_fails_closed_if_startup_was_bypassed(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_REQUIRE_AUTH", "true")
    monkeypatch.delenv("DASHBOARD_TOKEN")
    _reset()
    from homeTheater.api import create_app

    client = TestClient(create_app())  # no `with`: lifespan doesn't run
    assert client.get("/library").status_code == 503


def test_busy_detail_gives_nothing_away(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.errors import JobBusyError

    async def busy(cfg: object) -> None:
        raise JobBusyError("sync is already running https://u:SEKRET@x/")

    monkeypatch.setattr("homeTheater.cleanup.plan_cleanup", busy)
    r = client.get("/api/cleanup", headers=TOKEN)
    assert r.status_code == 409 and "SEKRET" not in r.text


def test_job_lock_error_has_no_filesystem_path(config_file: Path) -> None:
    from homeTheater.config import get_config
    from homeTheater.errors import JobBusyError
    from homeTheater.locks import job_lock

    _reset()
    cfg = get_config()
    with job_lock(cfg, "sync"), pytest.raises(JobBusyError) as busy, job_lock(cfg, "sync"):
        pass
    assert "/" not in str(busy.value)


# --- W11/W12: /gaps ----------------------------------------------------------


@pytest.fixture
def gaps_client(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("TMDB_API_KEY", "sekret123")
    _reset()
    from homeTheater.api import create_app
    from homeTheater.db import init_db

    init_db()
    with TestClient(create_app()) as c:
        yield c


async def _instant_sleep(*a: object) -> None:
    return None


@pytest.mark.parametrize(
    "outcome",
    [httpx.Response(503), httpx.ConnectError("no route to api_key=sekret123")],
)
def test_gaps_survives_a_tmdb_outage(
    gaps_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: Any,
) -> None:
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.themoviedb\.org/.*").mock(side_effect=[outcome] * 9)
        r = gaps_client.get("/gaps")
    assert r.status_code == 502
    assert "TMDb is unavailable" in r.text and "Why not owned?" in r.text
    assert "sekret123" not in r.text
    assert "sekret123" not in capsys.readouterr().out


def test_gaps_page_shows_results_with_empty_decade(gaps_client: TestClient) -> None:
    """The "Any decade" option submits decade= and got a raw 422 JSON page."""

    payload = {
        "results": [
            {
                "id": 1,
                "title": "Film",
                "release_date": "1994-01-01",
                "vote_average": 9.0,
                "vote_count": 99999,
            }
        ]
    }
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://api\.themoviedb\.org/.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        any_decade = gaps_client.get("/gaps?kind=movie&min_rating=0&decade=&q=")
        nineties = gaps_client.get("/gaps?kind=movie&decade=1990")
        eighties = gaps_client.get("/gaps?kind=movie&decade=1980")
        garbage = gaps_client.get("/gaps?kind=movie&decade=abc")
    assert any_decade.status_code == 200 and "Film" in any_decade.text
    assert nineties.status_code == 200 and "Film" in nineties.text
    assert eighties.status_code == 200 and "Film" not in eighties.text
    assert garbage.status_code == 200 and "Film" in garbage.text


def test_conftest_scrubs_every_secret_env_var() -> None:
    """A new Secrets field that isn't scrubbed would read the developer's real value."""

    from homeTheater.config.settings import Secrets
    from tests.conftest import SECRET_ENV_VARS

    assert {f.upper() for f in Secrets.model_fields} <= set(SECRET_ENV_VARS)
