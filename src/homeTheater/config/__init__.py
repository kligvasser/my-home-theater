"""Layered application configuration."""

from .loader import ConfigError, get_config, load_config
from .runtime import OverrideError, effective_config, load_overrides, save_overrides
from .settings import (
    Acquisition,
    AppConfig,
    Database,
    Discovery,
    FeatureFlags,
    Metadata,
    NASPaths,
    Schedule,
    Secrets,
    Subtitles,
    Taste,
    Thresholds,
)

__all__ = [
    "Acquisition",
    "AppConfig",
    "ConfigError",
    "Database",
    "Discovery",
    "FeatureFlags",
    "Metadata",
    "NASPaths",
    "OverrideError",
    "Schedule",
    "Secrets",
    "Subtitles",
    "Taste",
    "Thresholds",
    "effective_config",
    "get_config",
    "load_config",
    "load_overrides",
    "save_overrides",
]
