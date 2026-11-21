# Responsibility: Make a run's CAD repair inspection report durable, whatever the run's outcome.
# Owns: the report's object bytes and the one artifact row that names them.
# Boundaries: it delivers EVIDENCE. It never decides a status, never fails a run, and its absence
#             can never make a job look delivered (see application/artifact_policy.py).
# Collaborates with: pipeline/repair_inspect.py (which measures), contracts/object_storage.py and
#                    the artifact repository (which own the bytes and the row).
from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: The per-job logical identity of the repaired geometry handed back to the customer.
REPAIRED_CAD_LOGICAL_KEY = "repaired_cad"

#: The per-job logical identity of the report. One per job: inspection reads the immutable
#: uploaded source, so every attempt of a run inspects the same bytes and reaches the same report.
REPAIR_REPORT_LOGICAL_KEY = "repair_report"


async def deliver_repair_report(session_factory, *, job_id: str, report: dict,
                                repair_status: str = "",
                                repair_attempt: dict | None = None,
                                repair_handback: dict | None = None,
                                owner_id: str = "",
                                delivery_attempt: int = 0,
                                execution_generation: int = 0,
                                fence_commit=None) -> str:
    """Store the inspection report and bind it to the job. Returns the outcome's name, or "".

    WHY THIS IS NOT PART OF ARTIFACT DELIVERY. `artifact_uploader.deliver_succeeded_run` runs only
    on a genuine success and only from an engine workspace, and it fails closed when no mesh
    deliverable is present - correctly, because that is what delivery means. But the report's most
    important readers are on the runs that produced NO mesh: the operator deciding whether the
    file is worth repairing, and the customer being told why it was refused. So the report is
    delivered on its own, on every terminal outcome, from state rather than from a workspace.

    NOTHING HERE MAY END A RUN. Every failure is logged and swallowed: a store that would not take
    an evidence file is not a reason to withhold a mesh the user can otherwise download, nor to
    accuse their geometry of anything. The caller gets "" and carries on.

    The fence is the caller's to supply, for the same reason artifact registration takes one: a
    worker whose lease was taken over must leave no rows on a job it no longer speaks for.
    """
    if not report:
        return ""

    from meshpipeline.artifact_keys import repair_report_key
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced
    from meshpipeline.persistence.models import ArtifactType
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    payload: dict = {"repair_status": str(repair_status or ""), "report": report}
    # WHAT THE REPAIR DID, beside the inspection of what arrived: its operations, refusals, why it
    # stopped and what it left. A customer handed repaired CAD gets the explanation with it, and
    # the inspection above stays the upload's own, unchanged.
    if repair_attempt:
        payload["repair_attempt"] = dict(repair_attempt)
    # THE MESHER HANDBACK'S OWN REPAIR, when there was one: its record (outcome, faces, headline
    # summary and - with CAD_REPAIR_INVESTIGATION_TRACE - its investigation trace's location).
    if repair_handback:
        payload["repair_handback"] = dict(repair_handback)
    body = json.dumps(payload, sort_keys=True, default=str)
    # CONTENT-ADDRESSED. Two writers can race for one job - a retry, or a worker whose lease was
    # taken over - and a shared key let the loser overwrite bytes a COMMITTED row already pointed
    # at. A customer could then download a report that did not match the record.
    digest = hashlib.sha256(body.encode()).hexdigest()
    object_key = repair_report_key(str(job_id), digest)
    scratch: Path | None = None
    stored = None
    try:
        from meshpipeline.contracts.object_storage import get_object_store
        _fd, _name = tempfile.mkstemp(prefix="repair_report_", suffix=".json")
        import os
        os.close(_fd)
        scratch = Path(_name)
        # sort_keys so the same report yields the same bytes, and therefore the same checksum, on
        # a re-delivery of the same attempt - which the repository's CAS reads as idempotent
        # rather than as a conflicting second version of the same evidence.
        scratch.write_text(body)
        stored = get_object_store().upload_file(
            local_path=scratch, object_key=object_key, content_type="application/json")

        async with session_factory() as db:
            outcome = await ArtifactRepository().deliver_artifact(
                db, job_id=uuid.UUID(str(job_id)),
                logical_key=REPAIR_REPORT_LOGICAL_KEY,
                artifact_type=ArtifactType.repair_report,
                storage_key=object_key,
                size_bytes=int(stored.size_bytes or 0),
                checksum=stored.checksum,
                delivery_attempt=delivery_attempt,
                execution_generation=execution_generation)
            if fence_commit is not None:
                await fence_commit(db)
            await db.commit()
        logger.info("repair_report_delivery: %s status=%s key=%s - job_id=%s",
                    outcome.value, repair_status, object_key, job_id)
        return outcome.value
    except StaleWorkerFenced:
        # This worker no longer owns the job. The row rolled back with the session and the object
        # it stored is nobody's, which the artifact reconciler already handles for unowned keys.
        # Re-raised because the CALLER's response to being fenced is to produce nothing at all.
        logger.warning("repair_report_delivery: fenced before the row committed - job_id=%s", job_id)
        raise
    except Exception as exc:  # noqa: BLE001 - evidence is never load-bearing for a run
        logger.warning("repair_report_delivery: could not deliver the inspection report (%s) - "
                       "the run is unaffected - job_id=%s", exc, job_id)
        await _discard_orphan(session_factory, object_key, job_id=job_id, owner_id=owner_id,
                              logical_key=REPAIR_REPORT_LOGICAL_KEY,
                              artifact_type=ArtifactType.repair_report,
                              delivery_attempt=delivery_attempt, stored=stored)
        return ""
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


