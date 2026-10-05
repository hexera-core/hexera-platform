# Responsibility: Make a run's CAD repair inspection report durable, whatever the run's outcome.
# Owns: the report's object bytes and the one artifact row that names them.
# Boundaries: it delivers EVIDENCE. It never decides a status, never fails a run, and its absence
#             can never make a job look delivered (see application/artifact_policy.py).
# Collaborates with: pipeline/repair_inspect.py (which measures), contracts/object_storage.py and
#                    the artifact repository (which own the bytes and the row).
from __future__ import annotations

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

    payload = {"repair_status": str(repair_status or ""), "report": report}
    object_key = repair_report_key(str(job_id))
    scratch: Path | None = None
    try:
        from meshpipeline.contracts.object_storage import get_object_store
        _fd, _name = tempfile.mkstemp(prefix="repair_report_", suffix=".json")
        import os
        os.close(_fd)
        scratch = Path(_name)
        # sort_keys so the same report yields the same bytes, and therefore the same checksum, on
        # a re-delivery of the same attempt - which the repository's CAS reads as idempotent
        # rather than as a conflicting second version of the same evidence.
        scratch.write_text(json.dumps(payload, sort_keys=True, default=str))
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
        return ""
    finally:
        if scratch is not None:
            scratch.unlink(missing_ok=True)


async def deliver_repaired_cad(session_factory, *, job_id: str, local_path,
                               suffix: str = ".step",
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
    from pathlib import Path as _Path

    from meshpipeline.contracts.execution_guard import StaleWorkerFenced
    from meshpipeline.persistence.models import ArtifactType
    from meshpipeline.persistence.repositories.artifact_repository import ArtifactRepository

    src = _Path(str(local_path or ""))
    if not src.is_file():
        # Nothing to hand back. Not an error: most runs mesh the upload as it arrived.
        return ""

    safe = (suffix or src.suffix or ".step").lower()
    object_key = f"jobs/{job_id}/repaired{safe}"
    try:
        from meshpipeline.contracts.object_storage import get_object_store
        stored = get_object_store().upload_file(
            local_path=src, object_key=object_key, content_type="application/octet-stream")

        async with session_factory() as db:
            outcome = await ArtifactRepository().deliver_artifact(
                db, job_id=uuid.UUID(str(job_id)),
                logical_key=REPAIRED_CAD_LOGICAL_KEY,
                artifact_type=ArtifactType.repaired_cad,
                storage_key=object_key,
                size_bytes=int(stored.size_bytes or 0),
                checksum=stored.checksum,
                delivery_attempt=delivery_attempt,
                execution_generation=execution_generation)
            if fence_commit is not None:
                await fence_commit(db)
            await db.commit()
        logger.info("repair_report_delivery: repaired CAD %s key=%s - job_id=%s",
                    outcome.value, object_key, job_id)
        return outcome.value
    except StaleWorkerFenced:
        logger.warning("repair_report_delivery: fenced before the repaired CAD row committed "
                       "- job_id=%s", job_id)
        raise
    except Exception as exc:  # noqa: BLE001 - evidence is never load-bearing for a run
        logger.warning("repair_report_delivery: could not deliver the repaired CAD (%s) - the "
                       "run is unaffected - job_id=%s", exc, job_id)
        return ""
