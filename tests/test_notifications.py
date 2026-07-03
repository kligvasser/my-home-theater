"""Notifier selection, Telegram send (respx), and notify() error-swallowing."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx


def _reset() -> None:
    from homeTheater.config import loader

    loader.get_config.cache_clear()


def test_build_notifier_selects_log_without_telegram(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    _reset()
    from homeTheater.config import get_config
    from homeTheater.notifications import LogNotifier, build_notifier

    notifier = build_notifier(get_config(), httpx.AsyncClient())
    assert isinstance(notifier, LogNotifier)


def test_build_notifier_selects_telegram(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.notifications import TelegramNotifier, build_notifier

    notifier = build_notifier(get_config(), httpx.AsyncClient())
    assert isinstance(notifier, TelegramNotifier)


@respx.mock
async def test_telegram_send() -> None:
    from homeTheater.notifications import TelegramNotifier

    route = respx.post("https://api.telegram.org/bot123/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as http:
        await TelegramNotifier("123", "chat", http).send("hello")
    assert route.called
    assert b'"chat_id":"chat"' in route.calls.last.request.content
    assert b'"text":"hello"' in route.calls.last.request.content


@respx.mock
async def test_notify_swallows_errors(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    _reset()
    from homeTheater.config import get_config
    from homeTheater.notifications import notify

    respx.post("https://api.telegram.org/bot123/sendMessage").mock(return_value=httpx.Response(500))
    # Must not raise even though Telegram returned 500.
    await notify(get_config(), "boom")
