"""Scanning through the macOS SMB mount instead of SMB connections.

The catalog must not notice the switch: every path is the UNC string smbclient
would give. And the walk must fail closed (unreadable folder, wrong volume, hung
mount), keep secrets out of its child process, and never follow a symlink.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from homeTheater import nas_paths
from homeTheater.nas_paths import (
    MountPathError,
    decode_component,
    encode_component,
    mount_to_unc,
    require_no_redirect,
    unc_to_mount,
)
from homeTheater.scanner import _mountwalk, filesystem
from homeTheater.scanner._mountwalk import (
    MountInfo,
    NotMountedError,
    normalize_host,
    verify_smb_mount,
)
from homeTheater.scanner.filesystem import (
    MountedFileSystem,
    MountIdentityError,
    WalkAbortedError,
    abort_active_walks,
)

HOST, SHARE = "MyCloud.local", "Media"
UNC = f"\\\\{HOST}\\{SHARE}"


def _fs(base: Path, **kw: object) -> MountedFileSystem:
    kw.setdefault("verify_identity", False)
    return MountedFileSystem(str(base), HOST, SHARE, **kw)  # type: ignore[arg-type]


def _touch(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# --- name mapping -------------------------------------------------------------


@pytest.mark.parametrize(
    ("mount", "smb"),
    [
        ("Crime.101.2026.", "Crime.101.2026"),  # trailing dot (seen in the library)
        ("Trailing ", "Trailing"),
        ("What? Now: <x>", "What Now x"),
        ("Amélie", "Amélie"),  # NFD from the mount, NFC over SMB
        ("Plain Name (2020)", "Plain Name (2020)"),
    ],
)
def test_components_map_like_smbclient(mount: str, smb: str) -> None:
    assert encode_component(mount) == smb
    assert decode_component(smb) == unicodedata_nfc(mount)


def unicodedata_nfc(s: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", s)


def test_mount_and_unc_paths_round_trip() -> None:
    base = "/Volumes/Media"
    path = f"{base}/Movies/Crime.101.2026./Crime.101.2026.mkv"
    unc = mount_to_unc(path, base, HOST, SHARE)
    assert unc == f"{UNC}\\Movies\\Crime.101.2026\\Crime.101.2026.mkv"
    assert unc_to_mount(unc, base) == path
    assert mount_to_unc("/Volumes/MediaOther/x.mkv", base, HOST, SHARE) is None


@pytest.mark.parametrize(
    "bad",
    [
        f"{UNC}\\..\\..\\etc\\passwd",
        f"{UNC}\\Movies\\\\x.mkv",
        f"{UNC}\\.",
        "\\\\h",
        # private-use names that only become ".." / "." once decoded
        f"{UNC}\\.\uf029\\.\uf029\\Users\\me\\m.mkv",
        f"{UNC}\\\uf029\\x.mkv",
    ],
)
def test_unsafe_unc_paths_never_map_into_the_mount(bad: str) -> None:
    with pytest.raises(MountPathError):
        unc_to_mount(bad, "/Volumes/Media")


def test_subtitle_writes_refuse_a_symlinked_folder_on_the_nas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.subtitles.native.placement import write_subtitle

    volumes = tmp_path / "Volumes"
    outside = tmp_path / "outside"
    outside.mkdir()
    (volumes / "Media" / "Movies").mkdir(parents=True)
    (volumes / "Media" / "Movies" / "Evil").symlink_to(outside)
    monkeypatch.setattr(nas_paths, "VOLUMES", str(volumes))

    with pytest.raises(MountPathError):
        write_subtitle(str(volumes / "Media/Movies/Evil/Subs/film.he.srt"), b"1\n")
    assert list(outside.iterdir()) == []  # not even the Subs folder or a temp file

    write_subtitle(str(volumes / "Media/Movies/Good/Subs/film.he.srt"), b"1\n")
    assert (volumes / "Media/Movies/Good/Subs/film.he.srt").read_bytes() == b"1\n"
    require_no_redirect(str(tmp_path / "elsewhere"))  # outside Volumes: not policed
    with pytest.raises(MountPathError):
        require_no_redirect(str(volumes / "Media/Movies/../../../etc"))


def test_placement_maps_catalog_paths_with_mapped_names(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from homeTheater.subtitles.native.placement import resolve_local_media

    config = SimpleNamespace(subtitles=SimpleNamespace(library_base_dir="/Volumes/Media"))
    unc = f"{UNC}\\Movies\\Swiped.2025\\Swiped.mkv"
    got = resolve_local_media(unc, config)  # type: ignore[arg-type]
    assert got == "/Volumes/Media/Movies/Swiped.2025./Swiped.mkv"


# --- the walk -----------------------------------------------------------------


def test_walk_yields_the_paths_the_smb_walker_would(tmp_path: Path) -> None:
    _touch(tmp_path / "Movies/Crime.101.2026./Crime.101.2026.mkv", b"12345")
    _touch(tmp_path / "Movies/Amélie (2001)/Amelie.mkv")
    _touch(tmp_path / "Movies/Film (2020)/.hidden.mkv")
    _touch(tmp_path / "Movies/Film (2020)/._Film.mkv")
    _touch(tmp_path / "Movies/@eaDir/thumb.mkv")
    _touch(tmp_path / "Movies/.AppleDouble/x.mkv")
    _touch(tmp_path / "Movies/Film (2020)/Film.mkv")

    fs = _fs(tmp_path)
    assert fs.resolve("Movies") == f"{UNC}\\Movies"
    entries = {e.path: e for e in fs.walk("Movies")}
    crime = f"{UNC}\\Movies\\Crime.101.2026\\Crime.101.2026.mkv"
    assert set(entries) == {
        crime,
        f"{UNC}\\Movies\\Amélie (2001)\\Amelie.mkv",
        f"{UNC}\\Movies\\Film (2020)\\Film.mkv",
    }
    e = entries[crime]
    assert e.size == 5 and e.name == "Crime.101.2026.mkv"
    assert e.parent == f"{UNC}\\Movies\\Crime.101.2026"


def test_symlinks_are_neither_followed_nor_listed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _touch(outside / "secret.mkv")
    lib = tmp_path / "lib"
    _touch(lib / "Movies/Real (2020)/Real.mkv")
    (lib / "Movies/Linked").symlink_to(outside)
    (lib / "Movies/Real (2020)/Alias.mkv").symlink_to(outside / "secret.mkv")

    # Not listed, and not silently dropped either: the SMB walk might have listed
    # them, so a listing without them must not drive a prune.
    with pytest.raises(OSError, match="2 symlink"):
        list(_fs(lib).walk("Movies"))
    (lib / "Movies/Linked").unlink()
    (lib / "Movies/Real (2020)/Alias.mkv").unlink()
    paths = [e.path for e in _fs(lib).walk("Movies")]
    assert paths == [f"{UNC}\\Movies\\Real (2020)\\Real.mkv"]
    assert outside.joinpath("secret.mkv").exists()


def test_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    _touch(tmp_path / "elsewhere/x.mkv")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "Movies").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError, match="NotADirectoryError"):
        list(_fs(lib).walk("Movies"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_an_unreadable_folder_fails_the_walk(tmp_path: Path) -> None:
    _touch(tmp_path / "Movies/A (2020)/A.mkv")
    locked = tmp_path / "Movies/B (2020)"
    _touch(locked / "B.mkv")
    locked.chmod(0)
    try:
        with pytest.raises(OSError, match="PermissionError errno=13"):
            list(_fs(tmp_path).walk("Movies"))
    finally:
        locked.chmod(0o755)


def test_a_missing_root_fails_the_walk(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="FileNotFoundError"):
        list(_fs(tmp_path).walk("Movies"))


@pytest.mark.parametrize("root", ["../etc", "Movies/../..", ""])
def test_unsafe_roots_are_refused(tmp_path: Path, root: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        list(_fs(tmp_path).walk(root))


def test_identity_is_checked_in_the_child(tmp_path: Path) -> None:
    _touch(tmp_path / "Movies/A/A.mkv")
    # A plain folder is not a mount: the real check refuses it.
    with pytest.raises(MountIdentityError, match="nothing is mounted"):
        list(_fs(tmp_path, verify_identity=True).walk("Movies"))
    with pytest.raises(MountIdentityError, match="apfs volume|hfs volume"):
        list(MountedFileSystem("/", HOST, SHARE).walk("Movies"))


def test_the_volume_is_checked_before_and_after_the_walk(tmp_path: Path) -> None:
    import io

    _touch(tmp_path / "Movies/A/A.mkv")
    calls: list[int] = []
    out = io.StringIO()
    _mountwalk.walk(str(tmp_path), "Movies", out, lambda: calls.append(1))
    assert calls == [1, 1]

    def swapped() -> None:
        calls.append(1)
        if len(calls) > 3:
            raise MountIdentityError("the volume changed")

    out = io.StringIO()
    with pytest.raises(MountIdentityError):
        _mountwalk.walk(str(tmp_path), "Movies", out, swapped)
    assert out.getvalue() == ""  # nothing is emitted from a walk that didn't verify


def test_not_mounted_remounts_once_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag = tmp_path / "mounted"
    _fake_walker(
        tmp_path,
        monkeypatch,
        "import os, sys\n"
        f"if not os.path.exists({str(flag)!r}):\n"
        "    sys.stderr.write('nothing is mounted at x\\n'); sys.exit(4)\n"
        "print('{\"done\": 0}')\n",
    )
    calls: list[str] = []
    assert (
        list(_fs(tmp_path, ensure_mounted=lambda: (calls.append("m"), flag.touch())).walk("Movies"))
        == []
    )
    assert calls == ["m"]

    flag.unlink()

    def fails() -> None:
        calls.append("m")
        raise OSError("share not mounted")

    with pytest.raises(OSError, match="not mounted"):
        list(_fs(tmp_path, ensure_mounted=fails).walk("Movies"))


def test_child_crash_text_never_reaches_the_parent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> int:
        raise UnicodeEncodeError("utf-8", "/Volumes/Media/secret name", 0, 1, "bad")

    monkeypatch.setattr(_mountwalk, "walk", boom)
    assert _mountwalk.main(["x", "/b", "Movies", _mountwalk.NO_VERIFY]) == 2
    assert capsys.readouterr().err == "UnicodeEncodeError\n"

    def denied(*_a: object, **_k: object) -> int:
        raise PermissionError(13, "denied", "/Volumes/Media/Movies/private")

    monkeypatch.setattr(_mountwalk, "walk", denied)
    assert _mountwalk.main(["x", "/b", "Movies", _mountwalk.NO_VERIFY]) == 2
    assert capsys.readouterr().err == "PermissionError errno=13\n"


def test_stderr_is_sanitized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_walker(
        tmp_path,
        monkeypatch,
        "import sys\nsys.stderr.write('bad\\x1b[31m\\u00e9 ' + 'x' * 900)\nsys.exit(2)\n",
    )
    with pytest.raises(OSError) as err:
        list(_fs(tmp_path).walk("Movies"))
    text = str(err.value)
    assert "\x1b" not in text and len(text) < 400


def _fake_walker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    script = tmp_path / "fake_walk.py"
    script.write_text(body)
    monkeypatch.setattr(filesystem, "_MOUNTWALK", str(script))


def test_a_truncated_listing_fails_the_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_walker(
        tmp_path, monkeypatch, 'print(\'{"p": ["Movies", "a.mkv"], "s": 1}\')\n'  # no done line
    )
    with pytest.raises(OSError, match="ended early"):
        list(_fs(tmp_path).walk("Movies"))


def test_the_child_gets_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMB_PASS", "hunter2")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    out = tmp_path / "env.txt"
    _fake_walker(
        tmp_path,
        monkeypatch,
        "import os, sys\n"
        f"open({str(out)!r}, 'w').write(repr(sorted(os.environ)) + repr(sys.flags.isolated))\n"
        "print('{\"done\": 0}')\n",
    )
    assert list(_fs(tmp_path).walk("Movies")) == []
    seen = out.read_text()
    assert "SMB_PASS" not in seen and "PYTHONPATH" not in seen
    assert seen.endswith("1")  # -I: no user site, no cwd on sys.path


def test_a_hung_walk_times_out_and_leaves_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_walker(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    started = time.monotonic()
    with pytest.raises(WalkAbortedError, match="timed out"):
        list(_fs(tmp_path, walk_timeout=1.0).walk("Movies"))
    assert time.monotonic() - started < 10
    assert str(tmp_path) not in _walkers_full()


def _walkers_full() -> str:
    out = subprocess.run(["pgrep", "-fl", "fake_walk.py"], capture_output=True, text=True)
    return out.stdout


def test_abort_kills_a_walk_in_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_walker(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    fs = _fs(tmp_path)
    errors: list[BaseException] = []

    def run() -> None:
        try:
            list(fs.walk("Movies"))
        except BaseException as exc:  # noqa: BLE001 - recorded for the assert
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 10
    while fs._proc is None and time.monotonic() < deadline:
        time.sleep(0.05)
    abort_active_walks()
    worker.join(10)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], WalkAbortedError)
    assert str(tmp_path) not in _walkers_full()


def test_child_filters_match_the_scanner() -> None:
    assert _mountwalk.IGNORED_DIRS == filesystem.IGNORED_DIRS
    for name in [".x", "@eaDir", "#recycle", "Season 1", "._a", "a.mkv"]:
        assert _mountwalk.is_ignored_dir(name) == filesystem.is_ignored_dir(name)
        assert _mountwalk.is_hidden_file(name) == filesystem.is_hidden_file(name)


def test_child_runs_on_the_standard_library_alone() -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-S", filesystem._MOUNTWALK],
        capture_output=True,
        text=True,
        env={},
    )
    assert done.returncode == 64 and "usage" in done.stderr


# --- mount identity -----------------------------------------------------------

_SOURCE = "//alice@MyCloud.local/Media"


def _verify(base: Path, info: MountInfo, hosts: tuple[str, ...] = (HOST,)) -> None:
    verify_smb_mount(str(base), SHARE, hosts, os.getuid(), read_info=lambda _p: info)


@pytest.fixture
def mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os.path, "ismount", lambda _p: True)


def _info(base: Path, **kw: object) -> MountInfo:
    fields: dict[str, object] = {
        "fstype": "smbfs",
        "mounted_on": str(base),
        "source": _SOURCE,
        "owner": os.getuid(),
    }
    fields.update(kw)
    return MountInfo(**fields)  # type: ignore[arg-type]


@pytest.mark.usefixtures("mounted")
def test_the_right_share_passes(tmp_path: Path) -> None:
    _verify(tmp_path, _info(tmp_path))
    _verify(tmp_path, _info(tmp_path, source="//MyCloud._smb._tcp.local/Media"))
    _verify(tmp_path, _info(tmp_path, source="//alice@10.0.0.9/Media"), (HOST, "10.0.0.9"))
    _verify(tmp_path, _info(tmp_path, source="//alice@MYCLOUD/media"))


@pytest.mark.usefixtures("mounted")
@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"fstype": "apfs"}, "not an SMB share"),
        ({"fstype": "msdos"}, "not an SMB share"),
        ({"mounted_on": "/Volumes/Other"}, "not the mount point"),
        ({"owner": os.getuid() + 1}, "another user"),
        ({"source": "//alice@MyCloud.local/Backups"}, "not the 'Media' share"),
        ({"source": "//alice@attacker.example/Media"}, "different server"),
        ({"source": "//alice@MyCloud.other.example/Media"}, "different server"),
        ({"source": "//alice@MyCloudX.local/Media"}, "different server"),
        ({"source": "//alice@10.0.0.99/Media"}, "different server"),  # IPs are exact
    ],
)
def test_a_different_volume_is_refused(tmp_path: Path, change: dict, message: str) -> None:
    with pytest.raises(MountIdentityError, match=message) as err:
        _verify(tmp_path, _info(tmp_path, **change), (HOST, "10.0.0.9"))
    assert "alice" not in str(err.value)  # the SMB user name never reaches a log


def test_nothing_mounted_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NotMountedError, match="nothing is mounted"):
        _verify(tmp_path, _info(tmp_path))


@pytest.mark.skipif(sys.platform != "darwin", reason="statfs layout is macOS's")
def test_statfs_reads_the_boot_volume() -> None:
    info = _mountwalk.read_mount_info("/")
    assert info.mounted_on == "/" and info.fstype in {"apfs", "hfs"}
    assert info.owner == 0
    with pytest.raises(MountIdentityError, match="apfs volume"):
        verify_smb_mount("/", SHARE, (HOST,), os.getuid())


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("MyCloud.local", "mycloud", True),
        ("MyCloud.local", "MyCloud._smb._tcp.local", True),
        ("MyCloud.local", "MyCloud.local.", True),
        ("10.0.0.9", "10.0.0.9", True),
        ("10.0.0.9", "10.0.0.99", False),
        ("192.168.1.5", "192.168.7.7", False),
        ("nas.local", "nas.other.example", False),
    ],
)
def test_hosts_compare_whole(a: str, b: str, same: bool) -> None:
    assert (normalize_host(a) == normalize_host(b)) is same


# --- wiring -------------------------------------------------------------------


def test_build_filesystem_uses_the_mount_when_configured(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from homeTheater.config import loader
    from homeTheater.scanner import build_filesystem

    text = config_file.read_text().replace(
        "  share: TestShare\n",
        "  share: TestShare\n  connect_host: 10.0.0.9\n  mount_dir: /Volumes/TestShare\n",
    )
    config_file.write_text(text)
    monkeypatch.setenv("SMB_HOST", "MyCloud.local")
    loader.get_config.cache_clear()
    fs = build_filesystem(loader.get_config())
    assert isinstance(fs, MountedFileSystem)
    assert fs.base_dir == "/Volumes/TestShare"
    assert fs.resolve("TV Shows") == "\\\\MyCloud.local\\TestShare\\TV Shows"


def test_import_registers_mapped_names(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from homeTheater.acquisition.torrent.service import _mount_to_unc
    from homeTheater.config import loader

    with config_file.open("a") as f:
        f.write("torrent:\n  library_base_dir: /Volumes/TestShare\n")
    monkeypatch.setenv("SMB_HOST", "MyCloud.local")
    loader.get_config.cache_clear()
    got = _mount_to_unc("/Volumes/TestShare/Movies/Swiped.2025./Swiped.mkv", loader.get_config())
    assert got == "\\\\MyCloud.local\\TestShare\\Movies\\Swiped.2025\\Swiped.mkv"


def test_upgrades_map_catalog_paths_through_nas_paths() -> None:
    from types import SimpleNamespace

    from homeTheater.upgrades import library_path

    config = SimpleNamespace(torrent=SimpleNamespace(library_base_dir="/Volumes/Media"))
    good = f"{UNC}\\Movies\\Swiped.2025\uf029\\Swiped.mkv"
    assert library_path(config, good) == "/Volumes/Media/Movies/Swiped.2025./Swiped.mkv"  # type: ignore[arg-type]
    evil = f"{UNC}\\.\uf029\\.\uf029\\Users\\x.mkv"
    assert library_path(config, evil) is None  # type: ignore[arg-type]
