# Responsibility: Put the right geometry bytes, at the right scale, in the workspace a run will execute in.
# Owns: source and interpretation resolution, materialisation, the workspace, and the preparation refusal.
# Boundaries: it materialises what was approved.
# Collaborates with: contracts/geometry_source.py and the object store.
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import NamedTuple

from meshpipeline.contracts.geometry_source import (
    GeometryInterpretationRef,
    GeometrySourceError,
    GeometrySourceRef,
    MaterializedGeometry,
    safe_suffix,
    sha256_of,
)
from meshpipeline.contracts.object_storage import ObjectNotFound, StorageError, get_object_store
from meshpipeline.errors import FailureClass

logger = logging.getLogger(__name__)

# The generated basename every attempt materialises to. The user's filename never becomes a path:
# it arrived from a client and is display-only. Only the sanitised suffix carries over, because
# parser dispatch still keys off it.
_BASENAME = "source"


async def resolve_row_ref(db, ref: GeometrySourceRef) -> GeometrySourceRef:
    import uuid as _uuid

    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    try:
        source_uuid = _uuid.UUID(ref.source_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise GeometrySourceError("the geometry source id is malformed",
                                  failure_class=FailureClass.INTERNAL) from exc
    row = await GeometrySourceRepository().get_for_owner(db, source_uuid, ref.owner_id)
    if row is None:
        raise GeometrySourceError("the uploaded geometry is not available for this owner",
                                  failure_class=FailureClass.NOT_AUTHORIZED)
    # RETENTION EXPIRY, checked before a single byte is fetched. This is not corruption, not a
    # storage outage and not our bug: the upload simply aged out of its retention window and the
    # bytes were deleted on purpose. Reporting it as DATA_INTEGRITY would tell the user their
    # file was damaged and send an operator hunting a fault that does not exist. The row is still
    # here - checksum, size, filename and timestamps - so the history that referenced it stays
    # readable; only a NEW run needs the file back.
    if getattr(row, "purged_at", None) is not None:
        raise GeometrySourceError(
            "this geometry was removed after its retention period, so it is no longer available "
            "to mesh; upload the file again to start a new run",
            failure_class=FailureClass.USER_INPUT)
    return GeometrySourceRef.from_row(row)


def materialize(ref: GeometrySourceRef, interpretation: GeometryInterpretationRef, *,
                workspace, row_ref: GeometrySourceRef | None = None,
                job_id: str = "") -> MaterializedGeometry:
    if row_ref is not None:
        drift = ref.disagreements_with(row_ref)
        if drift:
            raise GeometrySourceError(
                "the approved geometry no longer matches the stored source record "
                f"(differs on: {', '.join(drift)})",
                failure_class=FailureClass.DATA_INTEGRITY)

    ws = Path(workspace)
    ws.mkdir(parents=True, exist_ok=True)
    dest = ws / f"{_BASENAME}{safe_suffix(ref.suffix_hint)}"

    store = get_object_store()
    try:
        if not store.exists(object_key=ref.object_key):
            raise GeometrySourceError("the uploaded geometry is no longer in storage",
                                      failure_class=FailureClass.DATA_INTEGRITY)
        store.download_file(object_key=ref.object_key, destination=dest)
    except ObjectNotFound as exc:
        raise GeometrySourceError("the uploaded geometry is no longer in storage",
                                  failure_class=FailureClass.DATA_INTEGRITY) from exc
    except StorageError as exc:
        # A provider message can name buckets, endpoints and credential-adjacent detail. Log it;
        # hand the caller a sentence that says what happened and nothing about where.
        logger.warning("geometry materialize: retrieval failed - job_id=%s: %s", job_id, exc)
        raise GeometrySourceError("the uploaded geometry could not be retrieved",
                                  failure_class=FailureClass.DEPENDENCY_DOWN) from exc

    digest, size = sha256_of(dest)
    if size != int(ref.size_bytes) or digest != ref.sha256:
        dest.unlink(missing_ok=True)
        logger.error("geometry materialize: INTEGRITY FAILURE - job_id=%s expected %d bytes/%s, "
                     "got %d bytes/%s", job_id, ref.size_bytes, ref.sha256[:12], size, digest[:12])
        raise GeometrySourceError(
            "the retrieved geometry does not match the approved upload",
            failure_class=FailureClass.DATA_INTEGRITY)
    logger.info("geometry materialize: verified %d bytes - job_id=%s", size, job_id)
    return MaterializedGeometry(ref=ref, interpretation=interpretation, local_path=dest)


async def resolve_row_interpretation(db, snapshot: GeometryInterpretationRef,
                                     owner_id: str) -> GeometryInterpretationRef:
    import uuid as _uuid

    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    try:
        interpretation_id = _uuid.UUID(snapshot.interpretation_id)
    except (TypeError, ValueError) as exc:
        raise GeometrySourceError("the approved geometry interpretation id is malformed",
                                  failure_class=FailureClass.INTERNAL) from exc
    row = await GeometryInterpretationRepository().get_for_owner(db, interpretation_id, owner_id)
    if row is None:
        # Absent and foreign are the same answer here, because the lookup is tenant-scoped.
        raise GeometrySourceError(
            "the approved geometry interpretation is no longer available",
            failure_class=FailureClass.DATA_INTEGRITY)
    row_ref = GeometryInterpretationRef.from_domain(row)
    drift = snapshot.disagreements_with(row_ref)
    if drift:
        raise GeometrySourceError(
            "the approved geometry interpretation no longer matches the stored record "
            f"(differs on: {', '.join(drift)})",
            failure_class=FailureClass.DATA_INTEGRITY)
    return row_ref


async def materialize_for_job(db, ref: GeometrySourceRef,
                              interpretation: GeometryInterpretationRef, *, workspace,
                              job_id: str = "") -> MaterializedGeometry:
    row_ref = await resolve_row_ref(db, ref)
    verified = await resolve_row_interpretation(db, interpretation, ref.owner_id)
    # BOTH halves are individually valid and owned by this tenant at this point - and that is not
    # enough. An interpretation records the scale of ONE source; pairing it with different bytes
    # meshes this file at another file's size, and every check above still passes because nothing
    # asks whether the two describe the same thing. A tenant with two uploads is one payload edit
    # away from it, so the pairing is verified rather than assumed.
    if verified.geometry_source_id != ref.source_id:
        raise GeometrySourceError(
            "the approved geometry interpretation belongs to a different geometry source, so it "
            "does not describe the size of these bytes",
            failure_class=FailureClass.DATA_INTEGRITY)
    return materialize(ref, verified, workspace=workspace, row_ref=row_ref, job_id=job_id)


async def source_ref_for_session(db, session, owner_id: str) -> GeometrySourceRef | None:
    from meshpipeline.persistence.repositories.geometry_source_repository import (
        GeometrySourceRepository,
    )
    source_id = getattr(session, "geometry_source_id", None)
    if not source_id:
        return None
    row = await GeometrySourceRepository().get_for_owner(db, source_id, owner_id)
    if row is None:
        raise GeometrySourceError("the uploaded geometry is not available for this owner",
                                  failure_class=FailureClass.NOT_AUTHORIZED)
    return GeometrySourceRef.from_row(row)


async def interpretation_ref_for_session(db, session, owner_id: str):
    from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
    from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
        GeometryInterpretationRepository,
    )
    interpretation_id = getattr(session, "geometry_interpretation_id", None)
    if not interpretation_id:
        return None
    domain = await GeometryInterpretationRepository().get_for_owner(db, interpretation_id, owner_id)
    if domain is None:
        raise GeometrySourceError("the geometry's confirmed unit is not available for this owner",
                                  failure_class=FailureClass.NOT_AUTHORIZED)
    return GeometryInterpretationRef.from_domain(domain)


