"""Notifier interface + implementations (plan §5.10)."""

from __future__ import annotations

from typing import Protocol

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)

MAX_PLAIN_TEXT = 4000


class Notifier(Protocol):
    async def send(self, text: str, *, parse_mode: str | None = None) -> None: ...


class LogNotifier:
    """Default no-op-ish notifier: writes to the structured log."""

    async def send(self, text: str, *, parse_mode: str | None = None) -> None:
        log.info("notify", text=text)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self._client = client

    async def send(self, text: str, *, parse_mode: str | None = None) -> None:
        """``text`` must already be escaped for ``parse_mode``. No mode means plain
        text: Telegram shows it as is, so nothing needs escaping."""

        # Telegram refuses a long message outright, and a scan's error list can be one.
        # Cutting isn't safe in a parse mode: those callers fit their own text.
        payload = {"chat_id": self._chat_id, "text": text if parse_mode else text[:MAX_PLAIN_TEXT]}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = await self._client.post(self._url, json=payload)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Never let the raw error escape: its message embeds the request URL,
            # which contains the bot token.
            raise RuntimeError(
                f"Telegram sendMessage failed: HTTP {exc.response.status_code}"
            ) from exc
