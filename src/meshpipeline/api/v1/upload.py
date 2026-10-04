# Responsibility: Accept a geometry upload and turn it into a content-addressed source.
# Boundaries: accepted formats come from the intake-format declaration.
from __future__ import annotations

import logging
import uuid as _uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

import meshpipeline.agents.intake.settings as icfg
import meshpipeline.settings.runtime as rtcfg
from meshpipeline.api.security import org_dep, owner_dep, plan_dep
from meshpipeline.cad.ingest.upload_check import STEP_HEADER_REFUSAL as _STEP_HEADER_REFUSAL
from meshpipeline.contracts.intake_formats import (
    ACCEPTED_SUFFIXES,
    refusal_suffix,
    staged_name_for,
    unsupported_message,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# THE upload acknowledgement. One sentence pair, owned here, returned to the caller and stored as
# the session's first assistant message - so the UI renders what the API said instead of keeping a
# second copy of the wording. It states only what is true the instant an upload lands: bytes
# arrived. It does not name a format, claim the file was opened, or suggest an engine, because at
# this point nothing has parsed the geometry and the engine is the user's choice.
UPLOAD_ACKNOWLEDGEMENT = (
    "Geometry received. What are you meshing this for?"
)

_JOBS_DIR = Path(rtcfg.JOBS_DIR)
_JOBS_DIR.mkdir(parents=True, exist_ok=True)

# Every accepted format (contracts/intake_formats) is stored as uploaded; cad/ingest turns it into
# a canonical CAD solid or STL surface where it is read (the job materialiser, the geometry check).
_MAX_FILE_BYTES   = 500 * 1024 * 1024

# The IGES unit is read by OpenCASCADE, which loads the whole model to answer - several times the
# file's size in memory. Above this the upload does not ask it: the unit is left unresolved and is
# confirmed in conversation, which is the ordinary outcome for any file that states no unit. STEP
# is read from its text and has no such cost.
_IGES_UNIT_READ_MAX_BYTES = 64 * 1024 * 1024


def sanitised_upload_name(filename: str) -> tuple[str, str]:
    """The name an upload is recorded under, and its lowercase suffix.

    The client's name is never trusted as a path: the stem keeps only safe characters and is
    capped, and only the suffix carries forward, because format dispatch keys off it. Shared by the
    multipart and the direct upload, so both record a file under the same name.
    """
    import re as _re
    raw = filename or ""
    safe_stem = _re.sub(r"[^A-Za-z0-9_\-.]", "_", Path(raw).stem)[:64]
    cleaned = safe_stem + Path(raw).suffix.lower()
    return cleaned, Path(cleaned).suffix.lower()


def checked_upload(dest: Path, suffix: str) -> tuple[Path, str]:
    """THE content check every upload makes, shared by the multipart and the direct route.

    The bytes decide, not the name (cad/ingest/upload_check): a STEP without its ISO-10303-21
    header, a renamed SolidWorks part, a zip bomb posing as 3MF or a glTF that points at files the
    upload does not carry are refused here with a sentence that says what to do instead. A file
    that is plainly ANOTHER accepted format than its name says (an STL named .obj) is kept, and
    recorded as what it is. Returns the staged file and the suffix its bytes really are."""
    from meshpipeline.cad.ingest import check_upload

    try:
        verdict = check_upload(dest, suffix)
    except Exception as exc:  # noqa: BLE001 - a check that cannot run must not cost the upload
        logger.warning("upload: the content check could not run (%s); the file is kept and read "
                       "on the worker", exc)
        return dest, suffix
    if not verdict.ok:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=verdict.status, detail=verdict.refusal)
    if verdict.suffix != suffix:
        moved = dest.with_name(staged_name_for(verdict.suffix))
        dest.replace(moved)
        logger.info("upload: a %s-named file is a %s file by its content; recorded as such",
                    suffix, verdict.suffix)
        return moved, verdict.suffix
    return dest, suffix


STEP_HEADER_REFUSAL = _STEP_HEADER_REFUSAL



