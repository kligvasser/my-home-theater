"""Load and merge layered configuration, failing fast on bad input."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .settings import AppConfig, Secrets

DEFAULT_CONFIG_PATH = "config.yaml"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid (with a clear message)."""


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to {path} and edit it."
        )
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - passthrough of parser message
        raise ConfigError(f"Could not parse YAML at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config at {path} must be a mapping, got {type(data).__name__}.")
    return data


def load_config(config_path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Build :class:`AppConfig` from ``config.yaml`` + environment secrets.

    Raises :class:`ConfigError` with an actionable message on any problem so the
    app fails fast at startup rather than deep inside a job.
    """

    path = Path(config_path or os.environ.get("HOME_THEATER_CONFIG", DEFAULT_CONFIG_PATH))
    raw = _read_yaml(path)

    # Secrets come from the environment / .env, not the YAML file.
    raw.pop("secrets", None)
    try:
        secrets = Secrets()
        config = AppConfig(secrets=secrets, **raw)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration:\n{exc}") from exc
    return config


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Process-wide singleton config (cached). Use in FastAPI/APScheduler wiring."""

    return load_config()
