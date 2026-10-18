# Responsibility: Serve a parked repair run's unsure defects grouped and sampled, record an engineer's decisions, and resume the run.
# Owns: the review payload the console consumes, the directive list a resumed run is dispatched with.
# Boundaries: transport. Defect minting is application/repair_review_minting; persistence is the
#             repository; turning free text into a directive is the interpreter. The re-run is
#             the SAME job, so it is not admitted or charged again - its ending charges once.
from __future__ import annotations

import logging
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.application.job_service import JobService
from meshpipeline.persistence.models import JobStatus
from meshpipeline.persistence.repositories.job_repository import JobRepository
from meshpipeline.persistence.repositories.repair_review_repository import (
    ParkClosedError,
    RepairReviewRepository,
)
from meshpipeline.persistence.session import get_db
from meshpipeline.settings import cad_repair as rcfg

logger = logging.getLogger(__name__)

router = APIRouter()
svc = JobService()
repo = RepairReviewRepository()
job_repo = JobRepository()

_BLOCKING = frozenset({"error", "fatal"})


class DecisionIn(BaseModel):
    item_id: uuid.UUID | None = None
    group_key: str | None = None
    action: Literal["accept", "leave", "redirect", "apply_all"]
    text: str | None = None


def _status_of(job) -> str:
    status = getattr(job, "status", None)
    return str(getattr(status, "value", status) or "")


def _require_enabled() -> None:
    if not rcfg.CAD_REPAIR_REVIEW_ENABLED:
        raise HTTPException(409, "Per-defect repair review is not enabled")


