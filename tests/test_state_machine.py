"""Candidate status transitions are guarded (dashboard can't contradict the arr)."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeTheater.db.models import CandidateStatus, TitleKind
from homeTheater.errors import InvalidTransitionError


def _reset() -> None:
    from homeTheater.config import loader
    from homeTheater.db import session as db_session

    loader.get_config.cache_clear()
    db_session._engine = None
    db_session._SessionFactory = None


def _seed(status: CandidateStatus) -> int:
    from homeTheater.db import init_db, session_scope
    from homeTheater.db.models import Candidate, CandidateSource, Title

    init_db()
    with session_scope() as s:
        t = Title(tmdb_id=603, title="The Matrix", year=1999, kind=TitleKind.movie)
        s.add(t)
        s.flush()
        c = Candidate(title_id=t.id, source=CandidateSource.discovery, status=status)
        s.add(c)
        s.flush()
        return c.id


def _status(cid: int) -> CandidateStatus:
    from homeTheater.db import session_scope
    from homeTheater.db.models import Candidate

    with session_scope() as s:
        return s.get(Candidate, cid).status


def test_new_can_be_approved_and_rejected(config_file: Path) -> None:
    _reset()
    from homeTheater.discovery.actions import approve, reject

    cid = _seed(CandidateStatus.new)
    assert approve(cid)
    assert _status(cid) is CandidateStatus.approved
    assert reject(cid)  # approved -> rejected is a valid change of mind
    assert _status(cid) is CandidateStatus.rejected


def test_actions_are_idempotent(config_file: Path) -> None:
    _reset()
    from homeTheater.discovery.actions import reject

    cid = _seed(CandidateStatus.rejected)
    assert reject(cid)  # re-click: fine, no-op
    assert _status(cid) is CandidateStatus.rejected


@pytest.mark.parametrize(
    "status",
    [CandidateStatus.queued, CandidateStatus.downloading, CandidateStatus.imported],
)
def test_inflight_and_terminal_states_are_guarded(
    config_file: Path, status: CandidateStatus
) -> None:
    _reset()
    from homeTheater.discovery.actions import approve, reject

    cid = _seed(status)
    with pytest.raises(InvalidTransitionError):
        reject(cid)
    with pytest.raises(InvalidTransitionError):
        approve(cid)
    assert _status(cid) is status  # unchanged


def test_failed_can_be_retried_or_dismissed(config_file: Path) -> None:
    _reset()
    from homeTheater.discovery.actions import approve

    cid = _seed(CandidateStatus.failed)
    assert approve(cid)
    assert _status(cid) is CandidateStatus.approved
