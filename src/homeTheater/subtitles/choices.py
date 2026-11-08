"""What was chosen for a (file, language) pair, and what must not be chosen again.

Kept free of the sweep and of ffsubsync so the sweep can use it without depending on
the timing check.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from ..db.base import utcnow
from ..db.models import SubtitleReject, SubtitleSync
from ..db.session import session_scope

if TYPE_CHECKING:  # the sweep imports this module: no runtime import back into native
    from .native.base import SubtitleResult

STATE_PENDING = "pending"  # chosen, timing not looked at yet
STATE_HASH_MATCH = "hash_match"  # matched the file itself; no check needed
STATE_IN_SYNC = "in_sync"
STATE_CORRECTED = "corrected"  # retimed; the original sits beside it
STATE_APPROVED = "approved"
STATE_REVERTED = "reverted"
STATE_FAILED = "failed"  # the check didn't finish; retried after a cooldown

# opensubtitles.org puts its session id in download links, and that changes per login.
_SESSION_RE = re.compile(r"/sid-[^/\"]+")


@dataclass(slots=True)
class Rejections:
    keys: set[str] = field(default_factory=set)
    hashes: set[str] = field(default_factory=set)


def result_key(result: SubtitleResult) -> str:
    """Stable id of a provider's result, across searches and sessions."""

    identity = json.dumps([result.source, result.ref], sort_keys=True, default=str)
    return hashlib.sha256(_SESSION_RE.sub("", identity).encode()).hexdigest()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_rejections(owned_file_id: int, lang: str) -> Rejections:
    out = Rejections()
    with session_scope() as s:
        for row in s.scalars(
            select(SubtitleReject).where(
                SubtitleReject.owned_file_id == owned_file_id, SubtitleReject.lang == lang
            )
        ):
            if row.result_key:
                out.keys.add(row.result_key)
            if row.content_sha256:
                out.hashes.add(row.content_sha256)
    return out


def record_choice(
    owned_file_id: int, lang: str, source: str, release: str, key: str, hash_match: bool
) -> None:
    """Note a freshly placed subtitle. Whatever was known about the last one is void."""

    with session_scope() as s:
        row = s.scalar(
            select(SubtitleSync).where(
                SubtitleSync.owned_file_id == owned_file_id, SubtitleSync.lang == lang
            )
        )
        if row is None:
            row = SubtitleSync(owned_file_id=owned_file_id, lang=lang)
            s.add(row)
        row.state = STATE_HASH_MATCH if hash_match else STATE_PENDING
        row.source = source
        row.release = release[:512]
        row.result_key = key
        row.hash_match = hash_match
        row.offset_seconds = None
        row.fail_count = 0
        row.next_check_at = None
        row.checked_at = utcnow()
        row.detail = None
