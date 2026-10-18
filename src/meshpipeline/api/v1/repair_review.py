# Responsibility: Serve a parked repair run's unsure defects grouped and sampled, record an engineer's decisions, and resume the run.
# Owns: the review payload the console consumes, the directive list a resumed run is dispatched with.
# Boundaries: transport. Defect minting is application/repair_review_minting; persistence is the
#             repository; turning free text into a directive is the interpreter. The re-run is
#             the SAME job, so it is not admitted or charged again - its ending charges once.
from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.application.job_service import JobService
from meshpipeline.persistence.models import JobStatus
from meshpipeline.persistence.session import get_db
from meshpipeline.settings import cad_repair as rcfg

logger = logging.getLogger(__name__)

router = APIRouter()
svc = JobService()


class _Lazy:
    """A repository built on first use: routes import no persistence repository at module scope."""

    def __init__(self, loader):
        self._loader, self._obj = loader, None

    def __getattr__(self, name):
        if self._obj is None:
            self._obj = self._loader()
        return getattr(self._obj, name)


def _review_repo():
    from meshpipeline.persistence.repositories.repair_review_repository import (
        RepairReviewRepository,
    )
    return RepairReviewRepository()


def _job_repo():
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    return JobRepository()


repo = _Lazy(_review_repo)
def _lease_repo():
    from meshpipeline.persistence.lease import LeaseRepository
    return LeaseRepository()


job_repo = _Lazy(_job_repo)
lease_repo = _Lazy(_lease_repo)

_BLOCKING = frozenset({"error", "fatal"})


class DecisionIn(BaseModel):
    item_id: uuid.UUID | None = None
    group_key: str | None = None
    action: Literal["accept", "leave", "redirect", "apply_all"]
    text: str | None = None


def _park_closed() -> type[Exception]:
    from meshpipeline.persistence.repositories.repair_review_repository import ParkClosedError
    return ParkClosedError


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
        "decision_history": getattr(item, "decision_history", None) or [],
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
        job = await _owned_job(db, job_id, owner_id, organization_id)
        park = await repo.latest_park(db, job_id)
        # A DELIVERED RUN SERVES ITS OFFERED ITEMS (minted without a park), even when an earlier
        # cycle of it parked: park -> re-run -> delivered must not keep showing the closed park.
        delivered = _status_of(job) == JobStatus.succeeded.value
        offered = park is None or delivered
        items = await repo.items_for(db, job_id, None if offered else park.id)
    if not items:
        raise HTTPException(409, "This run has nothing to review")
    # The served status is the job's AND the park's together: an open park under a job that has
    # moved on (resumed, cancelled) is no longer something a person can act on.
    session_id = (park.geometry_session_id if park is not None
                  else str((getattr(job, "dispatch_payload", None) or {}).get("session_id") or ""))
    if offered:
        status = "offered"
    elif park.status == "open" and _status_of(job) == JobStatus.pending_review.value:
        status = "open"
    else:
        status = "closed"
    decided = sum(1 for i in items if i.state != "open")
    # THE MINT CAP'S DROPPED COUNT rides the run's own final result (written with the park), so
    # the review can say it shows the largest of a larger number - no column for one integer.
    truncated = max(int((getattr(job, "final_result", None) or {}).get("review_truncated")
                        or 0), 0)
    return {"job_id": str(job_id), "status": status,
            "geometry_session_id": session_id,
            "groups": _groups(items), "decided": decided, "open": len(items) - decided,
            "truncated": truncated, "total": len(items) + truncated}


def _log_decision(job_id: uuid.UUID, *, item_id, group_key, action: str, proposal,
                  text: str | None, started: float, directive: dict | None = None) -> None:
    from meshpipeline.capture.logger import TrainingLogger
    TrainingLogger(str(job_id)).log("repair_review", op_id="review:decide", payload={
        "item": str(item_id) if item_id else None, "group": group_key, "action": action,
        "proposal": proposal, "text": text, "directive": directive,
        "latency_ms": int((time.perf_counter() - started) * 1000)})


