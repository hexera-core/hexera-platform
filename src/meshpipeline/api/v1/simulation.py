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
    DisputeFace,
    DisputeIn,
    DisputeOut,
    DownloadLinkOut,
    JobStatus_,
)
from meshpipeline.api.security import org_dep, owner_dep, plan_dep
from meshpipeline.application import spend_gate
from meshpipeline.application.job_service import JobService
from meshpipeline.contracts import engineering_text
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


def _artifact_label(artifact_type, engine: str, logical_key: str = "") -> str:
    _t = getattr(artifact_type, "value", str(artifact_type))
    # an FEA mesh's solver files, one per solver, each saying whether that solver has really run it
    from meshpipeline.engines.cfd_exports import EXPORT_ARTIFACTS as FLOW_EXPORTS
    from meshpipeline.engines.fea_writers import EXPORT_ARTIFACTS
    # ...and a flow mesh's: the OpenFOAM case, the Fluent mesh, the EnSight case (engines/cfd_exports.py)
    for _key, _file, _label in (*EXPORT_ARTIFACTS, *FLOW_EXPORTS):
        if logical_key == _key:
            return _label
    if _t == "mesh":
        return "Surface preview (.msh)"
    if _t == "repair_report":
        return "CAD inspection report (.json)"
    if _t == "repaired_cad":
        # NAMED, not left to the fallback below, which rendered it "Repaired cad" - a download
        # button whose own label looked like a bug on the most consequential file we hand back.
        return "Repaired geometry (.step)"
    if _t == "returned_cad":
        # the customer's own file handed back as it arrived (a repair that kept nothing): the label
        # must never let it pass for a repaired one
        return "Your geometry, unchanged (.step)"
    if _t == "mesh_hints":
        return "Mesh-size hints (.json)"
    if _t == "mesh_bundle":
        try:
            from meshpipeline.engines.registry import get_spec
            return f"{get_spec(engine).deliverable.label} (.tar.gz)"
        except Exception:  # noqa: BLE001 - an unknown engine still gets a true name
            return "Mesh case (.tar.gz)"
    return _t.replace("_", " ").capitalize()


def _menu_rank(artifact) -> int:
    """Where a download sits in the menu: the bundle first, then the solver files in their table's
    order (engines/fea_writers, engines/cfd_exports), then the rest as stored."""
    from meshpipeline.engines.cfd_exports import EXPORT_ARTIFACTS as FLOW_EXPORTS
    from meshpipeline.engines.fea_writers import EXPORT_ARTIFACTS
    if getattr(artifact.artifact_type, "value", artifact.artifact_type) == ArtifactType.mesh_bundle.value:
        return 0
    keys = [k for k, _f, _l in (*FLOW_EXPORTS, *EXPORT_ARTIFACTS)]
    key = str(getattr(artifact, "logical_key", "") or "")
    return 1 + keys.index(key) if key in keys else 1000


def _surface_preview_hidden(artifact, engine: str) -> bool:
    """A flow mesh's boundary .msh ("Surface preview") is the viewer's own file, not a download: it is
    no longer stored for a flow mesh (application/artifact_uploader), and one an older run stored is
    not offered either."""
    from meshpipeline.engines.cfd_exports import ENGINES
    return (engine in ENGINES and str(getattr(artifact, "logical_key", "") or "") == ArtifactType.mesh.value
            and getattr(artifact.artifact_type, "value", artifact.artifact_type) == ArtifactType.mesh.value)


#: The durable record's verdict, in the words the job status has always served.
_SERVED_VERDICT = {"passed": "PASS", "failed": "FAIL"}


