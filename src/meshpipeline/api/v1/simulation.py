# Responsibility: Serve a job's status, surface and dispute endpoints.
# Boundaries: transport over the job service; it computes no result and reads no worker filesystem.
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import pagination
from meshpipeline.api.schemas import listing
from meshpipeline.api.schemas.job import (
    ArtifactOut,
    CancelIn,
    CancelOut,
    DisputeIn,
    DisputeOut,
    JobStatus_,
)
from meshpipeline.api.security import org_dep, owner_dep, plan_dep
from meshpipeline.application import spend_gate
from meshpipeline.application.job_service import JobService
from meshpipeline.persistence.models import ArtifactType
from meshpipeline.persistence.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()
svc = JobService()


def _amended_brief(parent_session, mode: str, comment: str) -> str:
    base = (parent_session.review_brief_txt or "") if parent_session else ""
    if mode != "accept" or not (comment or "").strip():
        return base
    return (base + "\n\nUSER ACCEPTANCE (authoritative - the engineer who owns this "
            "study has inspected the mesh and stated what is acceptable for it):\n"
            + comment.strip()[:2000])


@dataclass(frozen=True)
class _ParentIntake:
    """What the disputed run was approved with, as the dispute child inherits it."""

    session_id: str = ""
    review_brief_txt: str = ""
    intake_patches: list = field(default_factory=list)
    dimensionality: str = ""
    purpose: str = ""
    input_kind: str = ""


def _parent_intake(parent_session, dispatch_payload) -> _ParentIntake:
    """The parent's intake context: its session while the session is still linked to it, else
    the parent's own dispatch payload - the exact set it was approved and run with.

    The session is not the durable record of a run. Once a run has ended the conversation moves
    on (agents/intake/message.py releases the link and a later run rewrites the columns), so a
    dispute raised after that found no session and ran the child with no purpose, no patches
    and no brief. The payload has carried all of them since dispatch.
    """
    if parent_session is not None:
        return _ParentIntake(
            session_id=str(parent_session.id),
            review_brief_txt=str(parent_session.review_brief_txt or ""),
            intake_patches=list(parent_session.intake_patches or []),
            dimensionality=str(parent_session.dimensionality or ""),
            purpose=str(parent_session.purpose or ""),
            input_kind=str(parent_session.input_kind or ""))
    payload = dict(dispatch_payload or {})
    return _ParentIntake(
        session_id=str(payload.get("session_id") or ""),
        review_brief_txt=str(payload.get("review_brief_txt") or ""),
        intake_patches=list(payload.get("intake_patches") or []),
        dimensionality=str(payload.get("dimensionality") or ""),
        purpose=str(payload.get("purpose") or ""),
        input_kind=str(payload.get("input_kind") or ""))


async def _viewer_data_or_empty(job_id: uuid.UUID, owner_id: str, db=None, *,
                                organization_id: str = "") -> dict:
    try:
        return await _viewer_data(job_id, owner_id, db, organization_id=organization_id)
    except HTTPException as exc:
        # Job status must still render when the viewer payload is absent OR the object store is
        # briefly unreachable - a viewer problem is not a reason to fail the whole status page.
        # The dedicated viewer routes still surface the typed failure to the caller that asked
        # for it specifically.
        logger.info("viewer data unavailable for job %s (%s) - status degrades",
                    job_id, exc.status_code)
        return {}


