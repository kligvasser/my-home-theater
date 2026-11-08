"""S16/S20: the launchd service and packaging files. Nothing here installs anything."""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
PLIST = DEPLOY / "com.homeTheater.app.plist"
WRAPPER = DEPLOY / "home-theater-launchd.sh"
ROTATE = DEPLOY / "rotate-logs.sh"
INSTALL = DEPLOY / "install-launchd.sh"


def _plist() -> dict[str, object]:
    with PLIST.open("rb") as f:
        return plistlib.load(f)


def test_plist_runs_the_venv_entry_point_through_the_wrapper() -> None:
    args = _plist()["ProgramArguments"]
    assert isinstance(args, list)
    assert args[0].endswith("deploy/home-theater-launchd.sh")
    assert args[1].endswith("/.venv/bin/home-theater") and args[2] == "serve"
    assert "conda" not in PLIST.read_text()


def test_plist_restarts_failures_only_and_throttles() -> None:
    plist = _plist()
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert isinstance(plist["ThrottleInterval"], int) and plist["ThrottleInterval"] >= 30


def test_plist_logs_outside_tmp_and_the_repo_data_dir() -> None:
    plist = _plist()
    log_dir = plist["EnvironmentVariables"]["HOME_THEATER_LOG_DIR"]  # type: ignore[index]
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = plist[key]
        assert isinstance(path, str)
        assert not path.startswith(("/tmp", "/private/tmp"))
        assert path.startswith(log_dir)


