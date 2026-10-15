# Responsibility: Accept a geometry upload whose bytes go straight from the client to the object store, then check and adopt it.
# Boundaries: the two routes of the direct upload. The checks, the source record and the answer are the multipart upload's, reused.
# Collaborates with: api/v1/upload.py (the checks and the answer), the source cleanup intent, and the object store's signed PUT.

# WHY THIS EXISTS. A hosted deployment's front end caps a request body: Cloud Run refuses an HTTP/1
# request over 32 MiB with "413 Request Entity Too Large" before the application sees a byte, and
# the console relays the upload through a service with the same cap. Real aerospace and automotive
# CAD is routinely 40-500 MB. So a large file does not travel in a request body at all:
#
#   1. POST /direct               the API checks the name, the size and the quota, writes the
#                                 cleanup intent, and hands back a signed PUT URL for one object key
#   2. PUT <upload_url>           the client sends the bytes straight to the object store
#   3. POST /direct/{id}/finalize the API reads the object back and makes EVERY check the multipart
#                                 upload makes - the suffix, the size cap, the ISO-10303-21 header,
#                                 the declared unit - then records the source and opens the session,
#                                 and answers exactly what POST /step-file answers
#
# The bytes are untrusted until step 3 has read them: a client holding the URL can PUT anything, of
# any size. What was declared at step 1 is only used to refuse early.
#
# THE GUARANTEES OF THE MULTIPART PATH HOLD HERE TOO:
#   - the cleanup intent is committed BEFORE anything can be written (step 1), and it is HELD so the
#     maintenance sweep does not reclaim the object while its bytes may still be arriving
#   - a committed source never has an open intent: the source row, the session and the intent's
#     close are ONE transaction
#   - only the exact key is ever deleted, and only while holding the intent still pending
#   - an upload id is scoped to the tenant and to the person who began it; anyone else's reads as
#     one that never existed
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid as _uuid
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import meshpipeline.agents.intake.settings as icfg
from meshpipeline.api.security import org_dep, owner_dep, plan_dep
from meshpipeline.api.v1 import upload as _multipart
from meshpipeline.contracts.intake_formats import (
    ACCEPTED_SUFFIXES,
    refusal_suffix,
    staged_name_for,
    unsupported_message,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: How long the signed PUT may take to START. A PUT begun inside this time runs to completion
#: however long the transfer takes; the client asks for the URL immediately before sending.
UPLOAD_URL_TTL = timedelta(minutes=30)

#: How long maintenance leaves a direct upload's cleanup intent alone. Covers the URL's life plus
#: the slowest transfer worth waiting for (500 MB at ~250 kbit/s); after it, an upload nobody
#: finished is reclaimed by the ordinary sweep - exact key, exactly as a failed multipart upload is.
UPLOAD_HOLD = timedelta(hours=6)

#: Reading an object back stages it on this process's disk, which on Cloud Run is memory. Above this
#: size one object at a time is staged per process, so two 500 MB checks cannot land together.
_LARGE_BYTES = 64 * 1024 * 1024
_large_slot = asyncio.Semaphore(1)

#: The Content-Type the client is asked to send. Not signed - any value is accepted - but naming one
#: keeps the browser's CORS preflight predictable.
_PUT_HEADERS = {"Content-Type": "application/octet-stream"}

_NOT_FOUND = ("No upload with that id is waiting to be finished. Start the upload again with "
              "POST /api/v1/upload/direct.")
_EXPIRED = ("This upload was not finished in time and its file has been removed. "
            "Upload the file again.")
_NOT_ARRIVED = ("The file has not reached storage. Send it to the upload link first (a PUT of the "
                "whole file), then finish the upload.")
_STORE_DOWN = "Storage is unavailable - the upload was not saved."


class DirectUploadIn(BaseModel):
    filename: str = Field(..., min_length=1, max_length=1024,
                          description="The file's name. Its suffix must be an accepted geometry "
                                      "format (GET /api/v1/client-config lists them: STEP, IGES, "
                                      "BREP, STL, OBJ, PLY, OFF, 3MF, glTF/GLB, VTK/VTP/VTU, MSH, "
                                      "Nastran, Abaqus, Medit, SU2, Rhino 3DM, ECXML thermal "
                                      "models); finalize then "
                                      "reads the bytes and they decide. Send the same name to "
                                      "finalize.")
    size_bytes: int = Field(..., ge=0, description="The file's size in bytes, so an empty or "
                                                   "oversized file is refused before it is sent.")


class DirectUploadOut(BaseModel):
    upload_id:    str
    #: PUT the whole file here, as the request body, with `headers`. No credential is needed or
    #: accepted: the URL is the permission, for this one object, until `expires_at`.
    upload_url:   str
    method:       str = "PUT"
    headers:      dict[str, str]
    #: The latest moment the PUT may START. A transfer started before it runs to completion.
    expires_at:   str
    max_bytes:    int
    #: POST here (same credentials as this call, body {"filename": ...}) once the PUT succeeded.
    finalize_url: str


class _Scope(TypedDict):
    """One upload as its owner names it - the key every read, lock and delete of it is scoped by."""
    source_id: _uuid.UUID
    object_key: str
    owner_id: str
    organization_id: str


class FinalizeIn(BaseModel):
    filename: str = Field(..., min_length=1, max_length=1024,
                          description="The file's name, as given when the upload began.")


def _mb(n: int) -> int:
    return max(1, round(n / (1024 * 1024)))


def _too_large(size: int) -> str:
    return (f"This file is {_mb(size)} MB. The largest file accepted is "
            f"{_multipart._MAX_FILE_BYTES // (1024 * 1024)} MB - export only the bodies you want "
            f"meshed, or a lighter surface, and upload that.")


def _checked_name(filename: str) -> tuple[str, str]:
    cleaned, suffix = _multipart.sanitised_upload_name(filename)
    if suffix not in ACCEPTED_SUFFIXES:
        raise HTTPException(status_code=422, detail=unsupported_message(refusal_suffix(cleaned)))
    return cleaned, suffix


@router.post(
    "/direct", response_model=DirectUploadOut,
    summary="Begin a direct upload (for files too large for a request body)",
    description=(
        "Step 1 of 3 for a large geometry file. Checks the name, the declared size and the "
        "caller's quota, and returns a signed `upload_url`. Step 2: PUT the whole file to "
        "`upload_url` with `headers` (no other credential). Step 3: POST `finalize_url` with "
        "{\"filename\": ...}; it checks the bytes that arrived and answers exactly what "
        "POST /api/v1/upload/step-file answers. The PUT must start before `expires_at`."))
async def begin_direct_upload(
    body: DirectUploadIn,
    owner_id: str        = Depends(owner_dep),
    plan:     str        = Depends(plan_dep),
    organization_id: str = Depends(org_dep),
) -> DirectUploadOut:
    _checked_name(body.filename)
    if body.size_bytes == 0:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if body.size_bytes > _multipart._MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=_too_large(body.size_bytes))

    from meshpipeline.application.job_service import JobService
    from meshpipeline.persistence.session import get_db

    # THE QUOTA, before a byte moves - the same refusal the multipart upload gives. Asked again,
    # authoritatively, in the transaction that opens the session.
    try:
        async with get_db() as db:
            await JobService().check_quotas(db, owner_id, plan=plan)
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("begin_direct_upload: quota check failed: %s", exc)
        raise HTTPException(status_code=503,
                            detail="The upload could not be started - try again in a moment.") from exc

    from meshpipeline.contracts.geometry_source import source_object_key
    from meshpipeline.contracts.object_storage import get_object_store
    from meshpipeline.persistence.models import ReconciliationState
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )

    source_id = _uuid.uuid4()
    object_key = source_object_key(source_id)
    repo = SourceCleanupRepository()

    # WRITE-AHEAD, AND HELD. Committed before the URL exists, so no object can ever be written under
    # a key nothing durable names; held, so the sweep does not reclaim it while the bytes are still
    # on their way. An upload nobody finishes is reclaimed by the sweep once the hold has passed.
    try:
        async with get_db() as db:
            await repo.record_intent(db, owner_id=owner_id, source_id=source_id,
                                     object_key=object_key, organization_id=organization_id,
                                     hold=UPLOAD_HOLD)
            await db.commit()
    except Exception as exc:
        logger.error("begin_direct_upload: could not record the cleanup intent - refusing to hand "
                     "out a URL for an object nothing could reclaim: %s", exc)
        raise HTTPException(status_code=503, detail=_STORE_DOWN) from exc

    try:
        url = await asyncio.to_thread(get_object_store().create_upload_url,
                                      object_key=object_key, expires_in=UPLOAD_URL_TTL)
    except Exception as exc:
        logger.error("begin_direct_upload: could not sign an upload URL for %s: %s", source_id, exc)
        # No URL was issued, so nothing can ever arrive under this key: the intent is settled now
        # rather than left for the sweep.
        await _close_intent(object_key, ReconciliationState.resolved_deleted,
                            "no upload link was issued; nothing can be stored")
        raise HTTPException(status_code=503, detail=_STORE_DOWN) from exc

    logger.info("begin_direct_upload: upload %s begun (%d bytes declared) owner=%s",
                source_id, body.size_bytes, owner_id)
    return DirectUploadOut(
        upload_id=str(source_id), upload_url=url, headers=dict(_PUT_HEADERS),
        expires_at=(datetime.now(UTC) + UPLOAD_URL_TTL).isoformat(),
        max_bytes=_multipart._MAX_FILE_BYTES,
        finalize_url=f"/api/v1/upload/direct/{source_id}/finalize")


