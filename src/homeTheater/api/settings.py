"""Runtime settings: read effective values, save dashboard overrides (gated).

Overrides layer over ``config.yaml`` (see config.runtime). ``features.dry_run``
is intentionally read-only here — the safety switch stays in the file.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ..config import (
    OverrideError,
    effective_config,
    get_config,
    load_overrides,
    save_overrides,
)
from .auth import require_token

router = APIRouter(prefix="/api", tags=["settings"])

_SECTIONS = ("thresholds", "discovery", "taste")


def _snapshot() -> dict[str, Any]:
    file_cfg = get_config()
    eff = effective_config()
    out: dict[str, Any] = {
        "file": {},
        "effective": {},
        "overrides": load_overrides(),
        "read_only": {"dry_run": file_cfg.features.dry_run},
    }
    for section in _SECTIONS:
        out["file"][section] = getattr(file_cfg, section).model_dump(mode="json")
        out["effective"][section] = getattr(eff, section).model_dump(mode="json")
    out["file"]["features"] = {"auto_approve": file_cfg.features.auto_approve}
    out["effective"]["features"] = {"auto_approve": eff.features.auto_approve}
    return out


@router.get("/settings")
def api_settings() -> dict[str, Any]:
    return _snapshot()


@router.put("/settings", dependencies=[Depends(require_token)])
def api_settings_save(overrides: dict[str, Any]) -> dict[str, Any]:
    """Replace the runtime override blob (send {} to reset everything)."""

    try:
        save_overrides(overrides)
    except OverrideError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _snapshot()