async def _viewer_data(job_id: uuid.UUID, owner_id: str, db=None, *,
                       organization_id: str = "") -> dict:
    import json as _json

    from meshpipeline.application.viewer_payload import VIEWER_LOGICAL_KEY
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    async def _resolve(session):
        job = await svc.get_job(session, job_id, owner_id, organization_id=organization_id)
        if not job:
            raise HTTPException(404, "Job not found")
        if _is_cancelled(job):
            # a cancelled run delivers nothing, whatever a worker managed to store before the
            # cancel committed (see the status route)
            raise HTTPException(404, "This run was cancelled; it has no delivered mesh")
        return await ArtifactRepository().get_by_logical_key(session, job_id, VIEWER_LOGICAL_KEY)

    # Reuse the caller's session where the route already opened one: a second connection for the
    # same request is wasted work, and it made the lookup unreachable from the route's own
    # transaction.
    if db is not None:
        row = await _resolve(db)
    else:
        async with get_db() as _db:
            row = await _resolve(_db)
    if row is None:
        raise HTTPException(404, "Viewer data not available for this job")
    try:
        raw = await asyncio.to_thread(
            lambda: get_object_store().get_bytes(object_key=row.storage_key))
    except Exception as exc:  # noqa: BLE001 - the object store is a dependency, not the caller
        logger.warning("viewer data unreadable for job %s: %s", job_id, type(exc).__name__)
        raise HTTPException(503, "Viewer data is temporarily unavailable") from exc
    try:
        return _json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        # A row pointing at bytes that are not the payload is an integrity failure, never a 200.
        logger.error("viewer data for job %s is not valid JSON", job_id)
        raise HTTPException(500, "Viewer data is corrupt") from exc


def _is_cancelled(job) -> bool:
    from meshpipeline.persistence.models import JobStatus as _JS
    status = getattr(job, "status", None)
    return getattr(status, "value", status) == _JS.cancelled.value


def _artifact_label(artifact_type, engine: str) -> str:
    _t = getattr(artifact_type, "value", str(artifact_type))
    if _t == "mesh":
        return "Surface preview (.msh)"
    if _t == "mesh_bundle":
        try:
            from meshpipeline.engines.registry import get_spec
            return f"{get_spec(engine).deliverable.label} (.tar.gz)"
        except Exception:  # noqa: BLE001 - an unknown engine still gets a true name
            return "Mesh case (.tar.gz)"
    return _t.replace("_", " ").capitalize()


def _failed_concerns(review: dict, engine: str) -> list[str]:
    findings = review.get("axis_findings") or []
    if isinstance(findings, list):
        failed = [str(f.get("axis_key", "")) for f in findings
                  if isinstance(f, dict) and f.get("passed") is False and f.get("axis_key")]
    else:
        return []
    if not failed:
        return []
    try:
        from meshpipeline.engines.registry import get_spec
        axes = {a.name: a for a in get_spec(engine).review_rubric}
    except Exception:  # noqa: BLE001
        axes = {}
    out = []
    for name in failed:
        ax = axes.get(name)
        # An axis with no declared concern must NOT fall back to its key - say the
        # honest generic thing instead. (A test forbids the empty concern anyway.)
        out.append(ax.concern if ax and ax.concern
                   else "The reviewer found a problem it could not put in words here")
    return out


@router.get("")
async def list_jobs(limit: int = pagination.DEFAULT_LIMIT, cursor: str | None = None,
                    owner_id: str = Depends(owner_dep),
                    organization_id: str = Depends(org_dep)) -> dict:
    # DECLARED BEFORE `/{job_id}`. FastAPI matches in declaration order, so a collection route
    # placed after that one is unreachable - "" would be captured as a job id.
    bounded = pagination.clamp_limit(limit)
    async with get_db() as db:
        # ONE MORE ROW THAN THE PAGE - see listing.look_ahead. A full page is not evidence that
        # another page exists, and the difference is what decides whether a cursor is minted.
        rows = await svc.list_runs(db, owner_id, organization_id=organization_id,
                                   limit=listing.look_ahead(bounded),
                                   before=pagination.decode_cursor(cursor))
    return listing.page(
        rows, limit=bounded,
        item=lambda row: {
            "id": str(row[0].id),
            "status": getattr(row[0].status, "value", row[0].status),
            "task_label": row[1],
            # A DISPUTE CHILD HAS NO SESSION, so it has no label: the re-review creates a fresh
            # job and links no conversation to it. Saying so is what stops every re-review in the
            # list reading as an indistinguishable "Untitled study".
            "is_rerun": row[0].dispute_operation_key is not None,
            "created_at": row[0].created_at.isoformat() if row[0].created_at else None,
            "ended_at": row[0].ended_at.isoformat() if row[0].ended_at else None,
            "attempts": row[0].current_attempt,
            "failed_reason": getattr(row[0].failed_reason, "value", row[0].failed_reason),
        },
        key=lambda row: (row[0].created_at, row[0].id))


