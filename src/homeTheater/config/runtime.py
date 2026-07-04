"""Runtime settings overrides (plan §4 ``setting`` table, §5.8 dashboard control).

The dashboard can adjust *tuning* knobs (thresholds, discovery sources, taste
weight, ``auto_approve``) without editing ``config.yaml``: overrides live as one
JSON blob in the ``setting`` table and are deep-merged over the file config by
:func:`effective_config` at job time.

Deliberately NOT overridable: ``features.dry_run`` (the safety switch stays in
the file), paths, credentials, schedule — anything where a web click shouldn't
change process behaviour that drastically.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from ..logging_setup import get_logger
from .loader import get_config
from .settings import AppConfig

# NOTE: db imports stay function-local — config is imported by db.session, so a
# module-level import here would be circular.

log = get_logger(__name__)

SETTING_KEY = "runtime_overrides"

# Top-level sections the dashboard may override, and (for features) which keys.
OVERRIDABLE_SECTIONS = ("thresholds", "discovery", "taste", "features")
OVERRIDABLE_FEATURES = ("auto_approve",)


class OverrideError(ValueError):
    """The submitted overrides are not allowed or don't validate."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _validate_shape(overrides: dict[str, Any]) -> None:
    for section in overrides:
        if section not in OVERRIDABLE_SECTIONS:
            raise OverrideError(f"section {section!r} cannot be overridden at runtime")
    features = overrides.get("features") or {}
    for key in features:
        if key not in OVERRIDABLE_FEATURES:
            raise OverrideError(f"features.{key} cannot be overridden at runtime")


def _merged(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    data = config.model_dump(mode="python", exclude={"secrets"})
    _deep_merge(data, overrides)
    return AppConfig(secrets=config.secrets, **data)


def load_overrides() -> dict[str, Any]:
    """The stored override blob ({} when none)."""

    from ..db.models import Setting
    from ..db.session import session_scope

    with session_scope() as s:
        row = s.get(Setting, SETTING_KEY)
        if row is None or not row.value:
            return {}
        try:
            data = json.loads(row.value)
        except json.JSONDecodeError:
            log.warning("runtime.overrides_corrupt")
            return {}
        return data if isinstance(data, dict) else {}


def save_overrides(overrides: dict[str, Any]) -> AppConfig:
    """Validate + persist overrides; returns the resulting effective config.

    Empty sections/dicts are pruned; saving ``{}`` clears all overrides.
    """

    from ..db.models import Setting
    from ..db.session import session_scope

    overrides = _prune(overrides)
    _validate_shape(overrides)
    try:
        effective = _merged(get_config(), overrides)
    except ValidationError as exc:
        raise OverrideError(f"invalid override values:\n{exc}") from exc

    with session_scope() as s:
        row = s.get(Setting, SETTING_KEY)
        if row is None:
            row = Setting(key=SETTING_KEY)
            s.add(row)
        row.value = json.dumps(overrides)
    log.info("runtime.overrides_saved", sections=sorted(overrides))
    return effective


def _prune(value: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in value.items():
        if isinstance(v, dict):
            v = _prune(v)
            if not v:
                continue
        if v is None:
            continue
        out[k] = v
    return out


def effective_config() -> AppConfig:
    """File config with runtime overrides applied (falls back on bad data)."""

    config = get_config()
    overrides = load_overrides()
    if not overrides:
        return config
    try:
        _validate_shape(overrides)
        return _merged(config, overrides)
    except (OverrideError, ValidationError) as exc:
        log.warning("runtime.overrides_ignored", error=str(exc))
        return config
