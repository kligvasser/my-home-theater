"""Job failure alerts: say so once, and say when it's over.

Every scheduled job's outcome comes through :func:`report_job`, so this is the one
place that decides what reaches Telegram. A job that fails every 10 minutes alerts
on the first failure, stays quiet on the repeats, and sends one recovery message on
the next success. A success message from the same run travels in that recovery
message, not beside it.

The state lives in the ``setting`` table, one row per job so two jobs finishing
together can't overwrite each other, and survives a restart: otherwise every launchd
restart re-alerts a job that's still broken and forgets the recovery it owes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from ..db.base import utcnow
from ..db.models import Setting
from ..db.session import session_scope
from ..errors import redact_exc
from ..logging_setup import get_logger

log = get_logger(__name__)

_KEY_PREFIX = "job_alert:"

# Returns False when the message didn't go out. None counts as sent.
Send = Callable[[Any, str], Awaitable[bool | None]]


def _load(name: str) -> dict[str, Any] | None:
    with session_scope() as s:
        row = s.get(Setting, _KEY_PREFIX + name)
        return json.loads(row.value) if row is not None and row.value else None


def _save(name: str, state: dict[str, Any] | None) -> None:
    key = _KEY_PREFIX + name
    with session_scope() as s:
        row = s.get(Setting, key)
        if state is None:
            if row is not None:
                s.delete(row)
            return
        if row is None:
            row = Setting(key=key)
            s.add(row)
        row.value = json.dumps(state)


async def _remember(name: str, state: dict[str, Any] | None) -> None:
    try:
        await asyncio.to_thread(_save, name, state)
    except Exception as exc:  # worst case is one repeated alert
        log.warning("alerts.save_failed", job=name, error=redact_exc(exc))


async def report_job(
    config: Any, name: str, *, failed: bool, message: str | None, send: Send
) -> None:
    """Tell the owner what they need to hear about one finished run.

    Skipped runs don't come here: they prove nothing either way. Never raises.
    """

    try:
        state = await asyncio.to_thread(_load, name)
    except Exception as exc:
        # Can't tell a repeat from a first failure: better one alert too many than
        # a silent failure. Same for a routine message.
        log.warning("alerts.load_failed", job=name, error=redact_exc(exc))
        if message:
            await send(config, message)
        return

    if failed:
        failures = (state or {}).get("failures", 0) + 1
        alerted = bool(state and state.get("alerted"))
        if not alerted and message:
            # A notification lost to an outage stays owed: the next failure resends.
            alerted = await send(config, message) is not False
        since = (state or {}).get("since") or utcnow().isoformat()
        await _remember(name, {"since": since, "failures": failures, "alerted": alerted})
        return

    if state is None:
        if message:
            await send(config, message)
        return
    if not state.get("alerted"):  # nobody heard about the failure, so nothing to retract
        await _remember(name, None)
        if message:
            await send(config, message)
        return
    runs = state.get("failures", 1)
    text = f"✅ {name} job recovered after {runs} failed run{'' if runs == 1 else 's'}"
    if await send(config, f"{text}\n{message}" if message else text) is not False:
        await _remember(name, None)
