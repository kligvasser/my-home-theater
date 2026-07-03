"""Notifier interface + implementations (plan §5.10)."""

from __future__ import annotations

from typing import Protocol

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)


class Notifier(Protocol):
    async def send(self, text: str) -> None: ...


class LogNotifier:
    """Default no-op-ish notifier: writes to the structured log."""

    async def send(self, text: str) -> None:
        log.info("notify", text=text)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client

    async def send(self, text: str) -> None:
        resp = await self._client.post(self._url, json={"chat_id": self._chat_id, "text": text})
        resp.raise_for_status()
