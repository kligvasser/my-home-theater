"""The attention inbox: everything waiting on the owner, with what fixes it.

Reads what the rest of the app already knows: pipeline for parked and failed grabs,
the latest scan's ``job_run`` for refused prunes, ``job_freshness`` for stale jobs,
the provider probes for outages. All of it is local, cheap SQL, so the nav badge can
ask on every page; only :func:`collect`'s caller decides whether to probe providers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select

from .config import AppConfig
from .db.models import MARKER_STALLED, CandidateStatus, JobRun
from .db.session import session_scope
from .health import ProviderStatus
from .logging_setup import get_logger
from .pipeline import ExecutionState, attention_states
from .scanner.repair import unmatched_titles
from .scheduler import job_freshness

log = get_logger(__name__)

UNMATCHED_SHOWN = 50
PRUNE_SAMPLE_SHOWN = 10

# Both LibraryConflictError messages end with this.
_CONFLICT_PHRASE = "resolve it by hand"

# Stale jobs that have a run-now endpoint, and the body it wants. The others link to
# Status.
_RUN_NOW: dict[str, tuple[str, dict[str, Any] | None]] = {
    "scan": ("/api/pipeline/scan", None),
    "sync": ("/api/pipeline/sync", None),
    "acquire": ("/api/pipeline/acquire-now", None),
    "subtitle": ("/api/subtitles/search", None),
    "discovery": ("/api/candidates/discover", {}),
    "train": ("/api/preferences/train", None),
}


@dataclass(frozen=True, slots=True)
class Action:
    label: str
    url: str | None = None  # POST here, an existing endpoint wherever one exists
    href: str | None = None  # or just go here
    body: dict[str, Any] | None = None
    confirm: str | None = None


@dataclass(frozen=True, slots=True)
class Item:
    kind: str  # prune | conflict | import | failed | stalled | held | provider | job | unmatched
    label: str
    title: str
    detail: str = ""  # what happened
    hint: str = ""  # what to do
    actions: tuple[Action, ...] = ()
    sample: tuple[str, ...] = ()


@dataclass(slots=True)
class Inbox:
    items: list[Item] = field(default_factory=list)
    hidden: int = 0  # unmatched titles past the shown list

    @property
    def total(self) -> int:
        return len(self.items) + self.hidden

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [asdict(i) for i in self.items],
            "hidden": self.hidden,
            "total": self.total,
        }


def _name(state: ExecutionState) -> str:
    return f"{state.title} ({state.year})" if state.year else state.title


def _grab_item(state: ExecutionState) -> Item | None:
    """One candidate's card. The cheap retry that keeps the local data comes first:
    a failed season can still hold a parked import."""

    name = _name(state)
    candidate = f"/api/candidates/{state.candidate_id}"
    retry_import = Action("Retry import", url=f"{candidate}/retry-import")
    if state.can_retry_import:
        conflict = _CONFLICT_PHRASE in (state.attention or "")
        label = "Library conflict" if conflict else "Import stopped"
        kind = "conflict" if conflict else "import"
        return Item(kind, label, name, hint=state.attention or "", actions=(retry_import,))
    if state.attention:
        see = Action("Open Activity", href="/activity")
        return Item("held", "Held", name, hint=state.attention, actions=(see,))
    if state.status == CandidateStatus.failed.value:
        stalled = MARKER_STALLED in (state.error or "")
        return Item(
            "stalled" if stalled else "failed",
            "Stalled" if stalled else "Failed",
            name,
            detail=state.error or "",
            hint="Retry searches for a release again from scratch.",
            actions=(Action("Retry", url=f"{candidate}/restart"),),
        )
    return None


def latest_prune_refusals() -> list[dict[str, Any]]:
    """What the most recent finished scan refused to prune. A later clean scan means
    the problem went away, so only the newest run counts."""

    with session_scope() as s:
        run = s.scalars(
            select(JobRun)
            .where(JobRun.kind == "scan", JobRun.finished_at.is_not(None))
            .order_by(JobRun.started_at.desc(), JobRun.id.desc())
            .limit(1)
        ).first()
        return list((run.stats or {}).get("prune_refused") or []) if run is not None else []


def _prune_item(refusal: dict[str, Any]) -> Item:
    root, stale, total = refusal["root"], refusal["stale"], refusal["total"]
    limit = f"{refusal['max_percent']:g}% or {refusal['max_rows']} rows"
    detail = (
        f"The last scan wouldn't remove {stale} of {total} catalog rows under {root} "
        f"({refusal['percent']:g}%): over the limit of {limit}. It repeats on every "
        "scan until someone decides."
    )
    hint = (
        "If those files really are gone, accept. Only catalog rows go; nothing on the "
        "NAS is touched. A rescan does it, and only if it finds this same set missing."
    )
    warning = ""
    if refusal.get("walked") == 0:
        warning = (
            f"\n\nThe scan saw no files at all under {root}. That usually means the NAS "
            "dropped, not that the files are gone: check it first."
        )
        hint = "The scan saw nothing under this root. Check the NAS before accepting. " + hint
    sample = tuple(refusal.get("sample") or ())
    shown = "\n".join(f"  {path}" for path in sample[:PRUNE_SAMPLE_SHOWN])
    more = stale - min(len(sample), PRUNE_SAMPLE_SHOWN)
    confirm = (
        f"Remove {stale} of {total} catalog rows under {root} ({refusal['percent']:g}%)? "
        f"The limit is {limit}.\n\n{shown}"
        + (f"\n  …and {more} more" if more > 0 else "")
        + warning
    )
    accept = Action(
        "Accept this removal",
        url="/api/inbox/force-prune",
        body={"root": root, "fingerprint": refusal["fingerprint"]},
        confirm=confirm,
    )
    return Item("prune", "Scan refused", root, detail, hint, (accept,), sample)


def _job_item(job: Any) -> Item:
    if job.age_minutes is None:
        detail = "No recorded success yet."
    else:
        detail = f"Last success {job.age_minutes // 60} h ago; it should run every "
        detail += f"{job.interval_minutes} min."
    run = _RUN_NOW.get(job.name)
    action = Action("Run now", url=run[0], body=run[1]) if run else None
    return Item(
        "job",
        "Stale job",
        job.name,
        detail,
        actions=(action or Action("Open Status", href="/status"),),
    )


def _provider_item(status: ProviderStatus) -> Item:
    see = Action("Open Status", href="/status")
    return Item("provider", "Provider problem", status.name, status.detail, actions=(see,))


def collect(config: AppConfig, providers: list[ProviderStatus]) -> Inbox:
    """Everything needing the owner, most urgent first. Synchronous: call off the loop."""

    inbox = Inbox()
    inbox.items += [_prune_item(r) for r in latest_prune_refusals()]
    grabs = [item for s in attention_states(config) if (item := _grab_item(s))]
    order = ("conflict", "import", "failed", "stalled", "held")
    inbox.items += sorted(grabs, key=lambda item: order.index(item.kind))
    inbox.items += [_provider_item(p) for p in providers if p.configured and p.ok is False]
    inbox.items += [_job_item(j) for j in job_freshness(config) if j.stale]
    with session_scope() as s:
        unmatched, total = unmatched_titles(s, UNMATCHED_SHOWN)
    for row in unmatched:
        name = f"{row['title']} ({row['year']})" if row["year"] else row["title"]
        fix = Action("Match it", href=f"/repair?title={row['id']}")
        hint = "Tell it which TMDb title this is."
        inbox.items.append(Item("unmatched", f"Unmatched {row['kind']}", name, "", hint, (fix,)))
    inbox.hidden = total - len(unmatched)
    return inbox