class _Settled(Exception):
    """The intent was no longer pending when the finish came to adopt it."""

    def __init__(self, state):
        super().__init__(str(state))
        self.state = state


class _QuotaRefused(Exception):
    pass


@router.post(
    "/direct/{upload_id}/finalize", response_model=_multipart.StepFileOut,
    summary="Finish a direct upload: check the bytes that arrived and open the session",
    description=(
        "Step 3 of 3. Reads back what was PUT to the upload URL and makes every check "
        "POST /api/v1/upload/step-file makes. Answers the same body. Safe to repeat: finishing an "
        "upload that is already finished answers with the session it opened."))
async def finalize_direct_upload(
    upload_id: str,
    body: FinalizeIn,
    owner_id: str        = Depends(owner_dep),
    plan:     str        = Depends(plan_dep),
    organization_id: str = Depends(org_dep),
) -> _multipart.StepFileOut:
    filename, suffix = _checked_name(body.filename)
    try:
        source_id = _uuid.UUID(upload_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND) from exc

    from meshpipeline.contracts.geometry_source import sha256_of, source_object_key
    from meshpipeline.contracts.object_storage import ObjectNotFound, get_object_store
    from meshpipeline.persistence.models import ReconciliationState
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    object_key = source_object_key(source_id)
    repo = SourceCleanupRepository()
    scope: _Scope = {"source_id": source_id, "object_key": object_key, "owner_id": owner_id,
                     "organization_id": organization_id}

    # 1. WHOSE UPLOAD, AND IS IT STILL OPEN. Someone else's id is indistinguishable from none.
    try:
        async with get_db() as db:
            intent = await repo.get_upload_for_owner(db, **scope)
            state = intent.state if intent is not None else None
    except Exception as exc:
        logger.error("finalize_direct_upload: could not read upload %s: %s", source_id, exc)
        raise HTTPException(status_code=503, detail="The upload could not be looked up - "
                                                    "finish it again in a moment.") from exc
    if state is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if state is ReconciliationState.resolved_adopted:
        return await _replay(source_id, filename, owner_id, organization_id)
    if state is not ReconciliationState.pending:
        raise HTTPException(status_code=410, detail=_EXPIRED)

    try:
        store = get_object_store()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_STORE_DOWN) from exc

    # 2. WHAT ARRIVED - its size, before a byte of it is read.
    try:
        stored = await asyncio.to_thread(store.stat_object, object_key=object_key)
    except ObjectNotFound as exc:
        # Not refused and not reclaimed: the PUT may still be running, or may be retried. The
        # intent stays pending and held.
        raise HTTPException(status_code=409, detail=_NOT_ARRIVED) from exc
    except Exception as exc:
        logger.error("finalize_direct_upload: stat failed for %s: %s", source_id, exc)
        raise HTTPException(status_code=503, detail=_STORE_DOWN) from exc
    size = int(stored.size_bytes)
    if size <= 0:
        await _discard(store, scope, "the uploaded object was empty")
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if size > _multipart._MAX_FILE_BYTES:
        await _discard(store, scope, "the uploaded object exceeded the size limit")
        raise HTTPException(status_code=413, detail=_too_large(size))

    # 3. READ IT BACK AND CHECK IT - the multipart upload's checks, on the bytes that are stored.
    staging = _multipart._JOBS_DIR / f"direct-{source_id}"
    dest = staging / staged_name_for(suffix)
    try:
        slot = _large_slot if size > _LARGE_BYTES else None
        if slot is not None:
            await slot.acquire()
        try:
            staging.mkdir(parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(store.download_file, object_key=object_key,
                                        destination=dest)
            except ObjectNotFound as exc:
                raise HTTPException(status_code=409, detail=_NOT_ARRIVED) from exc
            except Exception as exc:
                logger.error("finalize_direct_upload: read-back failed for %s: %s", source_id, exc)
                raise HTTPException(status_code=503, detail=_STORE_DOWN) from exc

            try:
                dest, suffix = await asyncio.to_thread(_multipart.checked_upload, dest, suffix)
            except HTTPException:
                await _discard(store, scope, "the uploaded object is not geometry this product "
                                             "reads")
                raise

            digest, counted = await asyncio.to_thread(sha256_of, dest)
            if counted != size:
                # A second PUT to the same link landed between the size check and the read. What
                # was checked is not what is stored, so neither is kept.
                await _discard(store, scope, "the uploaded object changed while it was checked")
                raise HTTPException(status_code=409, detail=(
                    "The file changed while it was being checked. Upload it again."))
            evidence = await asyncio.to_thread(_multipart._declared_unit_evidence, dest)
        finally:
            # The copy is gone before the slot is handed on: the next large check must not find
            # this one's bytes still occupying the disk (memory, on Cloud Run).
            shutil.rmtree(staging, ignore_errors=True)
            if slot is not None:
                slot.release()

        # 4. ADOPT IT. The session, the source row, its interpretation and the intent's close are
        # ONE transaction, taken under the intent's row lock: a committed source never has an open
        # intent, and a second finish of the same upload waits and then sees this one's verdict.
        try:
            recorded = await _adopt(repo, scope, filename=filename, suffix=suffix, digest=digest,
                                    counted=counted, evidence=evidence, plan=plan)
        except _QuotaRefused as exc:
            # The bytes are fine and are kept: the intent stays pending and held, so finishing
            # again once a running job has ended needs no new upload.
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except _Settled as settled:
            if settled.state is ReconciliationState.resolved_adopted:
                return await _replay(source_id, filename, owner_id, organization_id)
            if settled.state is None:
                raise HTTPException(status_code=404, detail=_NOT_FOUND) from None
            raise HTTPException(status_code=410, detail=_EXPIRED) from None
        except Exception as exc:
            # Nothing was committed and the object is intact under its still-pending intent, so the
            # upload can simply be finished again; if it never is, the sweep reclaims it after the
            # hold. Deleting it here would make a database hiccup cost a 500 MB re-upload.
            logger.error("finalize_direct_upload: could not record source %s: %s", source_id, exc)
            raise HTTPException(status_code=503, detail=(
                "The upload could not be recorded - finish it again in a moment. The file is "
                "kept, so it does not need to be sent again.")) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    session_id, source_payload, interpretation_payload = recorded
    logger.info("finalize_direct_upload: stored %d bytes as source %s - session_id=%s owner=%s",
                counted, source_id, session_id, owner_id)
    _multipart._enqueue_geometry_check(str(session_id), owner_id, source_payload=source_payload,
                                       interpretation_payload=interpretation_payload)

    greeting = _multipart.UPLOAD_ACKNOWLEDGEMENT if icfg.INTAKE_GREETING_ON_UPLOAD else ""
    if greeting:
        try:
            from meshpipeline.persistence.repositories.session_repository import (
                SessionRepository,
            )
            async with get_db() as db:
                await SessionRepository().append_message(db, session_id, "assistant", greeting)
                await db.commit()
        except Exception as exc:
            logger.warning("finalize_direct_upload: failed to persist greeting to session: %s", exc)
    return _multipart.StepFileOut(session_id=str(session_id), step_filename=filename,
                                  intake_greeting=greeting,
                                  trace=_multipart._project_trace([]))


async def _adopt(repo, scope: _Scope, *, filename: str, suffix: str, digest: str, counted: int,
                 evidence, plan: str):
    from dataclasses import asdict as _asdict

    from sqlalchemy import update as _sa_update

    from meshpipeline.application.job_service import JobService
    from meshpipeline.contracts.geometry_source import (
        GeometryInterpretationRef,
        GeometrySourceRef,
    )
    from meshpipeline.persistence.models import ChatSession, GeometrySource, ReconciliationState
    from meshpipeline.persistence.repositories import tenant_scope
    from meshpipeline.persistence.session import get_db

    owner_id, organization_id = scope["owner_id"], scope["organization_id"]
    source_id, object_key = scope["source_id"], scope["object_key"]
    svc = JobService()
    async with get_db() as db:
        intent = await repo.lock_upload_for_owner(db, **scope)
        if intent is None or intent.state is not ReconciliationState.pending:
            raise _Settled(intent.state if intent is not None else None)
        try:
            await svc.check_quotas(db, owner_id, plan=plan)
        except ValueError as exc:
            raise _QuotaRefused(str(exc)) from exc
        session_id = await svc.create_session(db, owner_id, organization_id=organization_id)
        row = GeometrySource(
            id=source_id,
            **tenant_scope.stamp(owner_id=owner_id, organization_id=organization_id),
            original_filename=filename,
            suffix_hint=suffix, object_key=object_key, sha256=digest, size_bytes=counted)
        db.add(row)
        await db.flush()
        source_payload = GeometrySourceRef.from_row(row).to_payload()
        interpretation_payload = None
        interpretation_id = None
        # The unit the file credibly declares, recorded with the source exactly as the multipart
        # upload records it; unresolved leaves the session to propose and confirm one.
        if evidence is not None and evidence.resolved and evidence.unit is not None:
            from meshpipeline.contracts.geometry_units import ResolutionBasis
            from meshpipeline.persistence.repositories.geometry_interpretation_repository import (
                GeometryInterpretationRepository,
            )
            recorded = await GeometryInterpretationRepository().record(
                db, owner_id=owner_id, geometry_source_id=source_id, unit=evidence.unit,
                basis=ResolutionBasis.file_declared, evidence=evidence.detail,
                organization_id=organization_id)
            interpretation_id = _uuid.UUID(recorded.interpretation_id)
            interpretation_payload = _asdict(GeometryInterpretationRef.from_domain(recorded))
        await db.execute(
            _sa_update(ChatSession).where(ChatSession.id == session_id)
            .values(geometry_source_id=source_id, geometry_interpretation_id=interpretation_id))
        await repo.resolve(db, object_key=object_key, state=ReconciliationState.resolved_adopted,
                           detail="the geometry source row owns this object")
        await db.commit()
    return session_id, source_payload, interpretation_payload


async def _replay(source_id, filename: str, owner_id: str,
                  organization_id: str) -> _multipart.StepFileOut:
    """A finish that arrives after the upload was already finished - a retry after a lost reply -
    answers with the session the first finish opened, and changes nothing."""
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    from meshpipeline.persistence.session import get_db

    try:
        async with get_db() as db:
            session = await SessionRepository().get_for_owner_by_geometry_source(
                db, source_id, owner_id, organization_id=organization_id)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="The upload could not be looked up - "
                                                    "finish it again in a moment.") from exc
    if session is None:
        raise HTTPException(status_code=409, detail="This upload was already finished.")
    greeting = _multipart.UPLOAD_ACKNOWLEDGEMENT if icfg.INTAKE_GREETING_ON_UPLOAD else ""
    return _multipart.StepFileOut(session_id=str(session.id), step_filename=filename,
                                  intake_greeting=greeting, trace=[])