def test_plist_waits_for_the_mount_and_has_mount_on_its_path() -> None:
    env = _plist()["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert env["HOME_THEATER_MOUNT"].startswith("/Volumes/")
    assert int(env["HOME_THEATER_MOUNT_WAIT_SECONDS"]) > 0
    assert "/sbin" in env["PATH"].split(":")  # health runs `mount`


def test_deploy_scripts_are_executable_and_parse() -> None:
    for script in (WRAPPER, ROTATE, INSTALL):
        assert script.stat().st_mode & stat.S_IXUSR, script
        subprocess.run(["/bin/sh", "-n", str(script)], check=True)


def _stub(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _wrap(tmp_path: Path, *command: str, **env: str) -> subprocess.CompletedProcess[str]:
    calls = tmp_path / "caffeinate.args"
    caffeinate = _stub(tmp_path / "caffeinate", f'echo "$@" > {calls}')
    return subprocess.run(
        ["/bin/sh", str(WRAPPER), *command],
        env={
            "PATH": os.environ["PATH"],
            "HOME_THEATER_CAFFEINATE": str(caffeinate),
            **env,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_wrapper_holds_only_an_idle_sleep_assertion_and_becomes_the_app(tmp_path: Path) -> None:
    """exec, or launchd's pid would be a shell and TCC would judge that shell."""

    pid_file = tmp_path / "pid"
    app = _stub(tmp_path / "app", f'echo $$ > {pid_file}; echo "$@"')
    done = _wrap(tmp_path, str(app), "serve")
    assert done.returncode == 0 and "serve" in done.stdout

    for _ in range(100):
        args = tmp_path / "caffeinate.args"
        if args.exists() and args.read_text():
            break
        subprocess.run(["sleep", "0.01"], check=True)
    flags = (tmp_path / "caffeinate.args").read_text().split()
    assert flags[:2] == ["-i", "-w"]  # not -d, -s, -u or -m
    assert flags[2] == pid_file.read_text().strip()  # dies with the app


def test_wrapper_starts_the_app_when_the_mount_never_appears(tmp_path: Path) -> None:
    table = _stub(tmp_path / "mount", "echo '/dev/disk on / (apfs)'")
    app = _stub(tmp_path / "app", "echo started")
    done = _wrap(
        tmp_path,
        str(app),
        HOME_THEATER_MOUNT="/Volumes/Gone",
        HOME_THEATER_MOUNT_WAIT_SECONDS="0",
        HOME_THEATER_MOUNT_TABLE=str(table),
    )
    assert done.returncode == 0 and "started" in done.stdout
    assert "not mounted" in done.stderr


def test_wrapper_waits_until_the_mount_appears(tmp_path: Path) -> None:
    flag = tmp_path / "second-call"
    table = _stub(
        tmp_path / "mount",
        f"if [ -f {flag} ]; then echo '//nas/x on /Volumes/NAS (smbfs)'; " f"else touch {flag}; fi",
    )
    sleep = _stub(tmp_path / "sleep", "exit 0")
    app = _stub(tmp_path / "app", "echo started")
    done = subprocess.run(
        ["/bin/sh", str(WRAPPER), str(app)],
        env={
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "HOME_THEATER_CAFFEINATE": "/nonexistent",
            "HOME_THEATER_MOUNT": "/Volumes/NAS",
            "HOME_THEATER_MOUNT_WAIT_SECONDS": "60",
            "HOME_THEATER_MOUNT_TABLE": str(table),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert sleep.exists() and flag.exists()
    assert done.returncode == 0 and "started" in done.stdout and done.stderr == ""


def test_rotation_keeps_the_open_file_and_a_bounded_history(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    with log.open("ab") as held_open:  # what launchd does
        for generation in range(4):
            held_open.write(b"x" * 100 + str(generation).encode())
            held_open.flush()
            subprocess.run(["/bin/sh", str(ROTATE), str(tmp_path), "50", "2"], check=True)
        held_open.write(b"after")
        held_open.flush()
    assert log.read_bytes() == b"after"  # the same file, appended to from zero
    assert sorted(p.name for p in tmp_path.glob("*.gz")) == ["out.log.1.gz", "out.log.2.gz"]


def test_rotation_leaves_small_logs_alone(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    log.write_bytes(b"small")
    subprocess.run(["/bin/sh", str(ROTATE), str(tmp_path), "1000"], check=True)
    assert log.read_bytes() == b"small" and not list(tmp_path.glob("*.gz"))


def test_installer_dry_run_retargets_paths_and_touches_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "elsewhere"
    shutil.copytree(DEPLOY, project / "deploy")
    done = subprocess.run(
        ["/bin/sh", str(project / "deploy" / "install-launchd.sh"), "--dry-run"],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )
    plist = plistlib.loads(done.stdout.encode())
    assert plist["ProgramArguments"][1] == f"{project}/.venv/bin/home-theater"
    assert plist["StandardOutPath"] == f"{home}/Library/Logs/home-theater/out.log"
    assert "/Users/kligvasser" not in done.stdout
    assert list(home.iterdir()) == []


def test_installer_names_the_tcc_grant() -> None:
    text = INSTALL.read_text()
    assert "Full Disk Access" in text and "realpath" in text


def test_systemd_unit_uses_the_venv_and_restarts_on_failure() -> None:
    unit = (DEPLOY / "home-theater.service").read_text()
    assert "/.venv/bin/home-theater serve" in unit
    assert "miniconda" not in unit
    assert "Restart=on-failure" in unit and "RestartSec=30" in unit


def test_packaging_declares_what_the_app_needs() -> None:
    with (ROOT / "pyproject.toml").open("rb") as f:
        project = tomllib.load(f)
    assert "cryptography" in project["project"]["dependencies"]
    data = project["tool"]["setuptools"]["package-data"]["homeTheater"]
    assert "web/static/*.js" in data and (ROOT / "src/homeTheater/web/static/app.js").exists()


def test_environment_file_mirrors_the_dependencies() -> None:
    env = yaml.safe_load((ROOT / "environment.yaml").read_text())
    pip = next(d for d in env["dependencies"] if isinstance(d, dict))["pip"]
    for needed in ("cryptography", "scikit-learn"):
        assert needed in pip


def test_pre_commit_runs_a_secret_scanner() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}
    assert "gitleaks" in hooks


@pytest.mark.parametrize("name", ["config.example.yaml"])
def test_example_config_loads_with_strict_models(name: str) -> None:
    """Its keys sit on extra="forbid" models now; a stale one would break a new install."""

    from homeTheater.config.settings import AppConfig

    data = yaml.safe_load((ROOT / name).read_text())
    AppConfig(**data)