async def _owned_job(db, job_id: uuid.UUID, owner_id: str, organization_id: str):
    job = await svc.get_job(db, job_id, owner_id, organization_id=organization_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


def _item_dict(item) -> dict:
    return {
        "id": str(item.id), "group_key": item.group_key, "defect_code": item.defect_code,
        "severity": item.severity, "entity_type": item.entity_type,
        "entity_index": item.entity_index, "fingerprint": item.fingerprint,
        "measurements": item.measurements or {}, "message": item.message,
        "origin": item.origin, "pairing_key": item.pairing_key or "",
        "proposal": item.proposal, "camera": item.camera or {}, "state": item.state,
        "decision": item.decision,
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
    }


def _groups(items: list) -> list[dict]:
    by_key: dict[str, list] = {}
    for item in items:
        by_key.setdefault(item.group_key, []).append(item)
    size = max(int(rcfg.CAD_REPAIR_REVIEW_SAMPLE_SIZE), 0)
    out = []
    for key, members in by_key.items():
        proposed = next((m.proposal.get("op") for m in members
                         if isinstance(m.proposal, dict) and m.proposal.get("op")), "")
        out.append({
            "group_key": key, "defect_code": members[0].defect_code,
            "proposed_op": proposed or "", "count": len(members),
            "sampled_item_ids": [str(m.id) for m in members[:size]],
            "apply_all_allowed": not any(m.pairing_key for m in members),
            "items": [_item_dict(m) for m in members],
            "_blocking": any(m.severity in _BLOCKING for m in members),
        })
    out.sort(key=lambda g: (not g["_blocking"], -g["count"], g["group_key"]))
    for g in out:
        del g["_blocking"]
    return out


@router.get("/{job_id}/review")
async def get_review(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                     organization_id: str = Depends(org_dep)):
    _require_enabled()
    async with get_db() as db:
        await _owned_job(db, job_id, owner_id, organization_id)
        park = await repo.latest_park(db, job_id)
        items = await repo.items_for(db, job_id, park.id if park is not None else None)
    if not items:
        raise HTTPException(409, "This run has nothing to review")
    status = park.status if park is not None else "offered"
    decided = sum(1 for i in items if i.state != "open")
    return {"job_id": str(job_id), "status": status,
            "geometry_session_id": park.geometry_session_id if park is not None else "",
            "groups": _groups(items), "decided": decided, "open": len(items) - decided}


def _log_decision(job_id: uuid.UUID, *, item_id, group_key, action: str, proposal,
                  text: str | None, started: float, directive: dict | None = None) -> None:
    from meshpipeline.capture.logger import TrainingLogger
    TrainingLogger(str(job_id)).log("repair_review", op_id="review:decide", payload={
        "item": str(item_id) if item_id else None, "group": group_key, "action": action,
        "proposal": proposal, "text": text, "directive": directive,
        "latency_ms": int((time.perf_counter() - started) * 1000)})


async def _open_park_or_409(db, job, job_id: uuid.UUID):
    park = await repo.open_park(db, job_id) if _status_of(job) == JobStatus.pending_review.value \
        else None
    if park is None:
        raise HTTPException(409, "This run is not waiting for a review")
    return park


@router.post("/{job_id}/review/decision")
async def decide(job_id: uuid.UUID, body: DecisionIn, owner_id: str = Depends(owner_dep),
                 organization_id: str = Depends(org_dep)):
    _require_enabled()
    started = time.perf_counter()
    async with get_db() as db:
        job = await _owned_job(db, job_id, owner_id, organization_id)
        park = await _open_park_or_409(db, job, job_id)
        items = await repo.items_for(db, job_id, park.id)

        if body.action == "apply_all":
            group_key = body.group_key or ""
            members = [i for i in items if i.group_key == group_key]
            if not members:
                raise HTTPException(404, "Review group not found")
            if any(m.pairing_key for m in members):
                raise HTTPException(409, "This group holds paired defects; decide each one "
                                         "on its own")
            proposal = next((m.proposal for m in members if m.proposal), None)
            if proposal is None:
                raise HTTPException(409, "Nothing was proposed for this group, so there is "
                                         "nothing to apply")
            changed = await repo.record_group_decision(
                db, job_id, group_key, "accepted",
                {"action": "apply_all", "proposal": proposal})
            if not changed:
                raise HTTPException(409, "Every item in this group is already decided")
            await db.commit()
            _log_decision(job_id, item_id=None, group_key=body.group_key, action="apply_all",
                          proposal=proposal, text=body.text, started=started)
            return {"kind": "applied", "changed": changed}

        item = next((i for i in items if body.item_id is not None and i.id == body.item_id), None)
        if item is None:
            raise HTTPException(404, "Review item not found")

        if body.action == "redirect":
            text = (body.text or "").strip()
            if not text:
                raise HTTPException(422, "Say what you want tried instead")
            from meshpipeline.agents.repair_review.interpreter import interpret
            group = [_item_dict(i) for i in items if i.group_key == item.group_key]
            interp = await interpret(text, _item_dict(item), group, job_id=str(job_id))
            if interp.kind != "directive":
                _log_decision(job_id, item_id=item.id, group_key=item.group_key,
                              action="redirect_refused", proposal=item.proposal, text=text,
                              started=started)
                return {"kind": "refusal", "playback": interp.playback,
                        "reason": interp.reason or interp.playback}
            directive = {**interp.directive, "item_id": str(item.id)}
            decision = {"action": "redirect", "text": text, "directive": directive,
                        "playback": interp.playback, "scope": interp.scope}
            state = "redirected"
            reply = {"kind": "directive", "playback": interp.playback}
        elif body.action == "accept":
            if not isinstance(item.proposal, dict) or not item.proposal.get("op"):
                raise HTTPException(409, "Nothing was proposed for this defect; leave it or "
                                         "redirect it")
            decision, state, reply = ({"action": "accept", "proposal": item.proposal},
                                      "accepted", {"kind": "accepted"})
        else:
            decision, state, reply = {"action": "leave"}, "left", {"kind": "left"}

        try:
            await repo.record_decision(db, item.id, state, decision)
        except ParkClosedError as exc:
            raise HTTPException(409, "This review is already closed") from exc
        await db.commit()
    directive_out = decision.get("directive")
    _log_decision(job_id, item_id=item.id, group_key=item.group_key, action=body.action,
                  proposal=item.proposal, text=body.text, started=started,
                  directive=directive_out if isinstance(directive_out, dict) else None)
    return reply


def _replay_directives(kept: list) -> list[dict]:
    out = []
    for op in kept or []:
        if not isinstance(op, dict):
            continue
        name = op.get("name") or op.get("op")
        targets = [t for t in (op.get("targets") or []) if t]
        if not name or not targets:
            continue
        out.append({"op": name, "targets": targets, "defect_code": op.get("defect_code", ""),
                    "defect_fingerprint": targets[0], "source": "replay", "item_id": ""})
    return out


def _engineer_directive(item) -> dict | None:
    if item.state == "redirected":
        directive = (item.decision or {}).get("directive")
        return {**directive, "source": "engineer"} if isinstance(directive, dict) else None
    proposal = item.proposal if isinstance(item.proposal, dict) and item.proposal.get("op") \
        else (item.decision or {}).get("proposal")
    if item.state != "accepted" or not isinstance(proposal, dict) or not proposal.get("op"):
        return None
    own = item.proposal if isinstance(item.proposal, dict) else {}
    targets = list(own.get("targets") or ([item.fingerprint] if item.fingerprint else []))
    return {"op": proposal["op"], "targets": targets, "defect_code": item.defect_code,
            "defect_fingerprint": item.fingerprint, "source": "engineer",
            "item_id": str(item.id)}


@router.post("/{job_id}/review/run", status_code=202)
async def run_review(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                     organization_id: str = Depends(org_dep)):
    _require_enabled()
    from meshpipeline.persistence.job_state import TransitionResult
    async with get_db() as db:
        job = await _owned_job(db, job_id, owner_id, organization_id)
        if _status_of(job) == JobStatus.succeeded.value:
            raise HTTPException(409, "This run was delivered; its review is an offer. To "
                                     "apply it, run it again from the chat.")
        park = await _open_park_or_409(db, job, job_id)
        items = await repo.items_for(db, job_id, park.id)
        decided = sorted((i for i in items if i.state in ("accepted", "redirected")),
                         key=lambda i: (i.decided_at is None, i.decided_at))
        directives = _replay_directives(park.kept_operations)
        directives += [d for d in map(_engineer_directive, decided) if d is not None]

        # The status CAS first: of two racing runs only one wins, and only it closes the park.
        if await job_repo.transition(db, job_id, JobStatus.pending) != TransitionResult.applied:
            raise HTTPException(409, "This run is no longer waiting for a review")
        for item in items:
            if item.state == "open":
                await repo.record_decision(db, item.id, "unreviewed", {"action": "unreviewed"})
        await repo.close_park(db, park.id, "run")
        payload = {**dict(job.dispatch_payload or {}), "repair_directives": directives,
                   "review_continuation": True}
        await db.commit()

    # NOT admitted again: this is the parked run's own purchase, and the park deliberately did
    # not charge it - the re-run's ending does, once.
    from meshpipeline.application import pipeline_run
    try:
        async with get_db() as db:
            await pipeline_run.dispatch(db, str(job_id), payload)
    except Exception as exc:
        logger.error("review re-run of job %s could not be dispatched", job_id, exc_info=True)
        async with get_db() as db:
            await job_repo.mark_launch_failed(db, job_id, type(exc).__name__)
            await db.commit()
        raise HTTPException(500, "Failed to resume the repair run") from exc
    return {"job_id": str(job_id), "status": "pending", "directives": len(directives)}