async def _is_claimed(session_factory, object_key: str, *, job_id: str) -> bool:
    """Does a COMMITTED artifact row already point at these bytes?

    The keys here are content-addressed, so an identical re-delivery - a retry, or a worker that
    took the lease over - computes the key an earlier, successful delivery already committed. If
    that retry's own row then fails, the bytes under the key are not an orphan: they are the
    object a customer's artifact record names. Deleting them would turn a committed download into
    a 404.

    FAILING TO ANSWER COUNTS AS CLAIMED. If the database cannot be reached the honest answer is
    "unknown", and the safe reading of unknown is to keep the object: a leaked file costs storage,
    a wrongly deleted one costs the customer their deliverable.
    """
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    try:
        async with session_factory() as db:
            rows = await ArtifactRepository().get_by_job(db, uuid.UUID(str(job_id)))
        return any(getattr(row, "storage_key", None) == object_key for row in rows)
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair_report_delivery: could not check whether a row owns %s (%s) - "
                       "keeping the object - job_id=%s", object_key, exc, job_id)
        return True


async def _discard_orphan(session_factory, object_key: str, *, job_id: str, owner_id: str,
                         logical_key: str, artifact_type, delivery_attempt: int,
                         stored=None) -> None:
    """Hand an object whose row never committed to the artifact reconciler. NEVER DELETED HERE.

    WHY NOT DELETE ON THE SPOT. The keys are content-addressed, so two deliveries of identical
    bytes for one job - a retry, or a worker whose lease was taken over - share one key. If this
    one failed while the other was still committing, "no committed row names it" was true at the
    instant of the check and false a moment later, and the delete took the bytes the other
    delivery's committed row points at. The reconciler (application/maintenance/reconcile.py)
    decides later, when an in-flight writer has either committed (its ready row ADOPTS the object)
    or not, and re-verifies the object's checksum before it deletes anything.

    BEST EFFORT, and said so: a record that cannot be written leaves the object where it is - a
    leaked file costs storage, a wrongly deleted one costs the customer their deliverable.
    """
    if await _is_claimed(session_factory, object_key, job_id=job_id):
        logger.info("repair_report_delivery: %s is named by a committed artifact row - leaving "
                    "it where the customer can download it - job_id=%s", object_key, job_id)
        return
    if not owner_id:
        logger.warning("repair_report_delivery: no owner to record the orphaned object %s "
                       "against - keeping it - job_id=%s", object_key, job_id)
        return
    try:
        from meshpipeline.persistence.repositories.reconciliation_repository import (
            ReconciliationRepository,
        )
        async with session_factory() as db:
            await ReconciliationRepository().record_orphan(
                db, owner_id=owner_id, job_id=uuid.UUID(str(job_id)),
                delivery_attempt=int(delivery_attempt or 0), logical_key=logical_key,
                artifact_type=artifact_type, object_key=object_key,
                # what THIS delivery stored, so the reconciler refuses to delete bytes a later
                # write replaced (its generation check)
                object_checksum=getattr(stored, "checksum", None),
                object_size=int(getattr(stored, "size_bytes", 0) or 0),
                detail="repair evidence row never committed")
            await db.commit()
        logger.info("repair_report_delivery: handed the orphaned object %s to the reconciler "
                    "- job_id=%s", object_key, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("repair_report_delivery: could not record the orphaned object %s (%s) "
                       "- keeping it - job_id=%s", object_key, exc, job_id)


async def deliver_repaired_cad(session_factory, *, job_id: str, local_path,
                               suffix: str = ".step",
                               owner_id: str = "",
                               delivery_attempt: int = 0,
                               execution_generation: int = 0,
                               fence_commit=None) -> str:
    """Hand the repaired geometry back. Returns the outcome's name, or "".

    WHY THIS IS A DELIVERABLE. A customer whose CAD we fixed paid for the fixed CAD as much as for
    the mesh built from it; returning only the mesh keeps the more reusable half of the work. So
    the repaired bytes are uploaded beside it, downloadable, with their own digest.

    Same posture as the report above: evidence, delivered best-effort, and no failure here ends a
    run - except being fenced, which stops this worker producing anything at all.
    """
    from meshpipeline.artifact_keys import repaired_cad_key
    from meshpipeline.persistence.models import ArtifactType

    safe = (suffix or _suffix_of(local_path) or ".step").lower()
    return await _deliver_file(
        session_factory, job_id=job_id, local_path=local_path,
        key_for=lambda digest: repaired_cad_key(str(job_id), digest, safe),
        logical_key=REPAIRED_CAD_LOGICAL_KEY, artifact_type=ArtifactType.repaired_cad,
        content_type="application/octet-stream", what="repaired CAD", owner_id=owner_id,
        delivery_attempt=delivery_attempt, execution_generation=execution_generation,
        fence_commit=fence_commit)


#: The per-job logical identity of the customer's STEP handed back UNCHANGED (a passthrough).
RETURNED_CAD_LOGICAL_KEY = "returned_cad"
#: The per-job logical identity of the mesh-size hints sidecar beside the returned STEP.
MESH_HINTS_LOGICAL_KEY = "mesh_hints"


async def deliver_returned(session_factory, *, job_id: str, returned: dict,
                           step_delivered: bool = False,
                           owner_id: str = "",
                           delivery_attempt: int = 0,
                           execution_generation: int = 0,
                           fence_commit=None) -> dict:
    """Hand back what a repair attempt RETURNS (pipeline/repair_return.py) beyond what is already
    delivered: the passthrough STEP, and the mesh-hints sidecar for a repaired OR a passthrough
    STEP. Returns `{"step": outcome-or-"", "hints": outcome-or-""}`.

    LABELLED FOR WHAT IT IS. A passthrough is the customer's own bytes: it is delivered as
    `returned_cad`, never `repaired_cad`, so neither the download menu nor the manifest can call it
    repaired. A repaired STEP is delivered by `deliver_repaired_cad` (a promoted repair, a
    standalone job) - `step_delivered` says so, and only its sidecar is added here.

    The same posture as every delivery in this module: evidence, best-effort, never ends a run;
    only being fenced (StaleWorkerFenced) propagates.
    """
    from meshpipeline.artifact_keys import mesh_hints_key, returned_cad_key
    from meshpipeline.persistence.models import ArtifactType

    record = dict(returned or {})
    out = {"step": "", "hints": ""}
    common = {"owner_id": owner_id, "delivery_attempt": delivery_attempt,
              "execution_generation": execution_generation, "fence_commit": fence_commit}
    step = str(record.get("path") or "")
    if step and not step_delivered and record.get("source") == "passthrough":
        safe = (_suffix_of(step) or ".step").lower()
        out["step"] = await _deliver_file(
            session_factory, job_id=job_id, local_path=step,
            key_for=lambda digest: returned_cad_key(str(job_id), digest, safe),
            logical_key=RETURNED_CAD_LOGICAL_KEY, artifact_type=ArtifactType.returned_cad,
            content_type="application/octet-stream", what="returned (unchanged) CAD", **common)
    hints = str(record.get("hints_path") or "")
    # THE HINTS ARE AN ADDITION TO A STEP THE CUSTOMER HOLDS: never delivered without one
    if hints and (step_delivered or out["step"]):
        out["hints"] = await _deliver_file(
            session_factory, job_id=job_id, local_path=hints,
            key_for=lambda digest: mesh_hints_key(str(job_id), digest),
            logical_key=MESH_HINTS_LOGICAL_KEY, artifact_type=ArtifactType.mesh_hints,
            content_type="application/json", what="mesh hints", **common)
    return out


def _suffix_of(path) -> str:
    try:
        return Path(str(path or "")).suffix
    except Exception:  # noqa: BLE001 - no suffix is the default's to fill
        return ""


async def _deliver_file(session_factory, *, job_id: str, local_path, key_for, logical_key: str,
                        artifact_type, content_type: str, what: str,
                        owner_id: str = "", delivery_attempt: int = 0,
                        execution_generation: int = 0, fence_commit=None) -> str:
    """Upload one local file under a content-addressed key and bind it to the job as one artifact
    row. Returns the outcome's name, or "" when there was nothing to deliver or it failed.
    Only StaleWorkerFenced propagates."""
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    src = Path(str(local_path or ""))
    if not src.is_file():
        # Nothing to hand back. Not an error: most runs mesh the upload as it arrived.
        return ""

    from meshpipeline.contracts.geometry_source import sha256_of

    object_key = ""
    stored = None
    try:
        # INSIDE THE GUARD. Hashing reads the file again, and a file that vanished or became
        # unreadable between the check above and here must not raise out of a best-effort
        # delivery: the caller is finalising a terminal run and only expects to be fenced, so an
        # exception here would abandon a mesh the customer can otherwise download.
        digest, _ = sha256_of(src)
        object_key = key_for(digest)
        from meshpipeline.contracts.object_storage import get_object_store
        stored = get_object_store().upload_file(
            local_path=src, object_key=object_key, content_type=content_type)

        async with session_factory() as db:
            outcome = await ArtifactRepository().deliver_artifact(
                db, job_id=uuid.UUID(str(job_id)),
                logical_key=logical_key,
                artifact_type=artifact_type,
                storage_key=object_key,
                size_bytes=int(stored.size_bytes or 0),
                checksum=stored.checksum,
                delivery_attempt=delivery_attempt,
                execution_generation=execution_generation)
            if fence_commit is not None:
                await fence_commit(db)
            await db.commit()
        logger.info("repair_report_delivery: %s %s key=%s - job_id=%s",
                    what, outcome.value, object_key, job_id)
        return outcome.value
    except StaleWorkerFenced:
        logger.warning("repair_report_delivery: fenced before the %s row committed - job_id=%s",
                       what, job_id)
        raise
    except Exception as exc:  # noqa: BLE001 - evidence is never load-bearing for a run
        # NOT LOAD-BEARING FOR THE MESH, BUT NOT SILENT EITHER. A run that cannot hand back the
        # file it promised has delivered half of the work, so the caller is told - the delivery
        # manifest's `evidence_gaps` is what turns a missing repaired file into a visible gap.
        logger.warning("repair_report_delivery: could not deliver the %s (%s) - the mesh is "
                       "unaffected but the customer cannot download it - job_id=%s",
                       what, exc, job_id)
        if object_key:
            await _discard_orphan(session_factory, object_key, job_id=job_id, owner_id=owner_id,
                                  logical_key=logical_key, artifact_type=artifact_type,
                                  delivery_attempt=delivery_attempt, stored=stored)
        return ""
