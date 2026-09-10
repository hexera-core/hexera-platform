# Responsibility: Verify the chat turn carries the caller's ORGANISATION to every write it can reach.
# Boundaries: the threading only - what the repositories then do with it is tenant_scope's contract.
#
# WHY THIS FILE EXISTS. Reads scope on organization_id and fall back to owner_id; writes stamp
# both. A write that forgets the organisation therefore produces a row that no later read can see,
# because `NULL = <uuid>` is NULL rather than false - and nothing re-stamps it afterwards. Two
# writes on this path had exactly that defect: the job created by a chat approval, and the
# geometry interpretation recorded when the user names a unit in conversation.
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.message as msg
import meshpipeline.agents.intake.unit_clarification as uc
import meshpipeline.api.v1.chat as chat_mod
from meshpipeline.api.security import org_dep, owner_dep
from meshpipeline.api.v1.chat import router as chat_router

SID = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")
OWNER = "person@example.com"
ORG = "0e6a2a2a-1111-4111-a111-0e6a2a2a1111"


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1/chat")
    app.dependency_overrides[owner_dep] = lambda: OWNER
    app.dependency_overrides[org_dep] = lambda: ORG
    return app


def _session(**over):
    base = {"id": SID, "owner_id": OWNER, "job_id": None,
            "messages": [{"role": "user", "content": "hi"}],
            "request_txt": "", "review_brief_txt": "", "intake_patches": [],
            "dimensionality": "", "purpose": "", "input_kind": "", "mesh_engine": "",
            "domain": "", "engine_params": {}, "requested_mesh_fidelity": None,
            "geometry_source_id": None, "geometry_source": None,
            "geometry_interpretation_id": None, "llm_metadata": [], "intake_gate": {}}
    base.update(over)
    return SimpleNamespace(**base)


@asynccontextmanager
async def _db():
    yield AsyncMock()


async def test_the_inbound_message_carries_the_callers_organisation():
    seen: dict = {}

    async def _accept(inbound, **kw):
        seen["inbound"] = inbound
        return msg.MessageOutcome(status=msg.MessageStatus.not_found)

    with patch.object(msg, "accept", _accept):
        async with AsyncClient(transport=ASGITransport(app=_app()),
                               base_url="http://test") as c:
            await c.post("/api/v1/chat/message", json={"session_id": str(SID), "content": "hi"})

    assert seen["inbound"].organization_id == ORG, (
        "the route did not carry the caller's organisation into the turn, so every write the "
        "turn makes would be stamped with the owner alone")


async def test_an_approval_reaches_the_authority_with_the_organisation():
    from meshpipeline.api.schemas.chat import ChatResponse

    seen: dict = {}
    repo = MagicMock()
    repo.get_for_owner = AsyncMock(return_value=_session())

    async def _accept(inbound, **kw):
        return msg.MessageOutcome(status=msg.MessageStatus.approve, session=_session())

    async def _confirm(session, session_repo, owner_id, session_id, *, logger,
                       organization_id="", **kw):
        seen["organization_id"] = organization_id
        return ap.ConfirmOutcome(ap.ConfirmStatus.dispatched, "queued", job_id=uuid.uuid4())

    with patch.object(msg, "accept", _accept), \
         patch.object(ap, "confirm_pending_approval", _confirm), \
         patch("meshpipeline.persistence.repositories.session_repository.SessionRepository",
               return_value=repo):
        async with AsyncClient(transport=ASGITransport(app=_app()),
                               base_url="http://test") as c:
            resp = await c.post("/api/v1/chat/message",
                                json={"session_id": str(SID), "content": "yes, proceed"})

    assert resp.status_code == 200, resp.text
    assert isinstance(ChatResponse(session_id=SID, reply="x"), ChatResponse)
    assert seen["organization_id"] == ORG, (
        "the approval authority was not told which organisation the run belongs to, so the job "
        "it creates is stamped owner_id-only and no org-scoped read can ever find it")


async def test_a_unit_confirmed_in_conversation_is_recorded_for_the_organisation():
    # The unit answer is the ONE geometry write the conversation makes. api/v1/upload.py stamps
    # the file-declared interpretation with the organisation; this path must stamp the
    # user-confirmed one the same way, or the dispute route's org-scoped read of it returns None.
    seen: dict = {}
    source_id = uuid.uuid4()

    async def _record(db, *, owner_id, geometry_source_id, unit, organization_id=""):
        seen["owner_id"] = owner_id
        seen["organization_id"] = organization_id
        return SimpleNamespace(interpretation_id=str(uuid.uuid4()))

    locked = _session(geometry_source_id=source_id,
                      intake_gate={"unit_question": {"asked": True}})
    repo = MagicMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_message = AsyncMock()
    repo.bind_geometry_interpretation = AsyncMock()

    with patch.object(uc, "record", _record):
        await msg._settle_unit(
            msg.InboundMessage(session_id=SID, owner_id=OWNER, content="millimetres",
                               organization_id=ORG),
            AsyncMock(), gate=dict(locked.intake_gate), locked=locked, revision="r1",
            session_repo=repo, logger=MagicMock())

    assert seen["owner_id"] == OWNER
    assert seen["organization_id"] == ORG, (
        "the interpretation recorded from the conversation was not stamped with the "
        "organisation")


async def test_an_owner_with_no_organisation_still_records_the_unit():
    # The fallback, unchanged: an empty organisation stamps owner_id alone rather than refusing.
    seen: dict = {}

    async def _record(db, *, owner_id, geometry_source_id, unit, organization_id=""):
        seen["organization_id"] = organization_id
        return SimpleNamespace(interpretation_id=str(uuid.uuid4()))

    locked = _session(geometry_source_id=uuid.uuid4(),
                      intake_gate={"unit_question": {"asked": True}})
    repo = MagicMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_message = AsyncMock()
    repo.bind_geometry_interpretation = AsyncMock()

    with patch.object(uc, "record", _record):
        outcome = await msg._settle_unit(
            msg.InboundMessage(session_id=SID, owner_id=OWNER, content="millimetres"),
            AsyncMock(), gate=dict(locked.intake_gate), locked=locked, revision="r1",
            session_repo=repo, logger=MagicMock())

    assert outcome.status is msg.MessageStatus.unit_recorded
    assert seen["organization_id"] == ""
