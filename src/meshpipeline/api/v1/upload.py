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
from meshpipeline.api.security import owner_dep
from meshpipeline.contracts.intake_formats import (
    ACCEPTED_SUFFIXES,
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

# .vtp is a VTK PolyData surface - vmtk's native lumen input (a segmented vessel/duct
# surface). The engine's staging seam converts between the surface formats it needs.
_MAX_FILE_BYTES   = 500 * 1024 * 1024



class StepFileOut(BaseModel):
    session_id:      str
    step_filename:   str
    intake_greeting: str
    # The greeting turn's public trace, re-projected for the mode running NOW - the
    # same contract the chat turn returns, so the browser has one renderer.
    trace: list[dict] = []



@router.post("/step-file", response_model=StepFileOut)
async def upload_step_file(
    file:     UploadFile = File(..., description="Geometry file - a surface (.stl, .vtp) or CAD (.step/.stp, .iges/.igs). The selected engine's staging seam converts it to what that engine meshes."),
    owner_id: str        = Depends(owner_dep),
):
    filename = file.filename or ""
    import re as _re
    _safe_stem = _re.sub(r"[^A-Za-z0-9_\-.]", "_", Path(filename).stem)[:64]
    filename   = _safe_stem + Path(filename).suffix.lower()
    suffix = Path(filename).suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        raise HTTPException(status_code=422, detail=unsupported_message(suffix))

    try:
        from meshpipeline.application.job_service import JobService
        from meshpipeline.persistence.session import get_db
        _svc = JobService()
        async with get_db() as db:
            await _svc.check_quotas(db, owner_id)
            session_id = await _svc.create_session(db, owner_id)
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

    if suffix in (".step", ".stp"):
        try:
            with open(dest, "rb") as _fh:
                _header = _fh.read(64)
            if not _header.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"ISO-10303-21"):
                dest.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=400,
                    detail="File does not appear to be a valid STEP file (missing ISO-10303-21 header).",
                )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("upload_step_file: could not validate STEP header: %s", exc)

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
                                         object_key=object_key)
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
        async with get_db() as db:
            row = GeometrySource(
                id=source_id, owner_id=owner_id, original_filename=filename,
                suffix_hint=suffix, object_key=object_key, sha256=digest, size_bytes=counted)
            db.add(row)
            await db.flush()
            # WHAT SIZE these bytes are, when the file itself says so credibly. Recorded in the
            # same transaction as the source, because a source with a verified declaration and no
            # interpretation would send the user a question the file already answered.
            # Unresolved is a legitimate, common outcome - STL and VTP carry no unit at all, and a
            # STEP declaration that collides with the parser's own failure default is not evidence.
            # Those leave the session without an interpretation so intake asks; nothing is guessed
            # here, and no default is written.
            interpretation_id = None
            evidence = _declared_unit_evidence(dest)
            if evidence is not None and evidence.resolved and evidence.unit is not None:
                from meshpipeline.contracts.geometry_units import ResolutionBasis
                from meshpipeline.persistence.repositories.geometry_interpretation_repository import (  # noqa: E501
                    GeometryInterpretationRepository,
                )
                recorded = await GeometryInterpretationRepository().record(
                    db, owner_id=owner_id, geometry_source_id=source_id, unit=evidence.unit,
                    basis=ResolutionBasis.file_declared, evidence=evidence.detail)
                interpretation_id = _uuid.UUID(recorded.interpretation_id)
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



def _declared_unit_evidence(staged: Path):
    try:
        from meshpipeline.cad.unit_evidence import read_declared_unit
        return read_declared_unit(staged)
    except Exception as exc:  # noqa: BLE001 - unreadable evidence is "ask the user", not a 500
        logger.warning("upload_step_file: could not read the declared unit (%s); "
                       "the scale will be confirmed in conversation", exc)
        return None


def _project_trace(events: list) -> list:
    from meshpipeline.trace.sink import project_all
    return project_all(events)