async def _discard(store, scope: _Scope, detail: str) -> None:
    """Remove a refused object - this exact key, and only while its intent is still pending.

    The lock is what makes the delete safe: an object whose intent another finish already adopted
    belongs to a session now and is never touched here. When the database cannot be reached the
    object is left alone; its intent is pending and the sweep settles it after the hold."""
    from meshpipeline.persistence.models import ReconciliationState
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    repo = SourceCleanupRepository()
    try:
        async with get_db() as db:
            intent = await repo.lock_upload_for_owner(db, **scope)
            if intent is None or intent.state is not ReconciliationState.pending:
                return
            try:
                await asyncio.to_thread(store.delete_object, object_key=scope["object_key"])
            except Exception as exc:  # noqa: BLE001 - the intent keeps it discoverable
                logger.warning("finalize_direct_upload: could not delete refused object %s (the "
                               "intent stays pending for maintenance): %s", scope["source_id"], exc)
                return
            await repo.resolve(db, object_key=scope["object_key"],
                               state=ReconciliationState.resolved_deleted, detail=detail)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - never mask the refusal the caller is about to give
        logger.warning("finalize_direct_upload: could not settle refused upload %s (it stays "
                       "pending for maintenance): %s", scope["source_id"], exc)


async def _close_intent(object_key: str, state, detail: str) -> None:
    from meshpipeline.persistence.repositories.source_cleanup_repository import (
        SourceCleanupRepository,
    )
    from meshpipeline.persistence.session import get_db

    try:
        async with get_db() as db:
            await SourceCleanupRepository().resolve(db, object_key=object_key, state=state,
                                                    detail=detail)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - the sweep reaches the same end once the hold passes
        logger.warning("begin_direct_upload: could not close the intent for %s: %s", object_key, exc)
