# Responsibility: Decide whether a finished repair run parks for review, and persist what it left for a person.
# Owns: the park rule, the kept operations a park carries, and the park + item writes.
# Boundaries: it decides and writes inside the caller's transaction; the status transition and the
#             commit belong to terminal_finalize, so the park lands with the status or not at all.
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from meshpipeline.persistence.models import JobStatus
from meshpipeline.settings import cad_repair as rcfg

logger = logging.getLogger(__name__)


class OpenParkExists(Exception):
    """The job already has an open park - a defect: the run route closes it before a re-run."""


@dataclass(frozen=True)
class ReviewPlan:
    """What a finished run owes the review: its items, and a park when the run is waiting on them."""

    items: list = field(default_factory=list)
    park: bool = False
    kept_operations: list = field(default_factory=list)
    source_sha256: str = ""
    geometry_session_id: str = ""
    #: `[{item_id, outcome, reason}]` for directives that ran this time, written back onto their items.
    outcomes: list = field(default_factory=list)


def kept_operations(state: dict) -> list[dict]:
    """The operations the repair settled (status == kept) - what a resumed run starts from."""
    attempt = state.get("repair_attempt") or {}
    return [dict(op) for op in (attempt.get("operations") or [])
            if isinstance(op, dict) and op.get("status") == "kept"]


def source_sha256(state: dict) -> str:
    """The upload's digest: the lineage's original when a repair was promoted, else the geometry's."""
    original = (state.get("repair_lineage") or {}).get("original") or {}
    if original.get("sha256"):
        return str(original["sha256"])
    return str(((state.get("geometry") or {}).get("ref") or {}).get("sha256") or "")


def plan_review(state: dict, *, status: JobStatus,
                system_failure: bool = False) -> ReviewPlan | None:
    """None when review is off or the run left nothing for a person.

    A SYSTEM failure or a timeout never parks (and persists no items): it is not about the file, a
    person's decisions cannot cure it, and its blameless note must be the message.

    A failed run with candidates parks; a succeeded one keeps its status and only records the
    items. Any other status (a crash, a cancel) is not this module's to decide.
    """
    if not rcfg.CAD_REPAIR_REVIEW_ENABLED:
        return None
    items = list(state.get("repair_review_candidates") or [])
    outcomes = list(state.get("repair_directive_outcomes") or [])
    if status == JobStatus.failed and system_failure:
        return None
    if (not items and not outcomes) or status not in (JobStatus.failed, JobStatus.succeeded):
        return None
    if status == JobStatus.succeeded or not items:
        return ReviewPlan(items=items, outcomes=outcomes)
    return ReviewPlan(items=items, outcomes=outcomes, park=True, kept_operations=kept_operations(state),
                      source_sha256=source_sha256(state),
                      # THE CHAT SESSION id: the same id the console's
                      # GET /geometry/{session_id}/check/skin route (api/v1/geometry.py) serves
                      # the part's skin under, because the job's `session_id` is the session the
                      # upload created (api/v1/upload.py create_session).
                      geometry_session_id=str(state.get("session_id") or ""))


async def with_proposals(plan: ReviewPlan | None, job_id: str,
                         stopped_because: str = "") -> ReviewPlan | None:
    """The plan with the LLM's proposals filled in for items the rules had nothing for.

    Runs BEFORE persistence, so the items are written once, already proposed. Bounded by the
    proposer's call and timeout caps; fail-open: any failure returns the plan unchanged.
    """
    if plan is None or not plan.park or not plan.items:
        return plan  # only a park has somebody waiting on proposals
    try:
        from dataclasses import replace

        from meshpipeline.agents.repair_review.proposer import propose_for_groups
        return replace(plan, items=await propose_for_groups(
            list(plan.items), job_id=job_id, stopped_because=stopped_because))
    except Exception as exc:  # noqa: BLE001 - proposals are a convenience, never a reason to lose a park
        logger.warning("repair review proposals skipped for job %s: %s", job_id, type(exc).__name__)
        return plan


async def open_park_exists(db, job_id: uuid.UUID, repo=None) -> bool:
    from meshpipeline.persistence.repositories.repair_review_repository import (
        RepairReviewRepository,
    )
    return (await (repo or RepairReviewRepository()).open_park(db, job_id)) is not None


async def write_review(db, job_id: uuid.UUID, plan: ReviewPlan, repo=None) -> uuid.UUID | None:
    """Persist the plan in the caller's transaction; returns the new park's id, or None."""
    from meshpipeline.persistence.repositories.repair_review_repository import (
        RepairReviewRepository,
    )
    repo = repo or RepairReviewRepository()
    park_id = None
    if plan.park:
        if await open_park_exists(db, job_id, repo):
            raise OpenParkExists(str(job_id))
        park = await repo.create_park(db, job_id, plan.kept_operations, plan.source_sha256,
                                      plan.geometry_session_id)
        park_id = park.id
    items = await _close_the_loop(db, job_id, plan, repo)
    await repo.add_items(db, job_id, park_id, items)
    return park_id


async def _close_the_loop(db, job_id: uuid.UUID, plan: ReviewPlan, repo) -> list[dict]:
    """Record what became of the last review's directives, and give re-minted items their history.

    FAIL-OPEN, in its own savepoint: the park matters more than its history, and a failed
    statement must not poison the caller's transaction.
    """
    if not (rcfg.CAD_REPAIR_REVIEW_ENABLED and (plan.outcomes or plan.items)):
        return plan.items
    from meshpipeline.application.repair_review_minting import attach_history
    sp = None
    try:
        sp = await db.begin_nested()
        for outcome in plan.outcomes:
            try:
                item_id = uuid.UUID(str(outcome["item_id"]))
            except (KeyError, ValueError):
                continue
            await repo.record_outcome(db, item_id, outcome)
        prior: dict[str, list[dict]] = {}
        for row in await repo.decided_items(db, job_id):
            if row.fingerprint:
                prior.setdefault(row.fingerprint, []).append(
                    {**dict(row.decision or {}), "state": row.state,
                     "decided_at": row.decided_at.isoformat() if row.decided_at else None})
        items = attach_history(plan.items, prior)
        await sp.commit()
        return items
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair review: outcomes/history skipped for job %s: %s", job_id,
                       type(exc).__name__)
        if sp is not None:
            try:
                await sp.rollback()
            except Exception:  # noqa: BLE001
                pass
        return plan.items
