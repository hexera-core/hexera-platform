# Responsibility: Serve the intake conversation over HTTP.
# Boundaries: transport for the intake application seam; it holds no conversation logic.
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile

from meshpipeline.agents.intake import approval as ap
from meshpipeline.agents.intake import message as msg
from meshpipeline.agents.intake.agent import _extract_reply
from meshpipeline.api import pagination
from meshpipeline.api.schemas import listing
from meshpipeline.api.schemas.chat import AttachmentOut, ChatMessageIn, ChatNoteIn, ChatResponse
from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.application.intake_brief import build_brief
from meshpipeline.application.job_service import JobService
from meshpipeline.errors import classify_api_failure, user_message_for
from meshpipeline.persistence.session import get_db
from meshpipeline.trace.sink import project_all

logger = logging.getLogger(__name__)
router = APIRouter()
svc = JobService()


async def _brief(session, owner_id: str):
    """The request card, with the parts checklist read off the event loop (store reads only: drawing the card never
    starts a reading - Finalize notes does)."""
    import asyncio

    from meshpipeline.application import part_matches as PM
    if session is None or not getattr(session, "request_txt", None):
        return build_brief(session)
    pm = await asyncio.to_thread(lambda: PM.view(session, owner_id=owner_id))
    brief = build_brief(session, part_matches=pm)
    # the approval question the console says again once the notes are finalized and their parts shown
    if brief is not None and getattr(session, "job_id", None) is None \
            and ap.is_live((getattr(session, "intake_gate", None) or {}).get("approval")):
        brief["proceed_ask"] = ap.PROCEED_ASK
    return brief


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
    from meshpipeline.agents.intake.measured_choice import choice_payload
    from meshpipeline.contracts.user_notes import for_browser, system_written
    return {
        "session_id": str(session_id),
        # a turn the APPLICATION sent in the user's name (the stage's Proceed handing back to the
        # intake) is never shown as the user's words: the "Confirmed" card already says it. An attached
        # file's read text stays on the server (the file opens from its own route).
        "messages": [for_browser(m) for m in session.messages or []
                     if not (isinstance(m, dict) and m.get("role") == "user"
                             and system_written(m.get("content")))],
        # the open engine question's choice, so a reload draws it again
        "engine_choice": choice_payload(getattr(session, "intake_gate", None)),
        # the buttons under the last reply when it is an application question, so a reload draws them
        "choices": _last_choices(session.messages),
        # THE PLAN CARD and the file, so a reload in the middle of the questionnaire draws what the page showed
        "brief": await _brief(session, owner_id) if session.job_id is None else None,
        "geometry_filename": str(getattr(getattr(session, "geometry_source", None), "original_filename", "") or ""),
        # Awaiting confirmation only when intake is complete AND no job has
        # been dispatched yet (request_txt is cleared on dispatch, but be explicit).
        "awaiting_confirmation": bool(session.request_txt) and session.job_id is None,
        "job_id": str(session.job_id) if session.job_id else None,
    }



def _last_choices(messages) -> list[dict]:
    """The buttons under the conversation's last message, when it is an application question."""
    from meshpipeline.application.purpose_map import choices_for

    last = list(messages or [])[-1:]
    if not last or not isinstance(last[0], dict) or last[0].get("role") != "assistant":
        return []
    return choices_for(str(last[0].get("content") or ""))


async def _picked_faces(session_id, faces, owner_id: str, organization_id) -> list[dict]:
    """NOTE-17: the faces picked with a message or note, re-read against the session's face map (application/
    face_pick.verify) - the map's own label and signature are what is kept. A face not on the map refuses the send
    in one plain line; it is never guessed. Only the owner's session is read."""
    if not faces:
        return []
    import asyncio

    from meshpipeline.application import face_pick
    from meshpipeline.persistence.repositories.session_repository import SessionRepository
    async with get_db() as db:
        owned = await SessionRepository().get_for_owner(db, session_id, owner_id, organization_id=organization_id)
    if owned is None:
        raise HTTPException(404, "Session not found")
    kept, dropped = await asyncio.to_thread(face_pick.verify, str(session_id), [f.model_dump() for f in faces])
    if dropped:
        raise HTTPException(422, face_pick.refused(dropped))
    return kept


#: how long the send waits for a requirement's reading before it answers without the reply (the reply then comes
#: with the card's poll of the reading, GET .../check/parts): with the model's TIMEOUT_S, no send is held past ~20 s
REQUIREMENT_READ_WAIT_S = 12.0
#: how often the send looks for the landed reading (a landed reading waited up to this long for nothing)
REQUIREMENT_POLL_S = 0.25


