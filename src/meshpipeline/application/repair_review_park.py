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


@dataclass(frozen=True)
class ReviewPlan:
    """What a finished run owes the review: its items, and a park when the run is waiting on them."""

    items: list = field(default_factory=list)
    park: bool = False
    kept_operations: list = field(default_factory=list)
    source_sha256: str = ""
    geometry_session_id: str = ""


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


def plan_review(state: dict, *, status: JobStatus) -> ReviewPlan | None:
    """None when review is off or the run left nothing for a person.

    A failed run with candidates parks; a succeeded one keeps its status and only records the
    items. Any other status (a crash, a cancel) is not this module's to decide.
    """
    if not rcfg.CAD_REPAIR_REVIEW_ENABLED:
        return None
    items = list(state.get("repair_review_candidates") or [])
    if not items or status not in (JobStatus.failed, JobStatus.succeeded):
        return None
    if status == JobStatus.succeeded:
        return ReviewPlan(items=items)
    return ReviewPlan(items=items, park=True, kept_operations=kept_operations(state),
                      source_sha256=source_sha256(state),
                      geometry_session_id=str(state.get("session_id") or ""))


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
        park = await repo.create_park(db, job_id, plan.kept_operations, plan.source_sha256,
                                      plan.geometry_session_id)
        park_id = park.id
    await repo.add_items(db, job_id, park_id, plan.items)
    return park_id


async def refuse_second_open_park(session_factory, plan: ReviewPlan | None, job_id: uuid.UUID,
                                  jlog, repo=None) -> ReviewPlan | None:
    """ONE OPEN PARK PER JOB. The run route closes the old park before it dispatches a re-run, so
    an open one at park time is a defect: say so and return None (fail plainly) rather than open
    a second. A plan that does not park passes through untouched."""
    if plan is None or not plan.park:
        return plan
    async with session_factory() as db:
        if not await open_park_exists(db, job_id, repo):
            return plan
    jlog.error("repair review: job %s already has an open park at park time - this is a defect "
               "(the run route closes it first); failing plainly without a second park", job_id)
    return None
