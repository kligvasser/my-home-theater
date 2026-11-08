"""L7: SMBFileSystem surviving sleep, dropped sessions and flaky ``.local`` names."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from homeTheater.scanner import filesystem
from homeTheater.scanner.filesystem import (
    SMBFileSystem,
    WalkAbortedError,
    abort_active_walks,
    is_transient_smb_error,
)

smbclient = pytest.importorskip("smbclient")
exceptions = pytest.importorskip("smbprotocol.exceptions")


class FakeSmb:
    """The smbclient surface the walker uses, on one fake ``Movies`` tree."""

    def __init__(self, server: str = "nas") -> None:
        self.server = server
        self.resets: list[Any] = []
        self.registers: list[tuple[str, dict[str, Any]]] = []
        self.walk_calls: list[tuple[str, dict[str, Any]]] = []
        self.stat_calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: list[BaseException] = []

    def tree(self, top: str) -> list[tuple[str, list[str], list[str]]]:
        return [
            (top, ["Film (2001)", "@eaDir"], []),
            (f"{top}\\Film (2001)", [], ["Film (2001).mkv", "._Film.mkv"]),
        ]

    def walk(self, top: str, onerror: Any = None, **kwargs: Any) -> Iterator[Any]:
        self.walk_calls.append((top, kwargs))
        for dirpath, dirnames, filenames in self.tree(top):
            if self.failures:
                raise self.failures.pop(0)
            yield dirpath, dirnames, filenames

    def stat(self, path: str, **kwargs: Any) -> SimpleNamespace:
        self.stat_calls.append((path, kwargs))
        return SimpleNamespace(st_size=7)

    def register(self, server: str, **kwargs: Any) -> None:
        self.registers.append((server, kwargs))

    def reset(self, fail_on_error: bool = True, connection_cache: Any = None) -> None:
        self.resets.append(connection_cache)


@pytest.fixture
def smb(monkeypatch: pytest.MonkeyPatch) -> FakeSmb:
    fake = FakeSmb()
    monkeypatch.setattr(smbclient, "walk", fake.walk)
    monkeypatch.setattr(smbclient, "stat", fake.stat)
    monkeypatch.setattr(smbclient, "register_session", fake.register)
    monkeypatch.setattr(smbclient, "reset_connection_cache", fake.reset)
    monkeypatch.setattr(filesystem, "_last_resolved", {})
    return fake


def _fs(host: str = "nas", **kwargs: Any) -> SMBFileSystem:
    kwargs.setdefault("retry_delays", (0.0, 0.0))
    return SMBFileSystem(host, "share", "idan", "pw", **kwargs)


def test_every_walk_starts_on_a_fresh_pool_and_session(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection pooled before a sleep is dead; `SMB socket was closed` was 66 of
    the 124 failed scans. The old `_registered` flag also skipped re-registering."""

    resets_when_registering: list[int] = []
    register = smb.register

    def counting_register(server: str, **kwargs: Any) -> None:
        resets_when_registering.append(len(smb.resets))
        register(server, **kwargs)

    monkeypatch.setattr(smbclient, "register_session", counting_register)
    fs = _fs()
    list(fs.walk("Movies"))
    resets_after_first = len(smb.resets)
    list(fs.walk("Movies"))
    assert resets_when_registering == [1, resets_after_first + 1]  # reset, THEN connect


def test_credentials_go_on_every_call(smb: FakeSmb) -> None:
    """A call that reconnects has only its own arguments: without them, a dropped
    session came back as `SpnegoError: No username or password` (36 failed scans)."""

    list(_fs().walk("Movies"))
    assert smb.walk_calls and smb.stat_calls
    for _, kwargs in smb.walk_calls + smb.stat_calls:
        assert kwargs["username"] == "idan" and kwargs["password"] == "pw"


def test_guest_calls_keep_signing_off(smb: FakeSmb) -> None:
    """A reconnecting call defaults to require_signing=True, which WD guest shares
    refuse, so the relaxed setting has to ride along too."""

    list(SMBFileSystem("nas", "share", retry_delays=()).walk("Movies"))
    assert smb.registers[0][1]["require_signing"] is False
    for _, kwargs in smb.walk_calls + smb.stat_calls:
        assert kwargs["username"] == "guest" and kwargs["require_signing"] is False