async def _requirement_reply(outcome, session_repo, owner_id: str, session_id, organization_id) -> ChatResponse:
    """NOTE-18: a requirement typed at the plan step - kept as a note by the authority - is read for the checklist
    now (what Finalize notes did), and answered with what the checklist took from it - written by the intake's model
    from those facts under a guard (agents/intake/plan_reply, NOTE-19) - kept in the conversation. A reading that
    takes longer than REQUIREMENT_READ_WAIT_S is not waited on: the send answers with `reply_later` and the reply comes
    with the card's poll. The plan stands: the answer carries the card as it now is."""
    import asyncio
    import time

    from meshpipeline.agents.intake import plan_reply as PR
    from meshpipeline.application import part_matches as PM

    async with get_db() as db:
        held = await session_repo.get_for_owner(db, session_id, owner_id, organization_id=organization_id)
    held = _require_session(held, session_id)
    from meshpipeline.application import meshing_standard_service as MSS
    from meshpipeline.application import plan_cost
    # NOTE-26: the reading costs what was typed with the team's standard the run will be dispatched with
    standard = await MSS.for_reading(get_db, organization_id=organization_id)
    view = await asyncio.to_thread(lambda: PM.finalize(held, owner_id=owner_id, **PM.with_standard(standard)))
    from meshpipeline.agents.intake import plan_talk as PT
    note = PR.owed_note(held)
    quick = PT.plain_face_rows(note) if note is not None else None
    sized: bool = bool(quick) and await asyncio.to_thread(plan_cost.stage_sized, str(session_id))
    if sized:
        # NOTE-26: an FEA solid the stage sized - what the picked face's size does to the mesh is costed with the
        # reading, so the reply waits for it like any other (the short way had no cost to say)
        quick = None
    if quick and view is not None and view.get("status") == "reading" and not view.get("failed"):
        # THE SHORT WAY: faces picked and one plain size - where and what are known now, so the reply is written at
        # once while the reading (started above) fills the card's list
        reply = await PR.answer_owed(held, {"status": "done", "rows": quick, "unsure": []}, owner_id=owner_id,
                                     session_repo=session_repo, db_factory=get_db, organization_id=organization_id)
        if reply:
            return ChatResponse(session_id=session_id, reply=reply, awaiting_confirmation=True,
                                brief=await _brief(held, owner_id))
    t0 = time.monotonic()
    while view and view.get("status") == "reading" and time.monotonic() - t0 < REQUIREMENT_READ_WAIT_S:
        await asyncio.sleep(REQUIREMENT_POLL_S)
        view = await asyncio.to_thread(lambda: PM.view(held, owner_id=owner_id))
    if view and view.get("status") == "reading":
        return ChatResponse(session_id=session_id, reply="", reply_later=True, awaiting_confirmation=True,
                            brief=await _brief(held, owner_id))
    reply = await PR.answer_owed(held, view, owner_id=owner_id, session_repo=session_repo, db_factory=get_db,
                                 organization_id=organization_id)
    return ChatResponse(session_id=session_id, reply=reply, reply_later=not reply, awaiting_confirmation=True,
                        brief=await _brief(held, owner_id))