def served_review(final_result: dict | None, review: dict) -> dict:
    """The review the job status serves - its verdict, and the reviewer's words that go with it.

    Once a job is finalized, its durable record (final_result) is the one authority on whether a
    review concluded and what it concluded. The viewer payload's `review` block is only a copy of
    the last review's verdict.json, and a review that ended WITHOUT a verdict used to write "FAIL"
    there: job 4f18812f was delivered with its review unfinished (final_result.reviewer_verdict
    null) and its status read "FAIL". So a finalized job serves a verdict, reasoning and findings
    only when its record says the review COMPLETED, and the verdict is the record's own. Before a
    record exists the payload is all there is, and only a real PASS or FAIL in it is served."""
    review = review if isinstance(review, dict) else {}
    if not isinstance(final_result, dict) or not final_result:
        if review.get("verdict") in ("PASS", "FAIL"):
            return review
        return {**review, "verdict": None}
    verdict = _SERVED_VERDICT.get(str(final_result.get("reviewer_verdict") or ""))
    if final_result.get("review_execution") != "completed" or verdict is None:
        # no concluded review on this run: nothing to judge by, so no verdict and no findings -
        # a retained earlier review's words must not read as this run's judgement either
        return {}
    return {**review, "verdict": verdict}


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
            # "delivered_with_concerns" beside `succeeded` - a run list must not read as a pass
            "review_outcome": _review_outcome(getattr(row[0], "final_result", None)),
        },
        key=lambda row: (row[0].created_at, row[0].id))


def _review_outcome(final_result) -> str | None:
    from meshpipeline.application.final_result import review_outcome_of
    try:
        return review_outcome_of(final_result if isinstance(final_result, dict) else None)
    except Exception:  # noqa: BLE001 - an old or malformed record never breaks a read
        return None


def _review_notes(final_result) -> list[str]:
    from meshpipeline.application.final_result import review_notes_of
    try:
        return review_notes_of(final_result if isinstance(final_result, dict) else None)
    except Exception:  # noqa: BLE001 - an old or malformed record never breaks a read
        return []


def _review_concerns(final_result) -> list[str]:
    from meshpipeline.application.final_result import review_concerns_of
    try:
        return review_concerns_of(final_result if isinstance(final_result, dict) else None)
    except Exception:  # noqa: BLE001 - an old or malformed record never breaks a read
        return []


