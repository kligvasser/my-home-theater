"""Dashboard auth (plan §5.8).

A single shared token gates every *mutating* endpoint. Approve/reject actions
trigger real grabs and library changes, so even on a LAN this must not be open.
Read-only endpoints (health, library views) are left ungated for now.

Webhooks use a separate ``WEBHOOK_TOKEN`` (falling back to ``DASHBOARD_TOKEN``)
because that token rides in Radarr/Sonarr webhook URLs and can end up in access
logs — a leak there must not expose the dashboard token.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Query, status
from pydantic import SecretStr

from ..config import get_config


def _matches(provided: str, configured: str) -> bool:
    """Constant-time token comparison; never raises (non-ASCII input -> False)."""

    return hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))


def _verify(token: str | None, configured: SecretStr | None) -> None:
    """Fail closed if no token is configured; else constant-time compare."""

    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_TOKEN is not configured; mutating endpoints are disabled.",
        )
    if token is None or not _matches(token, configured.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing token.",
        )


def require_token(x_auth_token: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject requests without the configured dashboard token."""

    _verify(x_auth_token, get_config().secrets.dashboard_token)


def require_webhook_token(
    token: str | None = Query(default=None),
    x_auth_token: str | None = Header(default=None),
) -> None:
    """Webhook dependency: accept the webhook token via ``?token=`` or the header.

    Radarr/Sonarr webhook connections can't always set a custom header, so the
    query param lets you embed the token in the configured webhook URL. Prefer a
    dedicated ``WEBHOOK_TOKEN`` so an access-log leak doesn't expose the
    dashboard token; without one, the dashboard token is accepted.
    """

    secrets = get_config().secrets
    _verify(token or x_auth_token, secrets.webhook_token or secrets.dashboard_token)