@router.get("/{job_id}", response_model=JobStatus_)
async def get_job(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                  organization_id: str = Depends(org_dep)):
    async with get_db() as db:
        job = await svc.get_job(db, job_id, owner_id, organization_id=organization_id)
        if not job:
            raise HTTPException(404, "Job not found")

        _vdata = await _viewer_data_or_empty(job_id, owner_id, db,
                                             organization_id=organization_id)
        _engine = str(_vdata.get("engine", "") or "")
        artifacts_out = []
        # A CANCELLED RUN LISTS NO DOWNLOADS. The registration fence stops a worker's rows landing
        # after the cancel commits, but a worker that registered them a moment BEFORE it did left
        # rows behind - on a job its owner stopped. They are never offered: what the owner asked
        # for was the run to end, and a mesh from it is not a delivery.
        for a in ([] if _is_cancelled(job) else (job.artifacts or [])):
            # The viewer payload is what the VIEWER consumes, not something the user downloads.
            # Listing it would put an internal JSON blob in the deliverables panel beside the
            # engine case.
            if a.artifact_type == ArtifactType.viewer_data:
                continue
            artifacts_out.append(
                ArtifactOut(
                    id=a.id,
                    artifact_type=a.artifact_type,
                    download_url=await svc.signed_url(a.storage_key),
                    size_bytes=a.size_bytes,
                    created_at=a.created_at,
                    label=_artifact_label(a.artifact_type, _engine),
                )
            )

        _review = _vdata.get("review") or {}
        # The DURABLE application-rendered terminal verdict (never model prose), reproduced from the
        # persisted final_result so a restarted API returns the same result.
        _fr_dict = getattr(job, "final_result", None)
        if not isinstance(_fr_dict, dict):
            _fr_dict = None
        _final_message = None
        if _fr_dict:
            try:
                from meshpipeline.application.final_result import FinalResult, render_message
                _final_message = render_message(FinalResult.from_dict(_fr_dict))
            except Exception:  # noqa: BLE001 - a malformed/old-version record never breaks the read
                _final_message = None

        return JobStatus_(
            id=job.id,
            status=job.status,
            current_attempt=job.current_attempt,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            ended_at=job.ended_at,
            artifacts=artifacts_out,
            mesh_available=bool(_vdata.get("mesh_available")),
            reviewer_verdict=_review.get("verdict"),
            reviewer_reasoning=_review.get("reasoning", ""),
            reviewer_findings=_failed_concerns(_review, _engine),
            final_message=_final_message,
            final_result=_fr_dict,
            # a string or nothing: a row from before the column carries none, and a test double must
            # not be able to smuggle an object into the wire contract
            cancel_reason=(job.cancel_reason if isinstance(getattr(job, "cancel_reason", None), str)
                           else None),
            worker_wake_minutes=worker_wake_estimate(job),
        )


def worker_wake_estimate(job) -> int | None:
    """The minutes a pending job may wait for a worker, or None once one has it.

    Pure over the row: `pending`/`queued` with no `started_at` is the only state in which the
    fleet may still be waking, and WORKER_WAKE_MINUTES (0 = no estimate) is the only number."""
    import meshpipeline.settings.runtime as rtcfg
    _status = getattr(job.status, "value", job.status)
    if _status not in ("pending", "queued") or getattr(job, "started_at", None) is not None:
        return None
    minutes = int(rtcfg.WORKER_WAKE_MINUTES or 0)
    return minutes if minutes > 0 else None