async def _open_park_or_409(db, job, job_id: uuid.UUID, *, lock: bool = False):
    park = (await repo.open_park(db, job_id, lock=lock)
            if _status_of(job) == JobStatus.pending_review.value else None)
    if park is None:
        raise HTTPException(409, "This run is not waiting for a review")
    return park


async def _lock_park_or_409(db, job_id: uuid.UUID, park) -> None:
    """Take the park's row lock before writing an answer - the lock `run_review` holds from reading
    the answers to closing the park. Without it an answer saved after that read was dispatched
    without, or overwritten as `unreviewed`. A park the run closed meanwhile is a 409."""
    locked = await repo.open_park(db, job_id, lock=True)
    if locked is None or locked.id != park.id:
        raise HTTPException(409, "This review is already closed")


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
            direction, settled_on = _settled_direction(members)
            if direction is None:
                raise HTTPException(409, "Answer the sample first; apply-all repeats what you "
                                         "decided there")
            proposal = next((m.proposal for m in members if m.proposal), None)
            logged_directive = None
            await _lock_park_or_409(db, job_id, park)
            if direction == "leave":
                changed = await repo.record_group_decision(
                    db, job_id, group_key, "left",
                    {"action": "apply_all", "direction": "leave",
                     "from_item": str(settled_on.id)})
            elif direction == "redirect":
                # THE SAMPLE'S REDIRECT, applied to each remaining member on its own: the same
                # operation, aimed at that member alone, so each still runs individually through
                # the session (and a member it cannot be played on says so).
                shape = dict((settled_on.decision or {}).get("directive") or {})
                if not shape.get("op"):
                    raise HTTPException(409, "The sample's redirect carries no operation to "
                                             "repeat; answer the rest one by one")
                changed = 0
                for member in members:
                    if member.state != "open" or not member.fingerprint:
                        continue
                    directive = {"op": shape.get("op"), "targets": [member.fingerprint],
                                 "defect_code": member.defect_code,
                                 "defect_fingerprint": member.fingerprint,
                                 "source": "engineer", "item_id": str(member.id)}
                    try:
                        await repo.record_decision(db, member.id, "redirected", {
                            "action": "apply_all", "direction": "redirect",
                            "from_item": str(settled_on.id), "directive": directive})
                    except _park_closed() as exc:
                        raise HTTPException(409, "This review is already closed") from exc
                    changed += 1
                logged_directive = {"op": shape.get("op"), "from_item": str(settled_on.id)}
            else:
                if proposal is None:
                    raise HTTPException(409, "Nothing was proposed for this group, so there is "
                                             "nothing to apply")
                changed = await repo.record_group_decision(
                    db, job_id, group_key, "accepted",
                    {"action": "apply_all", "direction": "accept", "proposal": proposal})
            if not changed:
                raise HTTPException(409, "Every item in this group is already decided")
            await db.commit()
            _log_decision(job_id, item_id=None, group_key=body.group_key,
                          action=f"apply_all:{direction}", proposal=proposal, text=body.text,
                          started=started, directive=logged_directive)
            return {"kind": "applied", "changed": changed, "direction": direction}

        item = next((i for i in items if body.item_id is not None and i.id == body.item_id), None)
        if item is None:
            raise HTTPException(404, "Review item not found")

        covered: list = []
        text, playback = (body.text or "").strip(), ""
        if body.action == "redirect":
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
            playback = interp.playback
            directive = {**interp.directive, "item_id": str(item.id)}
            if interp.scope == "group":
                scoped = _group_scope(item, items, playback)
                if scoped is None:
                    reason = ("this defect is one of a pair, and the answer for one changes what "
                              "its partner means, so it can't cover the group - say what to do "
                              "with this one alone")
                    _log_decision(job_id, item_id=item.id, group_key=item.group_key,
                                  action="redirect_refused", proposal=item.proposal, text=text,
                                  started=started)
                    return {"kind": "refusal", "playback": reason, "reason": reason}
                covered, playback = scoped
                directive["targets"] = [fp for fp in [item.fingerprint, *(m.fingerprint
                                                                         for m in covered)] if fp]
            decision = {"action": "redirect", "text": text, "directive": directive,
                        "playback": playback, "scope": interp.scope}
            state = "redirected"
            reply = {"kind": "directive", "playback": playback,
                     "covered": [str(m.id) for m in covered]}
        elif body.action == "accept":
            if not isinstance(item.proposal, dict) or not item.proposal.get("op"):
                raise HTTPException(409, "Nothing was proposed for this defect; leave it or "
                                         "redirect it")
            decision, state, reply = ({"action": "accept", "proposal": item.proposal},
                                      "accepted", {"kind": "accepted"})
        else:
            decision, state, reply = {"action": "leave"}, "left", {"kind": "left"}

        await _lock_park_or_409(db, job_id, park)
        try:
            await repo.record_decision(db, item.id, state, decision)
            # THE MEMBERS A GROUP REDIRECT COVERED are answered by it: recorded as such, so they
            # neither go through as `unreviewed` nor carry a directive of their own (the one
            # directive, on this item, already targets them).
            for member in covered:
                await repo.record_decision(db, member.id, "redirected", {
                    "action": "redirect", "scope": "group", "covered_by": str(item.id),
                    "text": text, "playback": playback})
            # ...and a NEW answer on this item un-covers the members its old group redirect held:
            # they are open again, never left holding an answer nobody gave them.
            now_covered = {m.id for m in covered}
            orphaned = [m.id for m in items if m.id not in now_covered
                        and (m.decision or {}).get("covered_by") == str(item.id)]
            if orphaned:
                await repo.reopen_covered(db, orphaned)
        except _park_closed() as exc:
            raise HTTPException(409, "This review is already closed") from exc
        await db.commit()
    directive_out = decision.get("directive")
    _log_decision(job_id, item_id=item.id, group_key=item.group_key, action=body.action,
                  proposal=item.proposal, text=body.text, started=started,
                  directive=directive_out if isinstance(directive_out, dict) else None)
    return reply