@router.get("/{job_id}/artifacts/{artifact_id}/download", response_model=DownloadLinkOut)
async def download_link(job_id: uuid.UUID, artifact_id: uuid.UUID,
                        owner_id: str = Depends(owner_dep),
                        organization_id: str = Depends(org_dep)):
    """A NEW download link for one deliverable, minted when the user clicks Download.

    The links in the job read are minted when the page loads; a user who reads the result for
    longer than a link lives, or whose large download drops and resumes later, met a 403. The
    console asks here at click time instead, and again once if that download is refused, so the
    link it uses always has its whole life ahead of it - a life that scales with the file's size.
    Served for exactly the rows the job read lists: the caller's own job, never a cancelled one,
    never the viewer's internal payload.
    """
    async with get_db() as db:
        job = await svc.get_job(db, job_id, owner_id, organization_id=organization_id)
        if not job:
            raise HTTPException(404, "Job not found")
        artifact = next((a for a in ([] if _is_cancelled(job) else (job.artifacts or []))
                         if str(a.id) == str(artifact_id)
                         and a.artifact_type != ArtifactType.viewer_data), None)
        if artifact is None:
            raise HTTPException(404, "Download not found")
        storage_key, size_bytes = artifact.storage_key, artifact.size_bytes
    url, ttl_s = await svc.fresh_download_link(storage_key, size_bytes)
    return DownloadLinkOut(artifact_id=artifact_id, download_url=url,
                           expires_in_seconds=ttl_s, size_bytes=int(size_bytes or 0))


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
        for a in ([] if _is_cancelled(job) else sorted(job.artifacts or [], key=_menu_rank)):
            # The viewer payload is what the VIEWER consumes, not something the user downloads.
            # Listing it would put an internal JSON blob in the deliverables panel beside the
            # engine case.
            if a.artifact_type == ArtifactType.viewer_data:
                continue
            if _surface_preview_hidden(a, _engine):
                continue
            artifacts_out.append(
                ArtifactOut(
                    id=a.id,
                    artifact_type=a.artifact_type,
                    download_url=await svc.signed_url(a.storage_key, a.size_bytes),
                    size_bytes=a.size_bytes,
                    created_at=a.created_at,
                    label=_artifact_label(a.artifact_type, _engine,
                                          str(getattr(a, "logical_key", "") or "")),
                )
            )

        # The DURABLE application-rendered terminal verdict (never model prose), reproduced from the
        # persisted final_result so a restarted API returns the same result.
        _fr_dict = getattr(job, "final_result", None)
        if not isinstance(_fr_dict, dict):
            _fr_dict = None
        # the verdict, reasoning and findings served beside it must agree with that record
        _review = served_review(_fr_dict, _vdata.get("review") or {})
        _final_message = None
        if _fr_dict:
            try:
                from meshpipeline.application.final_result import FinalResult, render_message
                _final_message = render_message(FinalResult.from_dict(_fr_dict))
            except Exception:  # noqa: BLE001 - a malformed/old-version record never breaks the read
                _final_message = None

        # The parcel's contents, from the same rows the downloads above came from. Guarded like
        # the rendered message beside it: a malformed or older record must never break a read of
        # what a customer was given.
        _manifest = None
        try:
            # ONLY ONCE THERE IS A TERMINAL RECORD. A pending job has no parcel, and an empty
            # account of one reads as "nothing was delivered" rather than "nothing has finished" -
            # so clients can rely on the manifest's presence to mean a final parcel exists.
            from meshpipeline.application.delivery_manifest import build as _build_manifest
            _manifest = None if not _fr_dict else _build_manifest(
                job_id=str(job.id),
                artifacts=([] if _is_cancelled(job) else (job.artifacts or [])),
                final_result=_fr_dict)
        except Exception:  # noqa: BLE001
            _manifest = None

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
            # the reviewer model's own words, shown on the result card: its math markup made plain
            reviewer_reasoning=engineering_text.plain(_review.get("reasoning", "")),
            reviewer_findings=_failed_concerns(_review, _engine),
            final_message=_final_message,
            final_result=_fr_dict,
            delivery_manifest=_manifest,
            review_outcome=_review_outcome(_fr_dict),
            review_concerns=_review_concerns(_fr_dict),
            review_notes=_review_notes(_fr_dict),
            # a string or nothing: a row from before the column carries none, and a test double must
            # not be able to smuggle an object into the wire contract
            cancel_reason=(job.cancel_reason if isinstance(getattr(job, "cancel_reason", None), str)
                           else None),
            worker_wake_minutes=worker_wake_estimate(job),
            geometry_filename=geometry_filename_of(job),
            session_id=(str(job.dispatch_payload["session_id"])
                        if isinstance(getattr(job, "dispatch_payload", None), dict)
                        and job.dispatch_payload.get("session_id")
                        else None),
        )


def geometry_filename_of(job) -> str | None:
    """What the user called the file this run meshes, or None when the run has no recorded source.

    Display only - the name the upload arrived with, never used to open anything. A string or
    nothing: a row from before sources were recorded has none."""
    name = getattr(getattr(job, "geometry_source", None), "original_filename", None)
    return name if isinstance(name, str) and name else None


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


@router.get("/{job_id}/mesh/partial")
async def get_mesh_partial(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                           organization_id: str = Depends(org_dep)):
    """The newest boundary surface of the mesh-so-far this job published, while it is still meshing.

    404 until there is one, and on every deployment that does not publish them
    (MESH_PROGRESSIVE_SNAPSHOT_ENABLED). The bytes are handed through as the mesher wrote them, as
    the geometry check's skin is: the worker already wrote the JSON the viewer reads, and decoding
    and re-encoding it here would only make copies.

    THE KEY IS NEVER THE CALLER'S. It is read from this job's own event stream - the pointer the
    run published - so a caller cannot name an object, and the route reaches nothing in the
    exchange that this job did not put there itself. Serving it through the API rather than
    handing the browser a signed URL is also what keeps the exchange bucket closed: no CORS rule,
    no public access, one reader.
    """
    import meshpipeline.settings.mesh_snapshot as mscfg
    if not mscfg.MESH_PROGRESSIVE_SNAPSHOT_ENABLED:
        raise HTTPException(404, "This deployment does not publish a mesh while it is building")
    async with get_db() as db:
        job = await svc.get_job(db, job_id, owner_id, organization_id=organization_id)
    if not job:
        raise HTTPException(404, "Job not found")
    key = await _published_partial_key(str(job_id))
    if not key:
        raise HTTPException(404, "No partial mesh has been published for this job")
    from fastapi.responses import Response

    from meshpipeline.contracts.mesh_partial import read_partial
    try:
        raw = await asyncio.to_thread(read_partial, key)
    except Exception as exc:  # noqa: BLE001 - the exchange is a dependency, not the caller
        logger.warning("partial mesh unreadable for job %s: %s", job_id, type(exc).__name__)
        raise HTTPException(503, "The partial mesh is temporarily unavailable") from exc
    if raw is None:
        # The run moved on and its exchange was tidied, or the frame expired. Not an error: the
        # delivered mesh is what the viewer shows from here.
        raise HTTPException(404, "No partial mesh is stored for this job")
    return Response(content=raw, media_type="application/json")