async def _question_reply(session_repo, owner_id: str, session_id, organization_id, *, text: str = "",
                          faces=()) -> ChatResponse:
    """NOTE-20: a question about the mesh at the plan step - nothing kept - answered from the numbers the product
    already holds before the run: the check's planned sizes (application/fea_check.planned_sizes) and the checklist's
    rows (its reading started, and waited on as a requirement's is, so what the user just set is in it). The plan
    stands: the answer carries the card as it now is."""
    import asyncio
    import time

    from meshpipeline.agents.intake import plan_reply as PR
    from meshpipeline.application import fea_check
    from meshpipeline.application import part_matches as PM

    async with get_db() as db:
        held = await session_repo.get_for_owner(db, session_id, owner_id, organization_id=organization_id)
    held = _require_session(held, session_id)
    from meshpipeline.application import meshing_standard_service as MSS
    standard = await MSS.for_reading(get_db, organization_id=organization_id)
    view = await asyncio.to_thread(lambda: PM.finalize(held, owner_id=owner_id, **PM.with_standard(standard)))
    t0 = time.monotonic()
    while view and view.get("status") == "reading" and time.monotonic() - t0 < REQUIREMENT_READ_WAIT_S:
        await asyncio.sleep(REQUIREMENT_POLL_S)
        view = await asyncio.to_thread(lambda: PM.view(held, owner_id=owner_id))
    plan = await asyncio.to_thread(fea_check.planned_sizes, str(session_id))
    reply = await PR.answer_question(held, view, plan, owner_id=owner_id, session_repo=session_repo,
                                     db_factory=get_db, organization_id=organization_id, text=text, faces=faces)
    return ChatResponse(session_id=session_id, reply=reply, awaiting_confirmation=True,
                        brief=await _brief(held, owner_id))


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
    """One chat turn, start to finish: the message is accepted, the authority settles what it can
    without a model (the units question, a deferred approval, the geometry check's hold while
    the part is drawn), else the intake answers. The geometry confirm runs a turn through here
    too, calling it directly."""
    from meshpipeline.agents.intake.agent import node_intake
    from meshpipeline.agents.intake.measured_choice import choice_payload
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    session_repo = SessionRepository()
    session_id = body.session_id
    faces = await _picked_faces(session_id, getattr(body, "faces", None), owner_id, organization_id)
    inbound = msg.InboundMessage(session_id=session_id, owner_id=owner_id,
                                 content=body.content, organization_id=organization_id,
                                 attachments=tuple({"id": a} for a in getattr(body, "attachments", None) or ()),
                                 faces=tuple(faces))
    outcome = await msg.accept(inbound, session_repo=session_repo, db_factory=get_db,
                               logger=logger)

    if outcome.status is msg.MessageStatus.not_found:
        raise HTTPException(404, "Session not found")
    if outcome.status is msg.MessageStatus.approve:
        return await _confirm_pending_approval(outcome.session, session_repo, owner_id,
                                               session_id,
                                               organization_id=organization_id)
    if outcome.status is msg.MessageStatus.requirement:
        return await _requirement_reply(outcome, session_repo, owner_id, session_id, organization_id)
    if outcome.status is msg.MessageStatus.plan_question:
        return await _question_reply(session_repo, owner_id, session_id, organization_id, text=body.content,
                                     faces=faces)
    if outcome.status is msg.MessageStatus.already_dispatched:
        return ChatResponse(session_id=session_id, reply=outcome.reply, done=True,
                            job_id=outcome.job_id)
    if outcome.answered:
        # A turn the authority settled with no model call: the deferral question, or the units
        # question. The reply is the authority's; the route only carries it.
        from meshpipeline.application.purpose_map import choices_for
        brief, again = None, False
        if outcome.status is msg.MessageStatus.reading_first:
            # the yes waits for the notes: the card shows the reading, and the console sends the yes again
            async with get_db() as db:
                held = await session_repo.get_for_owner(db, session_id, owner_id, organization_id=organization_id)
            brief, again = await _brief(held, owner_id), True
        return ChatResponse(session_id=session_id, reply=outcome.reply,
                            awaiting_confirmation=outcome.awaiting_confirmation,
                            choices=choices_for(outcome.reply), brief=brief, yes_after_reading=again,
                            restage=outcome.restage, stage_flow=outcome.stage_flow)

    session = _require_session(outcome.session, session_id)
    previous_run = outcome.previous_run
    if previous_run is not None and previous_run.status == "succeeded":
        # what the run delivered, measured: the chat answers "how big is it?" from this
        import dataclasses

        from meshpipeline.application.run_facts import read_delivered_size
        previous_run = dataclasses.replace(
            previous_run, cells=await read_delivered_size(previous_run.job_id))
    state = _build_intake_state(session, owner_id, previous_run=previous_run)
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
        _sess = await session_repo.get_for_owner(db, session_id, owner_id,
                                                  organization_id=organization_id)
    from meshpipeline.application.purpose_map import choices_for
    _reply = _extract_reply(result)
    _then = str(result.get("then_ask") or "") if _reply.strip() else ""
    return ChatResponse(
        session_id=session_id,
        reply=_reply,
        # the buttons under the application's own question (a purpose question; never a cell count)
        choices=choices_for(_reply),
        # the conditions the plan assumed, asked under its card (CFDREQ-37)
        then={"reply": _then, "choices": choices_for(_then)} if _then else None,
        awaiting_confirmation=bool(_sess.request_txt) and _sess.job_id is None,
        brief=await _brief(_sess, owner_id),
        # re-projected for the mode running NOW: a session written under raw must
        # not be served raw by a server that has since restarted safe
        trace=project_all(result.get("_public_trace", [])),
        engine_choice=choice_payload(getattr(_sess, "intake_gate", None)),
    )


@router.post("/notes",
             description="Keep the user's own notes from the console's box, word for word, beside "
                         "the session's other notes; answers with the brief as it now stands.")