_DIRECTIONS = {"accepted": "accept", "redirected": "redirect", "left": "leave"}


def _settled_direction(members: list) -> tuple[str | None, Any]:
    """`(direction, the item it was read from)`: what the engineer settled on in the sample.

    The sample is the group's first SAMPLE_SIZE members (what _groups serves as sampled). Only
    the engineer's own answers there count - an `unreviewed` or a group-covered member is not an
    answer about the group. All of them "leave it" means leave the rest; otherwise the LATEST
    answer that was not a leave is the direction (an accept, or a redirect whose operation the
    rest receive). Nothing answered yet: (None, None).
    """
    size = max(int(rcfg.CAD_REPAIR_REVIEW_SAMPLE_SIZE), 0)
    answered = sorted(
        (m for m in members[:size] if m.state in _DIRECTIONS and m.decided_at is not None
         and not (m.decision or {}).get("covered_by")),
        key=lambda m: m.decided_at)
    if not answered:
        return None, None
    acted = [m for m in answered if m.state != "left"]
    if not acted:
        return "leave", answered[-1]
    return _DIRECTIONS[acted[-1].state], acted[-1]


def _group_scope(item, items: list, playback: str) -> tuple[list, str] | None:
    """`(covered members, playback)` for a group-scoped redirect, or None when it cannot have one.

    A GROUP REDIRECT NEVER TARGETS A PAIRED DEFECT. The answer for one rim of a pairing changes
    what its partner means - one decision filling both would seal a bore - so paired members each
    need their own review, and the playback says they were left out. A redirect given ON a paired
    defect cannot cover the group at all (None). Members already answered keep their answer.
    """
    if item.pairing_key:
        return None
    same = [m for m in items if m.group_key == item.group_key and m.id != item.id]
    # OPEN MEMBERS, AND THOSE THIS ITEM'S OWN EARLIER GROUP ANSWER COVERED: replacing that answer
    # with another group answer keeps them in it rather than reopening them. Members answered on
    # their own keep their answer.
    mine = str(item.id)
    covered = [m for m in same if m.fingerprint and not m.pairing_key
               and (m.state == "open" or (m.decision or {}).get("covered_by") == mine)]
    paired = [m for m in same if m.pairing_key]
    if paired:
        n = len(paired)
        playback = (f"{playback} - {n} paired {'defect' if n == 1 else 'defects'} in this group "
                    f"{'is' if n == 1 else 'are'} left out: each one needs its own review")
    return covered, playback