async def _published_partial_key(job_id: str) -> str:
    """The object key of the newest partial THIS job published, or "" when it published none.

    Read from the job's own durable event log, which is the record of what the run said - so the
    route never trusts a key from a request and never derives one from an operation key it would
    have to be told. One list read; the log is already held for the page's replay."""
    import json as _json

    import meshpipeline.events as E
    from meshpipeline.artifact_keys import is_partial_key
    from meshpipeline.contracts.event_stream import subscription
    try:
        raw = await subscription(job_id).backlog()
    except Exception as exc:  # noqa: BLE001 - no stream, no pointer; the route 404s
        logger.info("partial mesh: the event log for job %s is unreadable (%s)",
                    job_id, type(exc).__name__)
        return ""
    key = ""
    for line in raw or ():
        try:
            event = _json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("type") == E.MESH_PARTIAL \
                and is_partial_key(event.get("object_key") or ""):
            key = str(event.get("object_key") or "") or key
    return key


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
        logger.info("surface: which way is up unavailable (%s)", type(exc).__name__)
        return None


@router.get("/{job_id}/surface.vtk")
async def get_surface_vtk(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                          organization_id: str = Depends(org_dep)):
    """The delivered boundary with its quality fields as a legacy VTK file, for ParaView. The
    arrays are the viewer's own, so what ParaView colours is what the heatmap coloured. Built from
    the stored viewer payload - no workspace is read. A polyMesh surface and the triangle surface
    of a tetrahedral engine's DELIVERED mesh (VMTK, Gmsh: kind=stl marked is_mesh) both export;
    an input skin shown in place of a mesh never does."""
    from fastapi.responses import Response

    from meshpipeline.render.vtk_export import surface_to_vtk
    data = await _viewer_data(job_id, owner_id, organization_id=organization_id)
    surface = data.get("surface")
    if not surface or not (surface.get("kind") == "polymesh"
                           or (surface.get("kind") == "stl" and surface.get("is_mesh") is True)):
        raise HTTPException(404, "No delivered mesh surface was stored for this job")
    try:
        body = await asyncio.to_thread(surface_to_vtk, surface)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=body, media_type="application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="mesh_quality_{str(job_id)[:8]}.vtk"'})


@router.get("/{job_id}/report.html")
async def get_mesh_report(job_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                          organization_id: str = Depends(org_dep)):
    """THE MESH REPORT as its own download (render/mesh_report.py): every limit, place, hot spot,
    solver check and joint the short result screen leaves out. Built from the stored viewer payload,
    as the copy in the Everything bundle was, so the two are the same page - and a run delivered
    before the bundle carried one still gets its report. Served as an attachment, never shown in place."""
    from fastapi.responses import Response

    from meshpipeline.render.mesh_report import mesh_report_html
    data = await _viewer_data(job_id, owner_id, organization_id=organization_id)
    if not data.get("surface"):
        raise HTTPException(404, "No delivered mesh was stored for this job")
    body = await asyncio.to_thread(mesh_report_html, data)
    return Response(content=body.encode("utf-8"), media_type="text/html; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="mesh_report_{str(job_id)[:8]}.html"',
                             "X-Content-Type-Options": "nosniff"})


