"""Dashboard auth (plan §5.8).

A single shared token gates every *mutating* endpoint. Approve/reject actions
trigger real grabs and library changes, so even on a LAN this must not be open.
Read-only endpoints (health, library views) are left ungated for now.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from ..config import get_config


def require_token(x_auth_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject requests without the configured dashboard token.

    If no ``DASHBOARD_TOKEN`` is configured, mutating endpoints are refused
    outright (fail closed) rather than silently allowing unauthenticated writes.
    """

    configured = get_config().secrets.dashboard_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_TOKEN is not configured; mutating endpoints are disabled.",
        )
    if x_auth_token is None or not secrets.compare_digest(
        x_auth_token, configured.get_secret_value()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Auth-Token.",
        )
