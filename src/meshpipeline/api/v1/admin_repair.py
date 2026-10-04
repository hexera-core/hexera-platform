# Responsibility: Serve the internal repair queue - what is waiting, what one job looks like, and the decisions staff make on it.
# Boundaries: HTTP shape, the admin credential, and orchestration over the repair repository. It
#             runs no repair, judges no geometry and decides no transition's legality.
# Collaborates with: persistence/repair_job_state.py, which owns which movements are legal.
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from meshpipeline.api.v1.admin_billing import admin_dep
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import RepairJobStatus
from meshpipeline.persistence.repair_job_state import (
    awaits_human,
    is_blocked,
    is_terminal,
    legal_sources,
)
from meshpipeline.persistence.session import get_db

router = APIRouter()


def repo():
    # IMPORTED PER CALL, not at module scope: a route is transport, and the architecture suite
    # keeps persistence out of its import graph so an API module cannot grow a second authority
    # over the database. Resolving it here also lets a test substitute the repository.
    from meshpipeline.persistence.repositories.repair_job_repository import RepairJobRepository
    return RepairJobRepository()

# THIS SURFACE IS CROSS-TENANT, and that is the whole point: the people using it are this service's
# own operators working one shared queue over every customer's jobs. It therefore sits behind the
# admin credential rather than an owner identity, exactly like /admin/ops - the customer-facing
# reads of the same rows are owner-scoped and live elsewhere.
#
# The credential proves staff access but names nobody, so every write here makes the operator state
# who they are and records that as a CLAIM (see CadRepairDecision.actor). Weaker than an identity,
# and recorded as what it is rather than dressed up.

#: The decisions an operator may take, and what each one means. A transition absent from here is
#: not refused by the route - `repair_job_state` refuses it - but naming the set keeps the surface
#: readable and stops a caller driving the job into a state no screen was designed for.
_DECISIONS: dict[str, RepairJobStatus] = {
    "inspect":          RepairJobStatus.inspecting,
    "choose_strategy":  RepairJobStatus.awaiting_strategy,
    "repair":           RepairJobStatus.repairing,
    "review_repair":    RepairJobStatus.repair_review,
    "mesh":             RepairJobStatus.meshing,
    "review_mesh":      RepairJobStatus.mesh_review,
    "manual_cleanup":   RepairJobStatus.manual_cleanup,
    "retry":            RepairJobStatus.retrying,
    "escalate":         RepairJobStatus.escalated,
    "ask_customer":     RepairJobStatus.waiting_customer,
    "block":            RepairJobStatus.customer_blocked,
    "deliver":          RepairJobStatus.delivered,
    "deliver_repair":   RepairJobStatus.repair_delivered,
}

#: Decisions that stop or refuse a customer's job. Each one MUST state why: a refusal nobody can
#: answer a complaint about is not a decision, it is a dead end.
_NEEDS_REASON = {"block", "ask_customer", "escalate", "manual_cleanup"}


class AssignIn(BaseModel):
    # The operator's own name. Empty releases the job back to the unassigned queue.
    operator: str = Field(default="", max_length=256)
    #: Refuse the claim if somebody already holds it, so two operators opening the same row
    #: cannot both believe they took it. A lead REASSIGNING work passes false.
    claim: bool = True


class DecideIn(BaseModel):
    decision: str = Field(min_length=1, max_length=32)
    actor: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=2000)
    notes: str = Field(default="", max_length=4000)
    #: Set alongside a strategy choice, so the route that records the decision also records what
    #: was chosen - two writes in one transaction rather than a screen that can half-save.
    strategy: str = Field(default="", max_length=32)


def _job_row(job) -> dict:
    return {
        "id": str(job.id),
        "owner_id": job.owner_id,
        "organization_id": str(job.organization_id) if job.organization_id else None,
        "status": job.status.value,
        "target_engine": job.target_engine,
        "service_priority": job.service_priority,
        "repair_status": job.repair_status,
        "current_strategy": job.current_strategy,
        "assigned_operator": job.assigned_operator,
        "blocked_reason": job.blocked_reason,
        # WHAT THE QUEUE SORTS AND COLOURS BY, answered here rather than re-derived per client:
        # a screen that decides for itself whether a state is a stall will disagree with the
        # metrics that use the same words.
        "awaiting_human": awaits_human(job.status),
        "blocked": is_blocked(job.status),
        "settled": is_terminal(job.status),
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "assigned_at": job.assigned_at.isoformat() if job.assigned_at else None,
    }