@router.get("/{job_id}/surface")
async def get_surface(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                      organization_id: str = Depends(org_dep)):
    data = await _viewer_data(job_id, owner_id, organization_id=organization_id)
    surface = data.get("surface")
    if not surface:
        raise HTTPException(404, "No renderable surface was delivered for this job")
    out = {"job_id": str(job_id), **surface}
    up = await _up_axis_of(job_id, owner_id, organization_id)
    if up:
        out["up_axis"] = up
    return out


async def _up_axis_of(job_id: uuid.UUID, owner_id: str, organization_id: str) -> str | None:
    """Which way is up for the part a job meshed - what the user confirmed on its geometry check,
    else what the check proposed - for the viewer to open with. None when the job had no check or
    it cannot be read: the viewer then opens z-up, as it always has. Display only."""
    try:
        async with get_db() as db:
            job = await svc.get_job(db, job_id, owner_id, organization_id=organization_id)
        payload = (getattr(job, "dispatch_payload", None) or {}) if job is not None else {}
        session_id = str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
        if not session_id:
            return None
        from meshpipeline.application.geometry_check import stored_up_axis
        return await asyncio.to_thread(stored_up_axis, session_id)
    except Exception as exc:  # noqa: BLE001 - a view preference never costs the user their mesh
        logger.info("surface: which way is up unavailable for job %s (%s)", job_id, type(exc).__name__)
        return None


@router.get("/{job_id}/surface.vtk")
async def get_surface_vtk(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                          organization_id: str = Depends(org_dep)):
    """The delivered boundary with its quality fields as a legacy VTK file, for ParaView. The
    arrays are the viewer's own, so what ParaView colours is what the heatmap coloured. Built from
    the stored viewer payload - no workspace is read."""
    from fastapi.responses import Response

    from meshpipeline.render.vtk_export import surface_to_vtk
    data = await _viewer_data(job_id, owner_id, organization_id=organization_id)
    surface = data.get("surface")
    if not surface or surface.get("kind") != "polymesh":
        raise HTTPException(404, "No polyMesh surface was delivered for this job")
    try:
        body = await asyncio.to_thread(surface_to_vtk, surface)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=body, media_type="application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="mesh_quality_{str(job_id)[:8]}.vtk"'})


#: The one sanitized failure this route returns when a dispute child could not be queued. A repeat of
#: an operation whose child already failed to launch is answered with the SAME text, so the public
#: contract gains no new shape from the retry policy.
_DISPATCH_FAILED = "Failed to enqueue dispute job"