async def chat_notes(body: ChatNoteIn, owner_id: str = Depends(owner_dep),
                     organization_id: str = Depends(org_dep)) -> dict:
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    faces = await _picked_faces(body.session_id, body.faces, owner_id, organization_id)
    status, session = await msg.add_console_note(
        body.session_id, owner_id, body.text, session_repo=SessionRepository(),
        db_factory=get_db, organization_id=organization_id,
        attachments=[{"id": a} for a in body.attachments], faces=faces)
    if status is msg.NoteStatus.not_found:
        raise HTTPException(404, "Session not found")
    if status is msg.NoteStatus.empty:
        raise HTTPException(422, "The note is empty.")
    if status is msg.NoteStatus.running:
        raise HTTPException(409, "The run has already started, so a new note cannot reach it. "
                                 "Add it before you approve the next run.")
    # THE NOTES AS STORED, always: the brief exists only once requirements are submitted, so a note
    # added before that answered with no brief - and no notes - though it was kept (dry run, 2 Nov)
    from meshpipeline.contracts.user_notes import clean as _clean_notes
    from meshpipeline.contracts.user_notes import shown as _shown
    notes = [{"id": n["id"], "text": n["text"], "source": n["source"], **_shown(n)}
             for n in _clean_notes(getattr(session, "user_notes", None))]
    out = {"session_id": str(body.session_id), "brief": await _brief(session, owner_id), "user_notes": notes}
    # "use cfMesh" IN THE NOTES chooses the engine as it does in the chat: the console says it in the
    # chat for the user, and the intake takes it from there (engine_selection.switch_by_words)
    from meshpipeline.agents.intake.engine_selection import note_chooses_engine
    gate = getattr(session, "intake_gate", None) or {}
    current = str((gate.get("selection") or {}).get("engine") or getattr(session, "mesh_engine", "") or "")
    say = note_chooses_engine(body.text, current)
    if say:
        out["say"] = say
    return out


@router.post("/attachments", response_model=AttachmentOut,
             description="Keep one picture or text file the user attached in the chat or notes box (checked by its "
                         "bytes; pictures re-encoded, a PDF's text read); answers with the row a message or note "
                         "then carries by its id.")
async def chat_attachment(session_id: uuid.UUID = Form(...), file: UploadFile = File(...),
                          owner_id: str = Depends(owner_dep), organization_id: str = Depends(org_dep)):
    from meshpipeline.application import chat_attachments as ca
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    data = await file.read(ca.TEXT_MAX_BYTES + 1)
    try:
        return await ca.accept(session_id, owner_id, name=file.filename, data=data,
                               session_repo=SessionRepository(), db_factory=get_db,
                               organization_id=organization_id)
    except LookupError:
        raise HTTPException(404, "Session not found") from None
    except ca.Refused as refused:
        raise HTTPException(refused.status, refused.line) from None


@router.get("/attachments/{session_id}/{attachment_id}",
            description="One attached file as kept (a picture's re-encoding, a text file or PDF as sent), for its "
                        "owner only; never run, never sniffed.")
async def chat_attachment_file(session_id: uuid.UUID, attachment_id: str, owner_id: str = Depends(owner_dep),
                               organization_id: str = Depends(org_dep)) -> Response:
    from meshpipeline.application import chat_attachments as ca
    from meshpipeline.persistence.repositories.session_repository import SessionRepository

    got = await ca.open_for_owner(session_id, attachment_id, owner_id, session_repo=SessionRepository(),
                                  db_factory=get_db, organization_id=organization_id)
    if got is None:
        raise HTTPException(404, "Attachment not found")
    data, entry = got
    return Response(content=data, media_type=ca.served_type(entry), headers=ca.served_headers(entry))



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

    if outcome.status is ap.ConfirmStatus.dispatch_failed and outcome.job_id:
        # A RUN THAT DID NOT START (the job row exists, nothing runs): told now, in our words, with the plan offered
        # again so Proceed works - never a 500 the console shows as "Could not send that: ..."
        logger.error("approval: the run did not start - session=%s job=%s: %s", session_id, outcome.job_id, cause)
        reply = await msg.offer_again_after_failed_start(session_id, owner_id, session_repo=session_repo)
        return ChatResponse(session_id=session_id, reply=reply, awaiting_confirmation=True)
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


def _build_intake_state(session, owner_id: str, *, previous_run=None) -> dict:
    from meshpipeline.pipeline.state_factory import make_pipeline_state
    state = make_pipeline_state(
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
        # the user's own notes, word for word: the chat answers with what they asked for in them
        user_notes=list(getattr(session, "user_notes", None) or []),
    )
    if previous_run is not None:
        # An intake-only working key, like `awaiting_confirmation` above: this conversation
        # already ran once and that run is over, so the turn is the start of another run on the
        # same geometry. The authority decided that (agents/intake/message.py); the route only
        # carries it.
        state["previous_run"] = previous_run.as_state()
    return state