#: The one sanitized failure this route returns when a dispute child could not be queued. A repeat of
#: an operation whose child already failed to launch is answered with the SAME text, so the public
#: contract gains no new shape from the retry policy.
_DISPATCH_FAILED = "Failed to enqueue dispute job"


async def _finished_parent(db, job_id: uuid.UUID, owner_id: str, organization_id: str, *,
                           needs_the_mesh: bool = True):
    """The finished run a dispute or a change request is made on, or the HTTP refusal that says why not.
    `needs_the_mesh`: a second look (VER-11) looks at the delivered mesh; a change (VER-9) is a new run
    from the upload and the settings, so a mesh no longer kept never stops it."""
    from meshpipeline.application.review_seed import SEED_LOGICAL_KEY
    from meshpipeline.persistence.models import JobStatus as _JS

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
    # A WORKER'S OWN COPY going (worker_disk marks it purged) is not the mesh going: the delivery kept a
    # seed of it in the object store, and any worker restores it from there (VER-17)
    _kept = any(str(getattr(a, "logical_key", "") or "") == SEED_LOGICAL_KEY for a in (getattr(parent, "artifacts", None) or []))
    if needs_the_mesh and parent.workspace_purged and not _kept:
        from meshpipeline.application.job_service import DISPUTE_MESH_GONE
        raise HTTPException(409, DISPUTE_MESH_GONE)
    return parent


def _change_facts(flags: list[dict], comment: str, *, session_id: str, payload: dict, started, ended,
                  review: bool = False):
    """VER-10: the marks tied to the upload's own faces, and what will be said about it (application/change_request):
    (the facts, the marks). `review`: a second look (VER-11, VER-18), which starts whatever the marks tie. Reads the
    session's face map from the object store - call it off the event loop."""
    from meshpipeline.application import change_request as CR
    from meshpipeline.application import face_pick
    from meshpipeline.engines import mesh_requirements as MR
    from meshpipeline.engines.fea_review import FEA_PURPOSE

    fm, scale = face_pick.current(session_id) if session_id and flags else (None, 1.0)
    marks = CR.resolve_marks(fm, flags, scale=scale, span_factor=polcfg.VIEWER_FLAG_SPAN_FACTOR)
    faces = CR.faces_of(marks)
    # VER-17: the upload's face list no longer kept - the marks cannot be tied, which is no reason to stop a
    # change the words still ask: it goes ahead from the words, and the reply says the marks were not used
    marks_lost = bool(flags) and fm is None
    if review:
        return CR.ChangeFacts(
            kind=CR.KIND_REVIEW, words=" ".join(str(comment or "").split())[:600], faces=faces, marks=len(flags),
            untied=[m.ordinal for m in marks if not m.tied] if not marks_lost else [],
            spots=CR.separate_spots(marks), last_run_minutes=CR.minutes_of(started, ended),
            marks_lost=marks_lost), marks
    kind = CR.KIND_ASK if flags and not faces and not marks_lost else CR.KIND_CHANGE
    flow = str(payload.get("purpose") or "") != FEA_PURPOSE
    facts = CR.ChangeFacts(
        kind=kind, words=" ".join(str(comment or "").split())[:600], faces=faces, marks=len(flags),
        untied=[m.ordinal for m in marks if not m.tied] if faces else [],
        spots=CR.separate_spots(marks), one_place=CR.one_place_words(comment),
        flow_cannot_size_a_face=(bool(faces) and flow and bool(payload.get("mesh_engine"))
                                 and not MR.flow_sizes_picked_faces(str(payload.get("mesh_engine")))),
        last_run_minutes=CR.minutes_of(started, ended), marks_lost=marks_lost,
        # PLAN-fea-36 on a change request: "finer" halves what the delivered mesh had at the marked faces (the FEA
        # solid and shell routes); a flow mesh refines by its own levels, said by the result
        size_change=CR.size_step(comment) if faces and not flow else "")
    return facts, marks


