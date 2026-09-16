# Responsibility: Serve the intake conversation over HTTP.
# Boundaries: transport for the intake application seam; it holds no conversation logic.
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import message as msg
from meshpipeline.agents.intake.agent import _extract_reply
from meshpipeline.api import pagination
from meshpipeline.api.schemas import listing
from meshpipeline.api.schemas.chat import ChatMessageIn, ChatResponse
from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.application.intake_brief import build_brief
from meshpipeline.application.job_service import JobService
from meshpipeline.errors import classify_api_failure, user_message_for
from meshpipeline.persistence.session import get_db
from meshpipeline.trace.sink import project_all

logger = logging.getLogger(__name__)
router = APIRouter()
svc = JobService()


@router.get("")
async def list_sessions(limit: int = pagination.DEFAULT_LIMIT, cursor: str | None = None,
                        owner_id: str = Depends(owner_dep),
                        organization_id: str = Depends(org_dep)) -> dict:
    # DECLARED BEFORE `/history/{session_id}`. FastAPI matches in declaration order, so a
    # collection route placed after that one would never be reached.
    bounded = pagination.clamp_limit(limit)
    async with get_db() as db:
        # ONE MORE ROW THAN THE PAGE - see listing.look_ahead.
        rows = await svc.list_conversations(db, owner_id, organization_id=organization_id,
                                            limit=listing.look_ahead(bounded),
                                            before=pagination.decode_cursor(cursor))
    return listing.page(
        rows, limit=bounded,
        item=lambda row: {
        "id": str(row.id),
        # The JSON key is "task_label" though the column is `ChatSession.domain`: `domain` is
        # documented on the model as "DESCRIPTIVE task label from intake ('elbow internal
        # flow')" - it IS the task label, under the name the schema actually gives it. Task 1's
        # `job_repository.list_for_owner` reads the same column for the same reason.
        "task_label": row.domain,
        "job_id": str(row.job_id) if row.job_id else None,
        "message_count": len(row.messages or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        },
        key=lambda row: (row.created_at, row.id))


@router.get("/history/{session_id}")
async def get_chat_history(session_id: uuid.UUID, owner_id: str = Depends(owner_dep),
                           organization_id: str = Depends(org_dep)):
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    # CONSTRUCTED HERE, not held at module scope. The import is function-local because
    # test_no_api_module_imports_a_persistence_repository_at_module_scope forbids the other
    # shape: an API module is transport over an application service, and the only repository a
    # route may touch is one it reaches for inside the request it is serving.
    session_repo = SessionRepository()
    async with get_db() as db:
        session = await session_repo.get_for_owner(db, session_id, owner_id,
                                                   organization_id=organization_id)
        if not session:
            raise HTTPException(404, "Session not found")
    return {
        "session_id": str(session_id),
        "messages": session.messages or [],
        # Awaiting confirmation only when intake is complete AND no job has
        # been dispatched yet (request_txt is cleared on dispatch, but be explicit).
        "awaiting_confirmation": bool(session.request_txt) and session.job_id is None,
        "job_id": str(session.job_id) if session.job_id else None,
    }



def _require_session(session, session_id):
    if session is None:
        raise HTTPException(
            500, "The chat session became unavailable while handling this message.")
    return session


@router.post("/message", response_model=ChatResponse,
             description="Hand the message to the intake authority and render what "
                         "it decided; this function owns no gate.")
async def chat_message(body: ChatMessageIn, owner_id: str = Depends(owner_dep),
                       organization_id: str = Depends(org_dep)):
    from meshpipeline.agents.intake.agent import node_intake
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    session_repo = SessionRepository()
    inbound = msg.InboundMessage(session_id=body.session_id, owner_id=owner_id,
                                 content=body.content, organization_id=organization_id)

    outcome = await msg.accept(inbound, session_repo=session_repo, db_factory=get_db,
                               logger=logger)

    if outcome.status is msg.MessageStatus.not_found:
        raise HTTPException(404, "Session not found")
    if outcome.status is msg.MessageStatus.approve:
        return await _confirm_pending_approval(outcome.session, session_repo, owner_id,
                                               body.session_id,
                                               organization_id=organization_id)
    if outcome.status is msg.MessageStatus.already_dispatched:
        return ChatResponse(session_id=body.session_id, reply=outcome.reply, done=True,
                            job_id=outcome.job_id)
    if outcome.answered:
        # A turn the authority settled with no model call: the deferral question, or the units
        # question. The reply is the authority's; the route only carries it.
        return ChatResponse(session_id=body.session_id, reply=outcome.reply,
                            awaiting_confirmation=outcome.awaiting_confirmation)

    session = _require_session(outcome.session, body.session_id)
    state = _build_intake_state(session, owner_id)
    # The turn after submit_requirements: the user is answering "shall I proceed?".
    state["awaiting_confirmation"] = bool(session.request_txt)
    result = await node_intake(state)

    await msg.persist_turn(inbound, result, session_repo=session_repo, db_factory=get_db)

    # A PROVIDER FAILURE IS A SYSTEM FAILURE, NOT A TURN. The authority classified it and returned
    # a marker with no assistant message, so composing a ChatResponse from it answered 200 with an
    # empty reply and the page rendered a blank bubble. errors.py owns both the class and the
    # blameless wording; the route only chooses the transport, exactly as _APPROVAL_STATUS below
    # does for the approval outcomes.
    if result.get("api_failure"):
        raise HTTPException(
            503, user_message_for(classify_api_failure(result["api_failure"])))

    # request_txt on the session (not just this turn's result) - a confirmation turn that
    # asks a clarifying question submits nothing new, yet is still awaiting confirmation.
    #
    # NOTE: the turn above already ran through msg.accept, whose own session lookup
    # (agents/intake/message.py) is still owner_id-only - see this task's report. Scoping this
    # re-read on the organisation cannot widen who reaches this line; it only keeps this read
    # consistent with the same rule everywhere else.
    async with get_db() as db:
        _sess = await session_repo.get_for_owner(db, body.session_id, owner_id,
                                                  organization_id=organization_id)
    return ChatResponse(
        session_id=body.session_id,
        reply=_extract_reply(result),
        awaiting_confirmation=bool(_sess.request_txt) and _sess.job_id is None,
        brief=build_brief(_sess),
        # re-projected for the mode running NOW: a session written under raw must
        # not be served raw by a server that has since restarted safe
        trace=project_all(result.get("_public_trace", [])),
    )


#: Typed approval outcome -> HTTP status. The route owns this table and nothing else about
#: approval: every status below is decided by agents/intake/approval, which knows no HTTP.
_APPROVAL_STATUS: dict[ap.ConfirmStatus, int] = {
    ap.ConfirmStatus.no_geometry:       400,
    ap.ConfirmStatus.no_confirmed_unit: 400,
    ap.ConfirmStatus.source_expired:    410,   # retention purge - the bytes are gone
    ap.ConfirmStatus.quota_exceeded:    429,
    ap.ConfirmStatus.inconsistent:      500,
    ap.ConfirmStatus.dispatch_failed:   500,
}


async def _confirm_pending_approval(session, session_repo, owner_id: str,
                                    session_id: uuid.UUID,
                                    organization_id: str = "") -> ChatResponse:
    cause: Exception | None = None
    try:
        outcome = await ap.confirm_pending_approval(
            session, session_repo, owner_id, session_id, logger=logger,
            organization_id=organization_id)
    except ap.ApprovalTransactionError as exc:
        outcome, cause = exc.outcome, exc

    status = _APPROVAL_STATUS.get(outcome.status)
    if status is not None:
        raise HTTPException(status_code=status, detail=outcome.message) from cause
    if outcome.ok:
        # dispatched, or an idempotent repeat of a run that already exists
        return ChatResponse(session_id=session_id, reply=outcome.message, done=True,
                            job_id=outcome.job_id)
    # A refusal the user can act on is a normal conversational reply, not an HTTP error.
    return ChatResponse(session_id=session_id, reply=outcome.message,
                        awaiting_confirmation=False)


def _session_geometry_state(session):
    row = getattr(session, "geometry_source", None)
    if row is None:
        return None
    from meshpipeline.contracts.geometry_source import GeometrySourceRef
    return {"ref": GeometrySourceRef.from_row(row).to_payload()}


def _build_intake_state(session, owner_id: str) -> dict:
    from meshpipeline.pipeline.state_factory import make_pipeline_state
    return make_pipeline_state(
        job_id=str(session.id),
        user_id=owner_id,
        session_id=str(session.id),
        messages=session.messages or [],
        geometry=_session_geometry_state(session),
        request_txt=session.request_txt or "",
        review_brief_txt=session.review_brief_txt or "",
        domain=session.domain or "",
        engine=session.mesh_engine or "",
        engine_params=session.engine_params or {},
        intake_gate=getattr(session, "intake_gate", None) or {},
        intake_patches=session.intake_patches or [],
        dimensionality=session.dimensionality or "",
        purpose=session.purpose or "",
        input_kind=session.input_kind or "",
        requested_mesh_fidelity=getattr(session, "requested_mesh_fidelity", None) or None,
    )