def _attempt_row(a) -> dict:
    return {
        "attempt_no": a.attempt_no,
        "mode": a.mode,
        "profile": a.profile,
        "tool_version": a.tool_version,
        # THE AUDIT PAIR, on the screen: the bytes read and the bytes produced. An inspection
        # shows no output because it produced none.
        "input_sha256": a.input_sha256,
        "output_sha256": a.output_sha256,
        "status": a.status,
        "report": a.report,
        "caps": a.caps,
        "measurements": a.measurements,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


def _decision_row(d) -> dict:
    return {
        "decision": d.decision,
        "from_status": d.from_status,
        "actor": d.actor,
        "reason": d.reason,
        "notes": d.notes,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def _parse(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="repair job not found") from None


def _statuses(status: str) -> tuple[RepairJobStatus, ...]:
    """The requested status filter. An unknown name is a 422, never silently ignored: a queue
    that answers a misspelled filter with every row is how an operator works the wrong list."""
    out = []
    for name in (s.strip() for s in (status or "").split(",")):
        if not name:
            continue
        try:
            out.append(RepairJobStatus(name))
        except ValueError:
            raise HTTPException(status_code=422,
                                detail=f"unknown repair status: {name}") from None
    return tuple(out)


@router.get("/queue", dependencies=[Depends(admin_dep)])
async def read_queue(status: str = "", operator: str = "", unassigned: bool = False,
                     limit: int = 50) -> dict:
    bounded = max(1, min(int(limit), 200))
    wanted = _statuses(status)
    async with get_db() as db:
        rows = await repo().operator_queue(db, statuses=wanted, assigned_operator=operator or None,
                                        unassigned_only=unassigned, limit=bounded)
    return {"jobs": [_job_row(j) for j in rows],
            # Echoed so a client can tell an empty queue from a filter that matched nothing.
            "filter": {"status": [s.value for s in wanted], "operator": operator,
                       "unassigned": unassigned, "limit": bounded}}


@router.get("/jobs/{job_id}", dependencies=[Depends(admin_dep)])
async def read_job(job_id: str) -> dict:
    jid = _parse(job_id)
    async with get_db() as db:
        job = await repo().operator_job(db, jid)
        if job is None:
            raise HTTPException(status_code=404, detail="repair job not found")
        attempts = await repo().attempts_for_job(db, jid)
        decisions = await repo().decisions_for_job(db, jid)
    return {
        "job": _job_row(job),
        "attempts": [_attempt_row(a) for a in attempts],
        "decisions": [_decision_row(d) for d in decisions],
        # WHAT THIS JOB MAY DO NEXT, from the one transition table. A screen that composed its own
        # list of buttons would drift from what the database will actually accept.
        "available_decisions": sorted(
            name for name, target in _DECISIONS.items()
            if job.status in legal_sources(target)),
    }


@router.post("/jobs/{job_id}/assign", dependencies=[Depends(admin_dep)])
async def assign_job(job_id: str, body: AssignIn) -> dict:
    jid = _parse(job_id)
    async with get_db() as db:
        job = await repo().operator_job(db, jid)
        if job is None:
            raise HTTPException(status_code=404, detail="repair job not found")
        took = await repo().assign(db, jid, operator=body.operator,
                                 claim_only_if_unassigned=bool(body.claim and body.operator))
        if not took:
            # 409, not 403: the caller is entitled to this queue, it just lost the race for this
            # item. The distinction is what lets a client refresh rather than give up.
            raise HTTPException(status_code=409,
                                detail=f"already assigned to {job.assigned_operator}")
        await db.commit()
        job = await repo().operator_job(db, jid)
    return {"job": _job_row(job)}


@router.post("/jobs/{job_id}/decide", dependencies=[Depends(admin_dep)])
async def decide_job(job_id: str, body: DecideIn) -> dict:
    jid = _parse(job_id)
    name = body.decision.strip()
    target = _DECISIONS.get(name)
    if target is None:
        raise HTTPException(status_code=422, detail=f"unknown decision: {name}")
    reason = body.reason.strip()
    if name in _NEEDS_REASON and not reason:
        raise HTTPException(status_code=422,
                            detail=f"{name} must state a reason - it stops a customer's job")

    async with get_db() as db:
        job = await repo().operator_job(db, jid)
        if job is None:
            raise HTTPException(status_code=404, detail="repair job not found")
        was = job.status.value

        outcome = await repo().transition(
            db, jid, target,
            # A blocking decision's reason IS the blocked reason the customer is eventually told;
            # recording it twice in different words is how the two come to disagree.
            blocked_reason=reason if name in _NEEDS_REASON else None,
            current_strategy=body.strategy or None)
        if outcome is TransitionResult.not_found:
            raise HTTPException(status_code=404, detail="repair job not found")
        if outcome is TransitionResult.rejected_current_state:
            # 409: the job moved under the operator, or the screen offered a button this state
            # never allowed. Either way the answer is to re-read it, not to retry blindly.
            raise HTTPException(
                status_code=409,
                detail=f"{name} is not available from {was} - re-read the job")

        # THE DECISION IS RECORDED EVEN WHEN THE JOB WAS ALREADY THERE. A second operator
        # confirming the same move is a real event with its own actor, and the audit is the
        # history of what people decided, not of what changed.
        await repo().record_decision(db, repair_job_id=jid, decision=name, actor=body.actor,
                                   from_status=was, reason=reason, notes=body.notes.strip())
        await db.commit()
        job = await repo().operator_job(db, jid)
        decisions = await repo().decisions_for_job(db, jid)

    return {"job": _job_row(job), "applied": outcome.value,
            "decisions": [_decision_row(d) for d in decisions]}