async def _rerun_base(db, parent, intake: _ParentIntake, payload: dict, *, owner_id: str,
                      organization_id: str) -> dict:
    """The fields a re-run of `parent` is dispatched with when its own payload is gone: exactly what a dispute
    child is built from (the intake context, the upload at the parent's scale, the team's standard)."""
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef, GeometrySourceRef

    interpretation = None
    if parent.geometry_interpretation_id:
        from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
            GeometryInterpretationRepository,
        )
        interpretation = await GeometryInterpretationRepository().get_for_owner(
            db, parent.geometry_interpretation_id, owner_id, organization_id=organization_id)
    standard = payload.get("meshing_standard")
    return {**({"meshing_standard": standard} if standard else {}),
            "job_id": str(parent.id), "owner_id": owner_id,
            "geometry_source": (GeometrySourceRef.from_row(parent.geometry_source).to_payload()
                                if parent.geometry_source else None),
            "geometry_interpretation": (GeometryInterpretationRef.from_domain(interpretation).to_payload()
                                        if parent.geometry_source and interpretation else None),
            "session_id": intake.session_id, "request_txt": "", "review_brief_txt": intake.review_brief_txt,
            "intake_patches": list(intake.intake_patches), "dimensionality": intake.dimensionality,
            "purpose": intake.purpose, "input_kind": intake.input_kind,
            "user_notes": list(payload.get("user_notes") or [])}