def execution_workspace(job_id: str):
    import meshpipeline.settings.runtime as rtcfg
    return Path(rtcfg.WORKSPACE_BASE) / str(job_id) / "geometry"


async def prepare_execution_geometry(ref: GeometrySourceRef | None,
                                     interpretation: GeometryInterpretationRef | None = None,
                                     *, job_id: str):
    if ref is None:
        return None
    if interpretation is None:
        # Bytes whose physical scale is unknown cannot be meshed: the run would have to assume
        # one, which is exactly the silent failure this contract removes.
        raise GeometrySourceError(
            "this geometry has no confirmed unit, so its physical size is unknown",
            failure_class=FailureClass.USER_INPUT)
    from meshpipeline.persistence.session import get_db
    async with get_db() as db:
        return await materialize_for_job(db, ref, interpretation,
                                         workspace=execution_workspace(job_id), job_id=job_id)


# #
# EXECUTION PREPARATION - resolve the checkpoint disposition and materialize the geometry.
# Extracted from application/pipeline_run._run_async. Two failures with one shape lived inline as
# ~49 lines: a checkpoint that cannot be read, and geometry that cannot be materialized. Both are
# terminal BEFORE execution, and both surface as a classified GeometrySourceError.
# WHOSE FAULT IT IS comes from the raise site, which is the only place that knows: a tenant miss is
# a refusal, a snapshot/row disagreement or a bad checksum is our data integrity, an unreachable
# store is infrastructure, a malformed snapshot is our own reconstruction defect. Defaulting to
# INTERNAL keeps an unclassified raise from being reported as the user's mistake.
# #


