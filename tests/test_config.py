"""Config loader: valid load, defaults, and fail-fast on bad input."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeTheater.config import ConfigError, load_config


def test_loads_valid_config(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert cfg.nas.movies_root == "Movies"
    # Safe defaults.
    assert cfg.features.dry_run is True
    assert cfg.features.auto_approve is False
    assert cfg.thresholds.min_imdb_rating == 7.0


def test_missing_file_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_config_fails_fast(tmp_path: Path) -> None:
    bad = tmp_path / "config.yaml"
    bad.write_text("nas:\n  movies_root: Movies\n")  # missing required tv_root
    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_config(bad)


def test_secrets_not_in_repr(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert "dashboard_token" not in repr(cfg)
