"""Notification helper: pick a notifier from config and send, never raising.

A failed notification must never break the pipeline, so errors are logged and
swallowed. For import/grab events, prefer Radarr/Sonarr's own Connect
notifications; use this for *our* events (new candidates, job failures).
"""

from __future__ import annotations

import httpx

from ..config import AppConfig
from ..errors import NotConfiguredError, redact_exc
from ..logging_setup import get_logger
from .base import LogNotifier, Notifier, TelegramNotifier

log = get_logger(__name__)


def build_notifier(config: AppConfig, client: httpx.AsyncClient) -> Notifier:
    s = config.secrets
    if s.telegram_bot_token and s.telegram_chat_id:
        return TelegramNotifier(s.telegram_bot_token.get_secret_value(), s.telegram_chat_id, client)
    return LogNotifier()


def telegram_configured(config: AppConfig) -> bool:
    s = config.secrets
    return bool(s.telegram_bot_token and s.telegram_chat_id)


async def send_telegram(config: AppConfig, text: str, *, parse_mode: str | None = None) -> None:
    """Send through Telegram or raise, for a caller that has to know it arrived.

    Raises :class:`NotConfiguredError` without a bot; there's no log fallback.
    """

    s = config.secrets
    if not (s.telegram_bot_token and s.telegram_chat_id):
        raise NotConfiguredError("Telegram is not configured")
    async with httpx.AsyncClient(timeout=10.0) as http:
        token = s.telegram_bot_token.get_secret_value()
        await TelegramNotifier(token, s.telegram_chat_id, http).send(text, parse_mode=parse_mode)


async def notify(config: AppConfig, text: str) -> bool:
    """Send a notification, swallowing any error. False if it didn't go out."""

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            await build_notifier(config, http).send(text)
    except Exception as exc:  # never break the caller
        log.warning("notify.failed", error=redact_exc(exc))
        return False
    return True