class ExecutionPreparation(NamedTuple):

    materialized: MaterializedGeometry | None
    refusal: dict | None
    #: The checkpoint disposition this preparation read. The graph-input step needs it to decide
    #: between a fresh state, a resume from the durable position, and a completed-thread replay.
    disposition: str = ""

    @property
    def prepared(self) -> bool:
        return self.refusal is None


async def prepare_for_execution(session_factory, *, job_id: str, geometry_source,
                                geometry_interpretation, classify_checkpoint,
                                checkpoint_thread: str, job_repo, jlog,
                                publish) -> ExecutionPreparation:
    from meshpipeline.errors import (
        FailureClass,
        record_dead_letter,
        user_message_for,
    )

    try:
        disposition = await classify_checkpoint(checkpoint_thread)
        jlog.info("Checkpoint disposition for job %s: %s", job_id, disposition)
        materialized = None
        if disposition != "complete":
            materialized = await prepare_execution_geometry(
                geometry_source, geometry_interpretation, job_id=job_id)
        return ExecutionPreparation(materialized, None, disposition)
    except GeometrySourceError as exc:
        jlog.error("Execution preparation REJECT - %s (job_id=%s)", exc, job_id)
        fc = getattr(exc, "failure_class", None) or FailureClass.INTERNAL
        dep = getattr(exc, "dependency", "geometry_source")
        # DURABLE refusal: the job row is transitioned to failed with the classified reason. A
        # refusal that only returned a dict left the row `running`, so a resumed run that could
        # not verify its approved bytes reported failure to the caller while the durable record
        # still claimed it was in flight.
        await _fail_job_row(session_factory, job_id, fc, job_repo=job_repo, jlog=jlog)
        record_dead_letter(job_id, fc, dep, str(exc))
        try:
            publish(user_message_for(fc))
        except Exception:                          # noqa: BLE001
            pass
        reason = "geometry_unavailable" if dep == "geometry_source" else "checkpoint_unreadable"
        return ExecutionPreparation(
            None, {"job_id": job_id, "status": "failed", "reason": reason}, "")


async def _fail_job_row(session_factory, job_id: str, failure_class, *, job_repo, jlog) -> None:
    from meshpipeline.errors import failed_reason_for
    from meshpipeline.persistence.job_state import TransitionResult
    from meshpipeline.persistence.models import FailedReason, JobStatus

    try:
        async with session_factory() as db:
            applied = await job_repo.transition(db, uuid.UUID(job_id), JobStatus.failed)
            if applied == TransitionResult.applied:
                row = await job_repo.get_internal(db, uuid.UUID(job_id))
                if row:
                    try:
                        row.failed_reason = FailedReason(failed_reason_for(failure_class))
                    except (ValueError, AttributeError):
                        row.failed_reason = FailedReason.unhandled
            await db.commit()
    except Exception as exc:                       # noqa: BLE001 - the refusal must still stand
        jlog.warning("execution preparation: could not mark job failed: %s", exc)
