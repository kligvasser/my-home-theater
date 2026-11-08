"""Runtime settings overrides (plan §4 ``setting`` table, §5.8 dashboard control).

The dashboard can adjust *tuning* knobs (thresholds, discovery sources, taste
weight, ``auto_approve``) without editing ``config.yaml``: overrides live as one
JSON blob in the ``setting`` table and are deep-merged over the file config by
:func:`effective_config` at job time.

Deliberately NOT overridable: ``features.dry_run`` (the safety switch stays in
the file), paths, backends, sources, credentials, schedule — anything where a web
click shouldn't change process behaviour that drastically.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from ..errors import redact_exc
from ..logging_setup import get_logger
from .loader import get_config
from .settings import AppConfig

# NOTE: db imports stay function-local — config is imported by db.session, so a
# module-level import here would be circular.

log = get_logger(__name__)

SETTING_KEY = "runtime_overrides"

# Top-level sections the dashboard may override, and (for the sections that also
# hold paths, backends or switches) which keys within them.
OVERRIDABLE_SECTIONS = ("thresholds", "discovery", "taste", "subtitles", "features", "acquisition")
OVERRIDABLE_FEATURES = ("auto_approve",)
# Only the nightly download window is dashboard-editable under acquisition;
# backend/profiles/paths stay in config.yaml.
OVERRIDABLE_ACQUISITION = ("window",)
# Coverage tuning only. library_base_dir, backend, sources and the user agents
# decide where files are written and which accounts are used, so they stay in
# config.yaml.
OVERRIDABLE_SUBTITLES = ("languages", "max_searches_per_sweep", "hearing_impaired")

_KEY_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "features": OVERRIDABLE_FEATURES,
    "acquisition": OVERRIDABLE_ACQUISITION,
    "subtitles": OVERRIDABLE_SUBTITLES,
}


class OverrideError(ValueError):
    """The submitted overrides are not allowed or don't validate."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _forbidden_keys(overrides: dict[str, Any]) -> list[str]:
    """Dotted names of the overrides the allow-lists don't permit."""

    bad: list[str] = []
    for section, value in overrides.items():
        if section not in OVERRIDABLE_SECTIONS:
            bad.append(section)
        elif section in _KEY_ALLOWLISTS and isinstance(value, dict):
            bad += [f"{section}.{k}" for k in value if k not in _KEY_ALLOWLISTS[section]]
    return bad


def _validate_shape(overrides: dict[str, Any]) -> None:
    for name in _forbidden_keys(overrides):
        if "." in name:
            raise OverrideError(f"{name} cannot be overridden at runtime")
        raise OverrideError(f"section {name!r} cannot be overridden at runtime")
    for section, value in overrides.items():
        if not isinstance(value, dict):
            raise OverrideError(f"section {section!r} must be a mapping")


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
        raise OverrideError(f"invalid override values:\n{redact_exc(exc)}") from exc

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


def _drop(overrides: dict[str, Any], dotted: tuple[Any, ...]) -> None:
    node: Any = overrides
    for key in dotted[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    if isinstance(node, dict):
        node.pop(dotted[-1], None)


def _usable_overrides(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """Apply a stored blob, shedding keys that were valid when saved but no longer
    are: one retired key must not cost every other override."""

    for name in _forbidden_keys(overrides):
        log.warning("runtime.override_dropped", key=name)
        _drop(overrides, tuple(name.split(".")))
    _validate_shape(overrides)
    try:
        return _merged(config, overrides)
    except ValidationError as exc:
        unknown = [e["loc"] for e in exc.errors() if e["type"] == "extra_forbidden"]
        if not unknown:
            raise
        for loc in unknown:
            log.warning("runtime.override_dropped", key=".".join(map(str, loc)))
            _drop(overrides, loc)
        return _merged(config, overrides)


def effective_config() -> AppConfig:
    """File config with runtime overrides applied (falls back on bad data).

    Reads the database: from async code call it through ``asyncio.to_thread``. The
    file config is the fallback when the table can't be read too, so a locked or
    not-yet-migrated database degrades a tuning knob rather than the job.
    """

    config = get_config()
    try:
        overrides = load_overrides()
    except SQLAlchemyError as exc:
        log.warning("runtime.overrides_unreadable", error=redact_exc(exc))
        return config
    if not overrides:
        return config
    try:
        return _usable_overrides(config, overrides)
    except (OverrideError, ValidationError) as exc:
        log.warning("runtime.overrides_ignored", error=redact_exc(exc))
        return config