class StepFileOut(BaseModel):
    session_id:      str
    step_filename:   str
    intake_greeting: str
    # The greeting turn's public trace, re-projected for the mode running NOW - the
    # same contract the chat turn returns, so the browser has one renderer.
    trace: list[dict] = []



@router.post(
    "/step-file", response_model=StepFileOut,
    description=(
        "Upload a geometry file in the request body (multipart form field `file`). Suited to "
        "files up to about 30 MB: a hosted deployment's front end refuses a larger request body "
        "before it reaches the API (HTTP 413). For a larger file use the direct upload - "
        "POST /api/v1/upload/direct, PUT the bytes to the `upload_url` it returns, then POST its "
        "`finalize_url` - which answers exactly what this route answers."))
async def upload_step_file(
    file:     UploadFile = File(..., description="Geometry file. CAD: .step/.stp, .iges/.igs, .brep. Surface or volume meshes: .stl, .obj, .ply, .off, .3mf, .glb, .gltf (single file), .vtk, .vtp, .vtu, .msh (Gmsh or Fluent), .bdf/.nas, .inp, .mesh, .su2, .3dm (Rhino meshes). The contents decide the format, not the name. A volume mesh is read as its boundary surface; named groups become named regions. Every file becomes a canonical CAD solid or STL surface before an engine sees it. GET /api/v1/client-config lists the formats."),
    owner_id: str        = Depends(owner_dep),
    plan:     str        = Depends(plan_dep),
    organization_id: str = Depends(org_dep),
):
    filename, suffix = sanitised_upload_name(file.filename or "")
    if suffix not in ACCEPTED_SUFFIXES:
        raise HTTPException(status_code=422, detail=unsupported_message(refusal_suffix(filename)))

    try:
        from meshpipeline.application.job_service import JobService
        from meshpipeline.persistence.session import get_db
        _svc = JobService()
        async with get_db() as db:
            await _svc.check_quotas(db, owner_id, plan=plan)
            session_id = await _svc.create_session(db, owner_id,
                                                    organization_id=organization_id)
            await db.commit()
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("upload_step_file: failed to create session: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to create chat session") from exc

    session_dir = _JOBS_DIR / str(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    # preserve the CAD format in the filename so the builder's tessellator picks the
    # right OCC reader (STEP vs IGES); STL and VTP are consumed directly. The mapping lives
    # with the format registry so it cannot drift from what the server accepts.
    dest = session_dir / staged_name_for(suffix)

    bytes_written = 0
    try:
        with open(dest, "wb") as fh:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > _MAX_FILE_BYTES:
                    fh.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"File exceeds {_MAX_FILE_BYTES // (1024*1024)} MB limit",
                    )
                fh.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save uploaded file: {exc}") from exc

    if bytes_written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Uploaded file is empty")

    # off the event loop: the check reads the file, and a large one must not stall other requests
    import asyncio as _asyncio
    dest, suffix = await _asyncio.to_thread(checked_upload, dest, suffix)

    step_filename = filename

    # DURABLE SOURCE. The staged file is a streaming buffer, not identity: the pipeline may run in
    # a different container, so the bytes go to object storage and the row records what they must
    # be. The digest is taken from the staged file, so it describes what was actually written.
    from meshpipeline.contracts.geometry_source import sha256_of, source_object_key
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.session import get_db

    digest, counted = sha256_of(dest)
    source_id = _uuid.uuid4()
    object_key = source_object_key(source_id)

    # WRITE-AHEAD CLEANUP INTENT. Committed BEFORE the object exists, because the two windows that
    # strand bytes cannot be covered by recording after the fact: the database may be the very
    # thing that failed, and the process may die between the write and the source transaction.
    # From here on this object is discoverable by maintenance whatever happens next, so it can
    # never be reclaimable only from a log line.
    from meshpipeline.persistence.models import ReconciliationState
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    _cleanup = SourceCleanupRepository()

    async def _close_intent(state: ReconciliationState, detail: str) -> None:
        # Best effort by design: the sweep reaches the same conclusion from the object itself, so a
        # failure here delays reclamation rather than losing it.
        try:
            async with get_db() as db:
                await _cleanup.resolve(db, object_key=object_key, state=state, detail=detail)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - never mask the caller's own failure
            logger.warning("upload_step_file: could not close the cleanup intent for source %s "
                           "(it stays pending for maintenance): %s", source_id, exc)

    try:
        async with get_db() as db:
            await _cleanup.record_intent(db, owner_id=owner_id, source_id=source_id,
                                         object_key=object_key,
                                         organization_id=organization_id)
            await db.commit()
    except Exception as exc:
        # FAIL CLOSED. Writing bytes that nothing durable names is exactly the state this intent
        # exists to prevent, so the upload is refused instead - with the established storage
        # message, because from the caller's side nothing was saved either way.
        dest.unlink(missing_ok=True)
        logger.error("upload_step_file: could not record the source cleanup intent - refusing to "
                     "store an object nothing could reclaim - session_id=%s: %s", session_id, exc)
        raise HTTPException(status_code=503,
                            detail="Storage is unavailable - the upload was not saved.") from exc

    try:
        get_object_store().upload_file(local_path=dest, object_key=object_key)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        logger.error("upload_step_file: object upload failed - session_id=%s: %s", session_id, exc)
        await _close_intent(ReconciliationState.resolved_deleted,
                            "object write failed; nothing was stored")
        raise HTTPException(status_code=503,
                            detail="Storage is unavailable - the upload was not saved.") from exc

    # COMPENSATION. The object exists now, so a database failure would strand it. Only this exact
    # key is removed - it was generated from a uuid moments ago and belongs to no other source -
    # and a failed removal is reported loudly, because the alternative is an object nobody knows
    # about. A prefix delete here could destroy another tenant's upload and is never used.
    try:
        from sqlalchemy import update as _sa_update

        from meshpipeline.persistence.models import ChatSession, GeometrySource
        from meshpipeline.persistence.repositories import tenant_scope
        async with get_db() as db:
            row = GeometrySource(
                id=source_id,
                **tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id),
                original_filename=filename,
                suffix_hint=suffix, object_key=object_key, sha256=digest, size_bytes=counted)
            db.add(row)
            await db.flush()
            from meshpipeline.contracts.geometry_source import GeometrySourceRef
            _source_payload = GeometrySourceRef.from_row(row).to_payload()
            _interpretation_payload = None
            # WHAT SIZE these bytes are, when the file itself says so credibly. Recorded in the
            # same transaction as the source, because a source with a verified declaration and no
            # interpretation would send the user a question the file already answered.
            # Unresolved is a legitimate, common outcome - STL and VTP carry no unit at all, and a
            # STEP unit entity that does not parse is not evidence (a well-formed metre is: the
            # entity is read from the file's text, not from the parser's fallback). Those leave
            # the session without an interpretation so the unit is proposed and confirmed; nothing
            # is guessed here, and no default is written. A declaration the part's size makes
            # implausible is doubted on the stage, beside the size, before anything is confirmed.
            interpretation_id = None
            evidence = _declared_unit_evidence(dest)
            if evidence is not None and evidence.resolved and evidence.unit is not None:
                from meshpipeline.contracts.geometry_units import ResolutionBasis
                from meshpipeline.persistence.repositories.geometry_interpretation_repository import (  # noqa: E501
                    GeometryInterpretationRepository,
                )
                recorded = await GeometryInterpretationRepository().record(
                    db, owner_id=owner_id, geometry_source_id=source_id, unit=evidence.unit,
                    basis=ResolutionBasis.file_declared, evidence=evidence.detail,
                    organization_id=organization_id)
                interpretation_id = _uuid.UUID(recorded.interpretation_id)
                from dataclasses import asdict as _asdict

                from meshpipeline.contracts.geometry_source import GeometryInterpretationRef
                _interpretation_payload = _asdict(GeometryInterpretationRef.from_domain(recorded))
            await db.execute(
                _sa_update(ChatSession).where(ChatSession.id == session_id)
                .values(geometry_source_id=source_id,
                        geometry_interpretation_id=interpretation_id))
            # CLOSED IN THE SAME TRANSACTION as the source row. That is what removes the window a
            # crash could land in: the source becoming durable and its intent being closed are one
            # commit, so a committed source never has an open intent, and a rolled-back source
            # never has a closed one.
            await _cleanup.resolve(db, object_key=object_key,
                                   state=ReconciliationState.resolved_adopted,
                                   detail="the geometry source row owns this object")
            await db.commit()
    except Exception as exc:
        logger.error("upload_step_file: failed to persist the geometry source: %s", exc)
        try:
            get_object_store().delete_object(object_key=object_key)
            logger.info("upload_step_file: compensated - removed orphan object for source %s",
                        source_id)
            await _close_intent(ReconciliationState.resolved_deleted,
                                "compensated at upload time")
        except Exception as cleanup_exc:
            # The object survives, but it is NOT lost: the intent recorded before the write is
            # still pending, so the maintenance sweep owns it from here. This log is now a
            # diagnostic, not the only record of the object's existence.
            logger.error("upload_step_file: compensation delete failed for source %s - the "
                         "recorded cleanup intent stays pending and maintenance will retry it: %s",
                         source_id, cleanup_exc)
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500,
                            detail="Failed to record the uploaded geometry.") from exc

    # The staging buffer has served its purpose; the durable copy is the object. Nothing local
    # survives this line, which is precisely why no geometry handle is constructed here - the
    # session now points at the source row, and the execution entry materialises from that.
    dest.unlink(missing_ok=True)
    logger.info("upload_step_file: stored %d bytes as source %s - session_id=%s owner=%s",
                counted, source_id, session_id, owner_id)

    # THE GEOMETRY CHECK: scouted on the worker from the moment the bytes are safe, so the
    # labelled picture is ready by the time the user has answered the first question. Never in
    # the upload's own failure path - a check that cannot start is a note, not a lost upload.
    _enqueue_geometry_check(str(session_id), owner_id, source_payload=_source_payload,
                            interpretation_payload=_interpretation_payload)

    # An upload is an upload. Nothing has been parsed, measured or checked at this point, so the
    # acknowledgement is a fixed server-owned sentence rather than a model turn: with no request
    # text and no materialised file there is nothing for a model to reason about, and the only
    # thing it could add is a claim about geometry nobody has looked at yet.
    greeting = UPLOAD_ACKNOWLEDGEMENT if icfg.INTAKE_GREETING_ON_UPLOAD else ""
    _greeting_trace: list = []   # no node executed on this path, so there is no turn to project

    if greeting:
        try:
            from meshpipeline.persistence.repositories.session_repository import SessionRepository
            async with get_db() as db:
                await SessionRepository().append_message(db, session_id, "assistant", greeting)
                await db.commit()
        except Exception as exc:
            logger.warning("upload_step_file: failed to persist greeting to session: %s", exc)

    return StepFileOut(
        session_id=str(session_id),
        step_filename=step_filename,
        intake_greeting=greeting,
        trace=_project_trace(_greeting_trace),
    )



