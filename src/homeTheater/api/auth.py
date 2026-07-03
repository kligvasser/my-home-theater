"""Dashboard auth (plan §5.8).

A single shared token gates every *mutating* endpoint. Approve/reject actions
trigger real grabs and library changes, so even on a LAN this must not be open.
Read-only endpoints (health, library views) are left ungated for now.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, Query, status

from ..config import get_config


def _verify(token: str | None) -> None:
    """Fail closed if no token is configured; else constant-time compare."""

    configured = get_config().secrets.dashboard_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_TOKEN is not configured; mutating endpoints are disabled.",
        )
    if token is None or not secrets.compare_digest(token, configured.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token.",
        )


def require_token(x_auth_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject requests without the configured dashboard token."""

    _verify(x_auth_token)


def require_webhook_token(
    token: str | None = Query(default=None),
    x_auth_token: str | None = Header(default=None),
) -> None:
    """Webhook dependency: accept the token via ``?token=`` or the header.

    Radarr/Sonarr webhook connections can't always set a custom header, so the
    query param lets you embed the token in the configured webhook URL.
    """

    _verify(token or x_auth_token)