@router.post("/{job_id}/dispute", response_model=DisputeOut, status_code=202)
async def dispute_job(job_id: uuid.UUID, body: DisputeIn, owner_id: str = Depends(owner_dep),
                      plan: str = Depends(plan_dep),
                      organization_id: str = Depends(org_dep)):
    from meshpipeline.application import dispute_operation
    from meshpipeline.persistence.models import JobStatus as _JS
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    if not body.flags and not (body.comment or "").strip():
        raise HTTPException(422, "Flag at least one region or describe the change you want")
    if len(body.flags) > polcfg.DISPUTE_MAX_FLAGS:
        raise HTTPException(422, f"Too many flagged regions (max {polcfg.DISPUTE_MAX_FLAGS})")

    # BUILT BEFORE THE TRANSACTION, because it is what the operation is identified by. This is the
    # same dict that is dispatched below - identity and intent cannot drift apart if there is only
    # one of them.
    _mode = body.mode if body.mode in ("rebuild", "accept") else "rebuild"
    user_dispute = {
        "of_job_id": str(job_id),
        "mode": _mode,
        "comment": (body.comment or "")[:2000],
        "flags": [{
            "x": f.x, "y": f.y, "z": f.z,
            **({"span": f.span} if f.span else {}),
            "patch": (f.patch or "")[:128],
            "note": (f.note or "")[:500],
        } for f in body.flags],
    }
    _operation = dispute_operation.operation_key(owner_id, job_id, user_dispute)

    job_repo = JobRepository()
    session_repo = SessionRepository()
    async with get_db() as db:
        parent = await svc.get_job(db, job_id, owner_id, organization_id=organization_id)
        if not parent:
            raise HTTPException(404, "Job not found")
        # A FAILED run that still produced a mesh is disputable too: the retry loop
        # gave up, but the mesh exists and the user is entitled to judge it (that is
        # the whole point of the human-in-the-loop). Only a run with NOTHING to look
        # at is refused.
        if parent.status == _JS.cancelled:
            raise HTTPException(409, "This run was cancelled, so there is no mesh to review - "
                                     "start a new run in the chat instead")
        if parent.status not in (_JS.succeeded, _JS.failed):
            raise HTTPException(409, "This job has not finished yet")
        if parent.status == _JS.failed:
            # A failed run is disputable ONLY if its final mesh actually reached the
            # reviewer - which is proof the EXECUTOR GATES PASSED (route_after_executor
            # sends a gate-failing mesh straight to the outcome, never to the reviewer).
            # So a stored verdict is the gate-validity certificate. Without it the mesh
            # is invalid, and no amount of user acceptance may deliver it: the reviewer's
            # quality bar is amendable, the gates are NOT.
            _dv = await _viewer_data_or_empty(job_id, owner_id, db,
                                              organization_id=organization_id)
            if not (_dv.get("mesh_available") and _dv.get("review")):
                raise HTTPException(409, "This run produced no valid mesh to review - "
                                         "describe the change in the chat instead")
        if parent.workspace_purged:
            raise HTTPException(409, "This job's mesh workspace has been purged and can "
                                     "no longer be re-reviewed")

        # IS THIS OPERATION ALREADY DONE? Asked before the quota check on purpose: a repeat creates
        # nothing, so it must not be refused for capacity, and it must not consume any. A child that
        # exists but failed to launch is not "on its way", so its repeat gets the same sanitized
        # failure instead of a 202 that would be untrue.
        # dispute_operation.claim stays owner_id-only: it lives in application/, not in
        # persistence/repositories, and its key already folds owner_id into the digest
        # (operation_key's own docstring: "no two tenants can ever collide on a key"), so
        # widening its predicate is not needed for isolation - only application-layer files this
        # task does not touch would benefit, and that is a design question for later work, not a
        # missed site.
        _replay = await dispute_operation.claim(db, owner_id=owner_id, key=_operation)
        if _replay is not None:
            if _replay.launch_failed:
                raise HTTPException(500, _DISPATCH_FAILED)
            return DisputeOut(job_id=_replay.job_id, dispute_of=job_id, flags=len(body.flags))

        try:
            # A DISPUTE IS A NEW RUN and is charged like one, so it passes the same credit gate as
            # a first approval. A key credential's own plan still wins when it names one; a console
            # caller's is the organisation's.
            _billed_plan = await spend_gate.admit(db, owner_id=owner_id,
                                                  organization_id=organization_id)
            await svc.check_quotas(db, owner_id, plan=plan or _billed_plan)
        except spend_gate.OutOfCredits as exc:
            raise HTTPException(402, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(429, str(exc)) from exc

        # Parent intake context: patches/dimensionality/brief from the session while it is still
        # linked to this run, else from the run's own dispatch payload (see _parent_intake);
        # request.txt is recovered from the parent workspace by the worker.
        _intake = _parent_intake(await session_repo.get_by_job_id(db, job_id),
                                 parent.dispatch_payload)

        new_job = await job_repo.create(db, owner_id=owner_id, organization_id=organization_id)
        # The child IS the operation's durable record. Written in the same transaction that creates
        # it, under the claim's lock, so the row and its identity become visible together.
        new_job.dispute_operation_key = _operation
        new_job.geometry_source_id = parent.geometry_source_id
        # A dispute re-runs the parent's geometry, so it inherits the parent's SCALE unchanged -
        # never a fresh reading. Re-resolving would let a correction recorded after the disputed
        # run silently change the size of the very result the user is disputing.
        new_job.geometry_interpretation_id = parent.geometry_interpretation_id
        _parent_interpretation = None
        if parent.geometry_interpretation_id:
            from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
                GeometryInterpretationRepository,
            )
            _parent_interpretation = await GeometryInterpretationRepository().get_for_owner(
                db, parent.geometry_interpretation_id, owner_id,
                organization_id=organization_id)
        await db.commit()

    try:
        from meshpipeline.application.dispatch_contract import build as _build_dispatch
        from meshpipeline.contracts.geometry_source import (
            GeometryInterpretationRef,
            GeometrySourceRef,
        )
        _payload = _build_dispatch(**{
            "job_id":           str(new_job.id),
            "owner_id":         owner_id,
            "geometry_source":  (GeometrySourceRef.from_row(parent.geometry_source).to_payload()
                                 if parent.geometry_source else None),
            "geometry_interpretation": (
                GeometryInterpretationRef.from_domain(_parent_interpretation).to_payload()
                if parent.geometry_source and _parent_interpretation else None),
            "session_id":       _intake.session_id,
            "request_txt":      "",   # worker recovers the parent's request.txt
            "review_brief_txt": _amended_brief(_intake, _mode, body.comment),
            "intake_patches":   list(_intake.intake_patches),
            "dimensionality":   _intake.dimensionality,
            "purpose":          _intake.purpose,
            "input_kind":       _intake.input_kind,
            "user_dispute":     user_dispute,
        })
        # The child is already committed, deliberately, so a worker that takes the message
        # immediately can see the job it names. The authority below owns the consequence: it
        # dispatches inside its own session scope - `db` above closed at the commit, and a
        # SQLAlchemy session stays usable after close(), so dispatching through it would check out
        # a SECOND connection nothing was responsible for (measured against real PostgreSQL: three
        # such failures grew the checked-out count 1 -> 2 -> 3, unreclaimed by gc.collect()) - and
        # it records a durable launch failure if the broker never took the message, so the child
        # cannot sit pending forever against the owner's quota.
        await dispute_operation.dispatch_or_record_failure(
            new_job.id, _payload, parent_job_id=job_id, key=_operation)
    except Exception as exc:
        raise HTTPException(500, _DISPATCH_FAILED) from exc

    return DisputeOut(job_id=new_job.id, dispute_of=job_id, flags=len(body.flags))


