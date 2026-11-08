"""Notifications (plan §5.10): Telegram / log, behind a small interface."""

from .base import LogNotifier, Notifier, TelegramNotifier
from .service import build_notifier, notify, send_telegram, telegram_configured

__all__ = [
    "LogNotifier",
    "Notifier",
    "TelegramNotifier",
    "build_notifier",
    "notify",
    "send_telegram",
    "telegram_configured",
]