def _enqueue_geometry_check(session_id: str, owner_id: str, *, source_payload: dict,
                            interpretation_payload: dict | None) -> None:
    import meshpipeline.settings.geometry_check as gcfg

    if not gcfg.GEOMETRY_CHECK_ENABLED:
        return
    from meshpipeline.application.geometry_check import start_scout

    # The "pending" marker goes down before the task is published, and a scout nobody will run
    # is marked failed rather than left pending; the retry route starts the same way.
    start_scout(session_id, owner_id, source=source_payload, interpretation=interpretation_payload)


def _declared_unit_evidence(staged: Path):
    try:
        from meshpipeline.cad.unit_evidence import UnitEvidence, read_declared_unit
        if (staged.suffix.lower() in (".iges", ".igs")
                and staged.stat().st_size > _IGES_UNIT_READ_MAX_BYTES):
            return UnitEvidence.unresolved(
                "the IGES file is too large to read its unit at upload")
        return read_declared_unit(staged)
    except Exception as exc:  # noqa: BLE001 - unreadable evidence is "ask the user", not a 500
        logger.warning("upload_step_file: could not read the declared unit (%s); "
                       "the scale will be confirmed in conversation", exc)
        return None


def _project_trace(events: list) -> list:
    from meshpipeline.trace.sink import project_all
    return project_all(events)