@router.post("/{job_id}/cancel", response_model=CancelOut)
async def cancel_job(job_id: uuid.UUID, body: CancelIn | None = None,
                     owner_id: str = Depends(owner_dep),
                     organization_id: str = Depends(org_dep)):
    """Stop a run the caller owns. Transport over the cancel authority, which owns the one
    transaction: the worker's eviction, the terminal `cancelled` record, its announcement on the
    stream and the release of the owner's slot and the tenant's credit reserve all land in that
    commit. The caller's own repeat is answered the same way; a job that already finished is
    refused, because a delivered mesh or a recorded failure is not something a cancel may undo."""
    from meshpipeline.application import job_cancel

    outcome = await job_cancel.cancel_job(job_id, owner_id=owner_id,
                                          organization_id=organization_id,
                                          reason=(body.reason if body else "") or "")
    status = outcome.status
    if outcome.result is job_cancel.CancelResult.not_found or status is None:
        raise HTTPException(404, "Job not found")
    if outcome.result is job_cancel.CancelResult.already_finished:
        raise HTTPException(409, f"This job has already {status.value}; there is nothing to cancel")
    return CancelOut(job_id=job_id, status=status, cancel_reason=outcome.cancel_reason,
                     already_cancelled=outcome.result is job_cancel.CancelResult.already_cancelled)