def test_the_pool_is_private_to_the_walk(smb: FakeSmb) -> None:
    """Resetting the global pool per walk would cut an SMB import on another thread."""

    fs = _fs()
    list(fs.walk("Movies"))
    assert all(cache is fs._pool for cache in smb.resets)
    assert smb.walk_calls[0][1]["connection_cache"] is fs._pool


def test_local_name_dials_the_resolved_address_but_keys_paths_on_the_name(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OwnedFile.path is keyed on the hostname. If an IP leaked in, every row in the
    catalog would look deleted."""

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.0.0.9", 445))])
    fs = _fs("MyCloud.local")
    entries = list(fs.walk("Movies"))
    assert smb.registers[0][0] == "10.0.0.9"
    assert smb.walk_calls[0][0].startswith("\\\\10.0.0.9\\share\\Movies")
    assert smb.stat_calls[0][0].startswith("\\\\10.0.0.9\\")

    root = fs.resolve("Movies")
    assert root == "\\\\MyCloud.local\\share\\Movies"
    assert [e.path for e in entries] == [f"{root}\\Film (2001)\\Film (2001).mkv"]
    assert entries[0].parent == f"{root}\\Film (2001)"


def test_configured_connect_host_skips_resolution(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_dns(*a: object, **k: object) -> None:
        raise AssertionError("resolved a host that has a configured address")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    entries = list(_fs("MyCloud.local", connect_host="10.0.0.9").walk("Movies"))
    assert smb.registers[0][0] == "10.0.0.9"
    assert entries[0].path.startswith("\\\\MyCloud.local\\")


def test_dns_failure_falls_back_to_the_last_good_address(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers: list[Any] = [
        [(2, 1, 6, "", ("10.0.0.9", 445))],
        socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided"),
    ]

    def dns(*a: object, **k: object) -> Any:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    list(_fs("MyCloud.local").walk("Movies"))
    list(_fs("MyCloud.local").walk("Movies"))  # new object, as every scan builds one
    assert [server for server, _ in smb.registers] == ["10.0.0.9", "10.0.0.9"]


def test_dns_failure_with_no_known_address_fails_after_retrying(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def dns(*a: object, **k: object) -> None:
        nonlocal calls
        calls += 1
        raise socket.gaierror(socket.EAI_NONAME, "unknown host")

    monkeypatch.setattr(socket, "getaddrinfo", dns)
    with pytest.raises(socket.gaierror):
        list(_fs("MyCloud.local").walk("Movies"))
    assert calls == 3


@pytest.mark.parametrize(
    "failure",
    [
        exceptions.SMBConnectionClosed("SMB socket was closed"),
        exceptions.SMBException("Request requires 1 credits but only 0 credits are available"),
        ValueError("Failed to connect to 'nas:445': timed out"),
        socket.gaierror(8, "nodename nor servname provided"),
        Exception("SpnegoError (1): No username or password was specified"),
        ConnectionResetError(54, "Connection reset by peer"),
        TimeoutError(60, "Operation timed out"),
    ],
)
def test_transient_failures_retry_and_yield_each_file_once(
    smb: FakeSmb, failure: BaseException
) -> None:
    smb.failures = [failure]
    entries = list(_fs().walk("Movies"))
    assert len(entries) == 1  # a partial first pass mustn't double anything
    assert len(smb.registers) == 2  # the retry reconnected


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError(13, "Permission denied"),
        FileNotFoundError(2, "No such file"),
        OSError(5, "Input/output error"),
        KeyError("bug"),
    ],
)
def test_permanent_failures_raise_at_once(smb: FakeSmb, failure: BaseException) -> None:
    assert not is_transient_smb_error(failure)
    smb.failures = [failure]
    with pytest.raises(type(failure)):
        list(_fs().walk("Movies"))
    assert len(smb.registers) == 1


def test_exhausted_retries_fail_the_scan_and_keep_every_row(
    config_file: Path, smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L1 must still hold: however the walk fails, it isn't "the files are gone"."""

    from homeTheater.config import loader
    from homeTheater.db import init_db, session_scope
    from homeTheater.db import session as db_session
    from homeTheater.db.models import OwnedFile, Title, TitleKind
    from homeTheater.scanner import scan_library

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None
    init_db()
    path = "\\\\nas\\share\\Movies\\Film (2001)\\Film (2001).mkv"
    with session_scope() as s:
        title = Title(title="Film", year=2001, kind=TitleKind.movie)
        title.owned_files = [OwnedFile(path=path, kind=TitleKind.movie)]
        s.add(title)

    smb.failures = [exceptions.SMBConnectionClosed("SMB socket was closed") for _ in range(3)]
    with pytest.raises(RuntimeError, match="Scan failed"):
        scan_library(_fs(), {TitleKind.movie: "Movies"})
    with session_scope() as s:
        assert s.query(OwnedFile).filter_by(path=path).count() == 1


def test_abort_stops_a_walk_blocked_in_a_socket_read(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung read never returns to check a flag. Closing the pool is what wakes it,
    and the resulting socket error must not be retried."""

    in_read = threading.Event()
    closed = threading.Event()

    def blocking_walk(top: str, onerror: Any = None, **kwargs: Any) -> Iterator[Any]:
        in_read.set()
        assert closed.wait(5)
        raise exceptions.SMBConnectionClosed("SMB socket was closed")
        yield

    def reset(fail_on_error: bool = True, connection_cache: Any = None) -> None:
        if in_read.is_set():
            closed.set()

    monkeypatch.setattr(smbclient, "walk", blocking_walk)
    monkeypatch.setattr(smbclient, "reset_connection_cache", reset)
    fs = _fs(retry_delays=())  # no retries left: only the abort can explain this
    errors: list[BaseException] = []

    def scan() -> None:
        try:
            list(fs.walk("Movies"))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    worker = threading.Thread(target=scan, daemon=True)
    worker.start()
    assert in_read.wait(5)
    abort_active_walks()
    worker.join(5)
    assert not worker.is_alive()
    assert [type(e) for e in errors] == [WalkAbortedError]


def test_finished_walks_are_not_aborted(smb: FakeSmb) -> None:
    fs = _fs()
    list(fs.walk("Movies"))
    abort_active_walks()
    assert len(list(fs.walk("Movies"))) == 1


def test_walk_timeout_aborts_a_hung_walk_for_manual_scans(
    smb: FakeSmb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manual scans have no job timeout, so the walk polices itself."""

    release = threading.Event()
    fs = _fs(walk_timeout=0.05)

    def hung(top: str, onerror: Any = None, **kwargs: Any) -> Iterator[Any]:
        release.wait(5)
        raise exceptions.SMBConnectionClosed("SMB socket was closed")
        yield

    def reset(fail_on_error: bool = True, connection_cache: Any = None) -> None:
        if fs._aborted.is_set():
            release.set()

    monkeypatch.setattr(smbclient, "walk", hung)
    monkeypatch.setattr(smbclient, "reset_connection_cache", reset)
    with pytest.raises(WalkAbortedError):
        list(fs.walk("Movies"))


def test_build_filesystem_separates_the_path_host_from_the_connect_host(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import loader
    from homeTheater.scanner import build_filesystem

    with config_file.open("a") as f:
        f.write("scanner:\n  walk_timeout_minutes: 2\n")
    text = config_file.read_text().replace(
        "  share: TestShare\n", "  share: TestShare\n  connect_host: 10.0.0.9\n"
    )
    config_file.write_text(text)
    monkeypatch.setenv("SMB_HOST", "MyCloud.local")
    loader.get_config.cache_clear()
    fs = build_filesystem(loader.get_config())
    assert isinstance(fs, SMBFileSystem)
    assert fs.host == "MyCloud.local" and fs._connect_host == "10.0.0.9"
    assert fs._walk_timeout == 120