def _replay_directives(kept: list) -> list[dict]:
    out = []
    for op in kept or []:
        if not isinstance(op, dict):
            continue
        name = op.get("name") or op.get("op")
        targets = [t for t in (op.get("targets") or []) if t]
        if not name or not targets:
            continue
        # AN ENGINEER-APPROVED OPERATION REPLAYS AS THE ENGINEER'S, with its item: re-labelled
        # "replay" it would meet the rim guard as corrupt state (an approved fill came back
        # fill_refused next cycle) and its outcome would never reach the item it answers.
        engineer = op.get("source") == "engineer" and bool(op.get("item_id"))
        out.append({"op": name, "targets": targets, "defect_code": op.get("defect_code", ""),
                    "defect_fingerprint": targets[0],
                    "source": "engineer" if engineer else "replay",
                    "item_id": str(op["item_id"]) if engineer else ""})
    return out


def _engineer_directives(item, items: list) -> list[dict]:
    """The directives an item's answer sends: one, except a GROUP FILL, which is one per hole.

    `fill_planar_hole` patches exactly one hole, while the session's progress rule needs every
    target's defect gone - so one fill aimed at "all these holes" patched the first, left the rest,
    and rolled back whole. Each hole gets its own fill, owned by the member it answers (so the
    outcome writes back onto that member), and marked `group` so it still meets the rim guard: the
    engineer looked at one defect, not at each of these.
    """
    from meshpipeline.cad.repair.operations import FillPlanarHole

    directive = _engineer_directive(item)
    if directive is None:
        return []
    targets = [t for t in directive.get("targets") or () if t]
    if (item.state != "redirected" or directive.get("op") != FillPlanarHole.name
            or len(targets) < 2):
        return [directive]
    owners = {m.fingerprint: m for m in items
              if m.fingerprint and (m.decision or {}).get("covered_by") == str(item.id)}
    if item.fingerprint:
        owners[item.fingerprint] = item
    out = []
    for fp in targets:
        owner = owners.get(fp, item)
        out.append({**directive, "targets": [fp], "defect_fingerprint": fp,
                    "defect_code": owner.defect_code or directive.get("defect_code", ""),
                    "item_id": str(owner.id), "group": True})
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
        # THE PARK'S ROW LOCK, held to the commit that closes it: a decision route answering
        # meanwhile waits for it, then finds the park closed (409) - never read stale, never
        # overwritten as `unreviewed` below.
        park = await _open_park_or_409(db, job, job_id, lock=True)
        items = await repo.items_for(db, job_id, park.id)
        decided = sorted((i for i in items if i.state in ("accepted", "redirected")),
                         key=lambda i: (i.decided_at is None, i.decided_at))
        directives = _replay_directives(park.kept_operations)
        directives += [d for item in decided for d in _engineer_directives(item, items)]

        # The status CAS first: of two racing runs only one wins, and only it closes the park.
        if await job_repo.transition(db, job_id, JobStatus.pending) != TransitionResult.applied:
            raise HTTPException(409, "This run is no longer waiting for a review")
        # THE RE-RUN REUSES THIS ROW: the parked execution's lease, execution id, deadline and
        # billed span are reset beside the CAS, so the re-run claims cleanly, gets its own budget
        # and is charged for execution time only (job_repository.review_rerun_reset).
        # `released_at` is the release mark the reset leaves (lease_expires_at): the stalled-job
        # reaper relaunches a pending job still carrying it, and a confirmed dispatch clears it.
        released_at = datetime.now(UTC)
        await job_repo.reset_for_review_rerun(db, job_id,
                                              started_at=getattr(job, "started_at", None),
                                              ended_at=getattr(job, "ended_at", None),
                                              now=released_at)
        unanswered = [i.id for i in items if i.state == "open"]
        try:
            for item_id in unanswered:
                await repo.record_decision(db, item_id, "unreviewed", {"action": "unreviewed"})
        except _park_closed() as exc:
            raise HTTPException(409, "This run is no longer waiting for a review") from exc
        await repo.close_park(db, park.id, "run")
        payload = {**dict(job.dispatch_payload or {}), "repair_directives": directives,
                   "review_continuation": True}
        # THE RE-RUN'S PAYLOAD IS WRITTEN IN THIS TRANSACTION, so a reaper relaunch after a crash
        # before the dispatch carries the directives, not the original payload.
        await job_repo.set_dispatch_payload(db, job_id, payload)
        await db.commit()

    # NOT admitted again: this is the parked run's own purchase, and the park deliberately did
    # not charge it - the re-run's ending does, once.
    #
    # THE STATUS FLIP COMMITS BEFORE THE DISPATCH, on purpose: dispatching first would let the
    # worker race a still-pending_review row into an illegal pending_review -> running. So a failed
    # dispatch is COMPENSATED below instead. A CRASH between the commit above and the dispatch
    # leaves a pending, unowned job carrying the release mark and the re-run's payload - the
    # stalled-job reaper's "released but never confirmed queued" rule relaunches it.
    from meshpipeline.application import pipeline_run
    try:
        async with get_db() as db:
            await pipeline_run.dispatch(db, str(job_id), payload)
    except Exception as exc:
        logger.error("review re-run of job %s could not be dispatched", job_id, exc_info=True)
        await _compensate(job_id, park.id, unanswered)
        raise HTTPException(503, "The repair run could not be resumed just now. Your decisions "
                                 "are kept; try again in a moment.") from exc
    # THE LAUNCHER TOOK IT: clear the release mark so the reaper does not launch it a second
    # time. Best effort - a mark left behind costs at most one refused duplicate claim.
    try:
        async with get_db() as db:
            await lease_repo.confirm_requeued(db, job_id, released_at)
            await db.commit()
    except Exception:  # noqa: BLE001 - the run is launched; this is bookkeeping
        logger.warning("review re-run of job %s: release mark not cleared", job_id, exc_info=True)
    return {"job_id": str(job_id), "status": "pending", "directives": len(directives)}


async def _compensate(job_id: uuid.UUID, park_id: uuid.UUID, unanswered: list) -> None:
    """Put a job whose dispatch failed back where the engineer left it: parked and decidable."""
    from meshpipeline.persistence.job_state import TransitionResult
    try:
        async with get_db() as db:
            # PINNED to `pending`: only the job this route itself flipped and nothing claimed. A
            # job a worker already took (running) is never pulled back into review.
            back = await job_repo.transition(db, job_id, JobStatus.pending_review,
                                             allow={JobStatus.pending})
            if back == TransitionResult.applied:
                await repo.reopen_park(db, park_id)
                await repo.restore_open(db, unanswered)
            await db.commit()
    except Exception:  # noqa: BLE001 - the 503 below is still the honest answer
        logger.error("review compensation for job %s failed", job_id, exc_info=True)