async def _change_request(job_id: uuid.UUID, body: DisputeIn, *, owner_id: str, plan: str, organization_id: str,
                          operation: str) -> tuple[DisputeOut, str]:
    """VER-9: a change asked on a finished mesh starts a NEW run of the same part with the change as a requirement
    on the marked faces; the reviewer reviews that new mesh afterwards, as any mesh. Nothing tied to the part:
    nothing starts, and the reply asks for the spot again. The reply is the model's, under the plan step's guard
    (VER-12). Returned with the person's side as the conversation keeps it (CR.said_in_chat)."""
    from meshpipeline.agents.intake import change_reply
    from meshpipeline.application import change_request as CR
    from meshpipeline.application import dispute_operation
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    flags = [f.model_dump(exclude_none=True) for f in body.flags]
    comment = (body.comment or "")[:2000]
    async with get_db() as db:
        parent = await _finished_parent(db, job_id, owner_id, organization_id, needs_the_mesh=False)
        if CR.upload_gone(parent):
            # the new run is made from the upload; with the file gone there is nothing to make it from
            raise HTTPException(409, CR.UPLOAD_GONE)
        parent_payload = parent.dispatch_payload if isinstance(parent.dispatch_payload, dict) else {}
        _intake = _parent_intake(await SessionRepository().get_by_job_id(db, job_id), parent_payload)
        if not (parent_payload.get("job_id") and parent_payload.get("owner_id")):
            # a run whose own payload is gone (retention clears it) or was never whole: the same fields a dispute
            # child is built from - the session or the payload's intake, the part at its scale, the standard
            parent_payload = await _rerun_base(db, parent, _intake, parent_payload, owner_id=owner_id,
                                               organization_id=organization_id)
        facts, _marks = await asyncio.to_thread(_change_facts, flags, comment, session_id=_intake.session_id,
                                                payload=parent_payload, started=getattr(parent, "started_at", None),
                                                ended=getattr(parent, "ended_at", None))

        def _out(new_id, text: str) -> DisputeOut:
            return DisputeOut(job_id=new_id, dispute_of=job_id, flags=len(body.flags), kind=facts.kind, reply=text,
                              faces=[DisputeFace(id=str(f["id"]), label=str(f.get("label") or ""))
                                     for f in facts.faces],
                              marks_not_on_the_part=facts.untied, session_id=_intake.session_id or None,
                              last_run_minutes=facts.last_run_minutes)

        said_by_them = CR.said_in_chat(comment, marks=len(flags), where=facts.where())
        if facts.kind == CR.KIND_ASK:
            new_job = None
        else:
            _replay = await dispute_operation.claim(db, owner_id=owner_id, key=operation)
            if _replay is not None:
                if _replay.launch_failed:
                    raise HTTPException(500, _DISPATCH_FAILED)
                return _out(_replay.job_id, CR.plain_reply(facts)), said_by_them
            try:
                _billed_plan = await spend_gate.admit(db, owner_id=owner_id, organization_id=organization_id)
                await svc.check_quotas(db, owner_id, plan=plan or _billed_plan)
            except spend_gate.OutOfCredits as exc:
                raise HTTPException(402, str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(429, str(exc)) from exc
            new_job = await JobRepository().create(db, owner_id=owner_id, organization_id=organization_id)
            new_job.dispute_operation_key = operation
            # the same part at the same scale as the run the change was asked on (as a dispute child: never re-read)
            new_job.geometry_source_id = parent.geometry_source_id
            new_job.geometry_interpretation_id = parent.geometry_interpretation_id
            note = CR.change_note(comment, facts.faces, of_job_id=str(job_id))
            payload = CR.change_payload(parent_payload, job_id=str(new_job.id), note=note,
                                        brief=CR.brief_line(comment, facts.faces, of_job_id=str(job_id)))
            await db.commit()

    if new_job is not None:
        try:
            await dispute_operation.dispatch_or_record_failure(new_job.id, payload, parent_job_id=job_id,
                                                               key=operation)
        except Exception as exc:
            raise HTTPException(500, _DISPATCH_FAILED) from exc
    said = await change_reply.written(facts.as_dict(), session_id=_intake.session_id, user_id=owner_id)
    return _out(new_job.id if new_job is not None else None, said["text"]), said_by_them


async def _keep_in_chat(out: DisputeOut, said_by_them: str, *, owner_id: str) -> None:
    """The request and its reply kept in the session's conversation, so a reload draws both (GET /chat/history);
    the intake authority writes them (message.keep_change_request). Nothing without a reply. The run has started
    whatever happens here, so a failure to keep the turn is logged, never answered as a failed request."""
    from meshpipeline.agents.intake import message as msg
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    if not (out.reply or "").strip() or not out.session_id:
        return
    try:
        session_id = uuid.UUID(str(out.session_id))
    except ValueError:
        return
    try:
        await msg.keep_change_request(session_id, owner_id, said=said_by_them, reply=out.reply,
                                      job_id=str(out.job_id) if out.job_id else "",
                                      session_repo=SessionRepository(), db_factory=get_db)
    except Exception:
        logger.exception("change request: could not keep the turn in session %s's chat", session_id)


@router.post("/{job_id}/dispute", response_model=DisputeOut, status_code=202)
async def dispute_job(job_id: uuid.UUID, body: DisputeIn, owner_id: str = Depends(owner_dep),
                      plan: str = Depends(plan_dep),
                      organization_id: str = Depends(org_dep)):
    from meshpipeline.application import change_request, dispute_operation

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

    if _mode == "rebuild" and change_request.is_change(body.comment):
        # VER-9: words that ask for a change are a requirement of a NEW run - never a review of the old mesh
        out, said_by_them = await _change_request(job_id, body, owner_id=owner_id, plan=plan,
                                                  organization_id=organization_id, operation=_operation)
        await _keep_in_chat(out, said_by_them, owner_id=owner_id)
        return out

    out, said_by_them = await _second_look(job_id, body, user_dispute, mode=_mode, owner_id=owner_id, plan=plan,
                                           organization_id=organization_id, operation=_operation)
    await _keep_in_chat(out, said_by_them, owner_id=owner_id)
    return out


async def _second_look(job_id: uuid.UUID, body: DisputeIn, user_dispute: dict, *, mode: str, owner_id: str, plan: str,
                       organization_id: str, operation: str) -> tuple[DisputeOut, str]:
    """VER-11 / VER-18: a second look at the delivered mesh at the marks, then the rebuild. The new run is the finished
    run's own payload (every setting, typed note and picked face it was asked) with the dispute added and each mark's
    faces in it, so the rebuild keeps all of it and makes the mesh finer at the marks when the second look finds
    nothing past a limit there. The reply is the model's under the change reply's guard, the code sentence its
    fallback (VER-12), never empty."""
    from meshpipeline.agents.intake import change_reply
    from meshpipeline.application import change_request as CR
    from meshpipeline.application import dispute_operation
    from meshpipeline.persistence.repositories.job_repository import JobRepository
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    flags = [f.model_dump(exclude_none=True) for f in body.flags]
    job_repo = JobRepository()
    async with get_db() as db:
        parent = await _finished_parent(db, job_id, owner_id, organization_id)
        parent_payload = parent.dispatch_payload if isinstance(parent.dispatch_payload, dict) else {}
        # Parent intake context: patches/dimensionality/brief from the session while it is still linked to this
        # run, else from the run's own dispatch payload (see _parent_intake)
        _intake = _parent_intake(await SessionRepository().get_by_job_id(db, job_id), parent_payload)
        if not (parent_payload.get("job_id") and parent_payload.get("owner_id")):
            parent_payload = await _rerun_base(db, parent, _intake, parent_payload, owner_id=owner_id,
                                               organization_id=organization_id)
        facts, marks = await asyncio.to_thread(_change_facts, flags, body.comment or "", session_id=_intake.session_id,
                                               payload=parent_payload, started=getattr(parent, "started_at", None),
                                               ended=getattr(parent, "ended_at", None), review=True)
        said_by_them = CR.said_in_chat(body.comment, marks=len(flags), where=facts.where())

        def _out(new_id, text: str) -> DisputeOut:
            return DisputeOut(job_id=new_id, dispute_of=job_id, flags=len(body.flags), kind=facts.kind, reply=text,
                              faces=[DisputeFace(id=str(f["id"]), label=str(f.get("label") or ""))
                                     for f in facts.faces],
                              marks_not_on_the_part=facts.untied, session_id=_intake.session_id or None,
                              last_run_minutes=facts.last_run_minutes)

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
        _replay = await dispute_operation.claim(db, owner_id=owner_id, key=operation)
        if _replay is not None:
            if _replay.launch_failed:
                raise HTTPException(500, _DISPATCH_FAILED)
            return _out(_replay.job_id, CR.plain_reply(facts)), said_by_them

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

        new_job = await job_repo.create(db, owner_id=owner_id, organization_id=organization_id)
        # The child IS the operation's durable record. Written in the same transaction that creates
        # it, under the claim's lock, so the row and its identity become visible together.
        new_job.dispute_operation_key = operation
        new_job.geometry_source_id = parent.geometry_source_id
        # A dispute re-runs the parent's geometry, so it inherits the parent's SCALE unchanged -
        # never a fresh reading. Re-resolving would let a correction recorded after the disputed
        # run silently change the size of the very result the user is disputing.
        new_job.geometry_interpretation_id = parent.geometry_interpretation_id
        await db.commit()

    try:
        # VER-18: THE FINISHED RUN'S OWN PAYLOAD - every saved setting as applied, typed note, picked face and
        # count it was asked - with the dispute and each mark's faces (the rebuild's "finer there", marked_spots)
        _dispute = {**user_dispute, "marks": CR.marks_of(marks)} if mode == "rebuild" else dict(user_dispute)
        _payload = CR.review_payload(parent_payload, job_id=str(new_job.id), user_dispute=_dispute,
                                     brief=_amended_brief(_intake, mode, body.comment))
        # The child is already committed, deliberately, so a worker that takes the message
        # immediately can see the job it names. The authority below owns the consequence: it
        # dispatches inside its own session scope - `db` above closed at the commit, and a
        # SQLAlchemy session stays usable after close(), so dispatching through it would check out
        # a SECOND connection nothing was responsible for (measured against real PostgreSQL: three
        # such failures grew the checked-out count 1 -> 2 -> 3, unreclaimed by gc.collect()) - and
        # it records a durable launch failure if the broker never took the message, so the child
        # cannot sit pending forever against the owner's quota.
        await dispute_operation.dispatch_or_record_failure(
            new_job.id, _payload, parent_job_id=job_id, key=operation)
    except Exception as exc:
        raise HTTPException(500, _DISPATCH_FAILED) from exc

    if mode == "accept":
        # the same mesh judged again against the bar they stated: nothing is rebuilt, so nothing of a rebuild is said
        return _out(new_job.id, CR.ACCEPT_REPLY), said_by_them
    said = await change_reply.written(facts.as_dict(), session_id=_intake.session_id, user_id=owner_id)
    return _out(new_job.id, said["text"]), said_by_them


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
