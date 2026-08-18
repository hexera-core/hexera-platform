# Responsibility: Verify the chat route delegates the turn, writes it in one transaction, and publishes nothing itself.
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import meshpipeline.agents.intake.message as msg
import meshpipeline.api.v1.chat as chat_mod
from meshpipeline.adapters.model_inference.failure_markers import marker_for
from meshpipeline.api.v1.chat import router as chat_router
from meshpipeline.contracts.model_routing import FailureCategory
from meshpipeline.errors import classify_api_failure, user_message_for

_app = FastAPI()
_app.include_router(chat_router, prefix="/api/v1/chat")
SID = uuid.UUID("bbbbcccc-2222-4222-b222-bbbbbbbbbbbb")


@asynccontextmanager
async def _db():
    yield AsyncMock()


def _session(**over):
    base = {"id": SID, "owner_id": "alice", "job_id": None,
            "messages": [{"role": "user", "content": "hi"}],
            "request_txt": "", "review_brief_txt": "", "intake_patches": [],
            "dimensionality": "", "purpose": "", "input_kind": "", "mesh_engine": "",
            "domain": "", "engine_params": {}, "requested_mesh_fidelity": None,
            "geometry_source_id": None, "geometry_source": None,
            "geometry_interpretation_id": None, "llm_metadata": [], "intake_gate": {}}
    base.update(over)
    return SimpleNamespace(**base)


async def _post(outcome, *, session=None, intake_result=None, confirm=None):
    repo = MagicMock()
    repo.get_for_owner = AsyncMock(return_value=session or _session())
    seen: dict = {}

    async def _accept(inbound, **kw):
        seen["inbound"] = inbound
        return outcome

    async def _persist(inbound, result, **kw):
        seen["persisted"] = result

    async def _intake(_state):
        seen["intake_calls"] = seen.get("intake_calls", 0) + 1
        return intake_result or {"messages": [{"role": "assistant", "content": "go on"}],
                                 "intake_gate": {}}

    patches = [
        patch.object(chat_mod, "get_db", _db),
        patch("meshpipeline.persistence.repositories.session_repository.SessionRepository",
              return_value=repo),
        patch.object(msg, "accept", _accept),
        patch.object(msg, "persist_turn", _persist),
        patch("meshpipeline.agents.intake.agent.node_intake", _intake),
    ]
    if confirm is not None:
        patches.append(patch.object(chat_mod, "_confirm_pending_approval", confirm))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        if confirm is not None:
            with patches[5]:
                async with AsyncClient(transport=ASGITransport(app=_app),
                                       base_url="http://test") as c:
                    resp = await c.post("/api/v1/chat/message",
                                        json={"session_id": str(SID), "content": "hello"})
            return resp, seen
        async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as c:
            resp = await c.post("/api/v1/chat/message",
                                json={"session_id": str(SID), "content": "hello"})
    return resp, seen


# the responses

async def test_an_ordinary_message_runs_the_turn_and_returns_the_reply():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    resp, seen = await _post(outcome)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "go on"
    assert body["awaiting_confirmation"] is False
    assert seen["intake_calls"] == 1
    assert seen["persisted"] is not None, "the turn's result was never handed to the authority"


async def test_the_authority_receives_the_authenticated_owner_and_the_exact_content():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    _, seen = await _post(outcome)
    inbound = seen["inbound"]
    assert isinstance(inbound, msg.InboundMessage)
    assert inbound.session_id == SID and inbound.content == "hello"
    assert inbound.owner_id, "the route did not pass the authenticated caller"


async def test_an_unknown_session_is_404():
    resp, seen = await _post(msg.MessageOutcome(status=msg.MessageStatus.not_found))
    assert resp.status_code == 404
    assert "intake_calls" not in seen, "the model ran for a session that does not exist"


async def test_a_deferred_approval_returns_the_authoritys_question_awaiting_confirmation():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.approval_deferred,
                                 reply="Shall I go ahead?", awaiting_confirmation=True)
    resp, seen = await _post(outcome)
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == "Shall I go ahead?"
    assert body["awaiting_confirmation"] is True
    assert body["done"] is False
    assert "intake_calls" not in seen, "a deferral spent a model call"


async def test_a_unit_question_returns_awaiting_confirmation_and_runs_no_model():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.unit_question,
                                 reply="What unit is this file in?", awaiting_confirmation=True)
    resp, seen = await _post(outcome)
    assert resp.json()["reply"] == "What unit is this file in?"
    assert resp.json()["awaiting_confirmation"] is True
    assert "intake_calls" not in seen


async def test_an_already_dispatched_session_reports_its_job_and_is_done():
    job = uuid.uuid4()
    outcome = msg.MessageOutcome(status=msg.MessageStatus.already_dispatched,
                                 reply=f"Mesh generation is already running for this session. "
                                       f"Job ID: {job}", job_id=job)
    resp, seen = await _post(outcome)
    body = resp.json()
    assert body["done"] is True
    assert body["job_id"] == str(job)
    assert str(job) in body["reply"]
    assert "intake_calls" not in seen


async def test_an_approval_is_handed_to_the_approval_authority_with_zero_model_calls():
    from meshpipeline.api.schemas.chat import ChatResponse

    called: dict = {}

    async def _confirm(session, repo, owner_id, session_id):
        called["session"] = session
        return ChatResponse(session_id=session_id, reply="Starting mesh generation.",
                            done=True, job_id=uuid.uuid4())

    session = _session()
    outcome = msg.MessageOutcome(status=msg.MessageStatus.approve, session=session)
    resp, seen = await _post(outcome, confirm=_confirm)
    assert resp.status_code == 200 and resp.json()["done"] is True
    assert called["session"] is session, "the route did not pass the authority's own session"
    assert "intake_calls" not in seen, "the model was called on a confirmation turn"


async def test_a_message_that_invalidated_an_approval_still_runs_the_turn():
    outcome = msg.MessageOutcome(
        status=msg.MessageStatus.approval_invalidated, session=_session(),
        transition=msg.GateTransition(change=msg.GateChange.approval_invalidated,
                                      revision="r2"))
    resp, seen = await _post(outcome)
    assert resp.status_code == 200
    assert seen["intake_calls"] == 1, "an invalidating message never reached intake"


async def test_a_recorded_unit_continues_to_the_turn():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.unit_recorded, session=_session())
    resp, seen = await _post(outcome)
    assert resp.status_code == 200 and seen["intake_calls"] == 1


async def test_awaiting_confirmation_follows_the_session_not_the_turn():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    resp, _ = await _post(outcome, session=_session(request_txt="a complete summary"))
    assert resp.json()["awaiting_confirmation"] is True


async def test_a_dispatched_session_is_not_awaiting_confirmation():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    resp, _ = await _post(outcome,
                          session=_session(request_txt="summary", job_id=uuid.uuid4()))
    assert resp.json()["awaiting_confirmation"] is False


#: Every marker the INTAKE role can produce. Derived from the production vocabulary rather than
#: written out, so this covers exactly the classifications the role emits and cannot drift from
#: them: each FailureCategory maps through failure_markers onto one of these.
INTAKE_FAILURE_MARKERS = sorted({marker_for("intake", c) for c in FailureCategory})


@pytest.mark.parametrize("marker", INTAKE_FAILURE_MARKERS)
async def test_a_classified_provider_failure_is_reported_not_answered_as_an_empty_success(marker):
    # The turn produced a classified failure and NO assistant message. Composing a ChatResponse
    # from that answered 200 with reply="" - the page rendered an empty bubble, stopped, and gave
    # the user nothing to act on while the failure sat fully classified in the log.
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    resp, _ = await _post(outcome, intake_result={"api_failure": marker})

    assert resp.status_code == 503, (
        f"a classified provider failure answered {resp.status_code}: a system failure is not a "
        "conversational turn, and 200 with an empty reply is what left the page blank")
    detail = resp.json()["detail"]
    assert detail == user_message_for(classify_api_failure(marker)), (
        "the route composed its own wording instead of using the failure authority in errors.py")
    assert detail.strip(), "the caller was told nothing"
    assert marker not in resp.text, "the internal failure marker reached the client"


# what must not leak

#: Every status whose response the route composes itself. The leak check runs over ALL of them:
#: scoping it to one branch is how mutation E11 slipped past - it leaked from the settled-reply
#: branch, which the single-status version never exercised.
LEAKY_STATUSES = [
    (msg.MessageStatus.approval_invalidated, msg.GateChange.approval_invalidated),
    (msg.MessageStatus.approval_deferred, msg.GateChange.approval_deferred),
    (msg.MessageStatus.unit_question, msg.GateChange.unit_asked),
    (msg.MessageStatus.unit_recorded, msg.GateChange.unit_answered),
    (msg.MessageStatus.proceed, msg.GateChange.none),
]


@pytest.mark.parametrize("status,change", LEAKY_STATUSES, ids=lambda x: getattr(x, "value", x))
async def test_no_internal_identifier_or_storage_detail_reaches_the_client(status, change):
    outcome = msg.MessageOutcome(
        status=status, session=_session(), reply="a plain reply",
        awaiting_confirmation=True,
        transition=msg.GateTransition(change=change,
                                      revision="deadbeefcafe0000",
                                      approval_status="invalidated",
                                      reason="user replied with a correction"))
    resp, _ = await _post(outcome)
    blob = resp.text
    for leaked in ("deadbeefcafe0000", "intake_gate", "get_for_update",
                   "expected_confirmation_msg_count", "user replied with a correction",
                   "s3://", "minio", "geometry_source_id"):
        assert leaked not in blob, (
            f"the {status.value} response leaks an internal value: {leaked}")


async def test_the_response_body_shape_is_unchanged():
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    resp, _ = await _post(outcome)
    from meshpipeline.api.schemas.chat import ChatResponse
    assert set(resp.json()) == set(ChatResponse.model_fields), (
        "the wire shape drifted from the declared response schema")


# the events channel

async def test_every_event_channel_is_written_by_the_authority_after_the_turn():
    result = {
        "messages": [{"role": "assistant", "content": "ok"}],
        "intake_gate": {"selection": {"id": "s1"}},
        "_intake_training_event": {"type": "intake_turn", "payload": {"turn": 1}},
        "_intake_agent_run_event": {"type": "agent_run", "payload": {"role": "intake"}},
        "_public_trace": [{"type": "reasoning", "payload": {}}],
        "_intake_search_events": [{"type": "search", "payload": {"q": "x"}}],
    }
    outcome = msg.MessageOutcome(status=msg.MessageStatus.proceed, session=_session())
    _, seen = await _post(outcome, intake_result=result)
    persisted = seen["persisted"]
    for channel in ("_intake_training_event", "_intake_agent_run_event", "_public_trace",
                    "_intake_search_events", "intake_gate"):
        assert channel in persisted, f"{channel} never reached the authority"


async def test_the_route_publishes_nothing_itself():
    import inspect

    src = inspect.getsource(chat_mod.chat_message)
    for publisher in ("append_intake_event", "publish", "emit", "set_intake_gate"):
        assert publisher not in src, f"the route publishes/persists directly: {publisher}"


# the authority's own writes

@pytest.mark.parametrize("key,setter", list(msg._REQUIREMENT_WRITES))
async def test_every_requirement_the_turn_produces_has_a_durable_writer(key, setter):
    repo = MagicMock()
    setattr(repo, setter, AsyncMock())
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(repo, _s, AsyncMock())
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_intake_event = AsyncMock()

    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="hi")
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": "r"}],
                                     key: "a-value"},
                           session_repo=repo, db_factory=_db)
    getattr(repo, setter).assert_awaited_once()


async def test_a_turn_that_says_nothing_about_a_field_never_blanks_it():
    repo = MagicMock()
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(repo, _s, AsyncMock())
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_intake_event = AsyncMock()

    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="hi")
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": "r"}],
                                     "mesh_engine": "", "purpose": None},
                           session_repo=repo, db_factory=_db)
    repo.set_mesh_engine.assert_not_awaited()
    repo.set_purpose.assert_not_awaited()


async def test_a_failed_turn_stores_no_assistant_message_while_a_successful_one_still_does():
    # A failed turn said nothing, so it must leave no assistant message. Appending one stored a
    # blank turn: the history read served it straight back, and the next attempt handed it to the
    # model as conversation. The user's own message is not this function's to write - `accept`
    # committed it before the turn ran, deliberately, because they said it.
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="hi")

    failed = MagicMock()
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(failed, _s, AsyncMock())
    failed.append_message = AsyncMock()
    failed.set_intake_gate = AsyncMock()
    failed.append_intake_event = AsyncMock()
    await msg.persist_turn(
        inbound, {"api_failure": marker_for("intake", FailureCategory.AUTH)},
        session_repo=failed, db_factory=_db)
    failed.append_message.assert_not_awaited()

    ok = MagicMock()
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(ok, _s, AsyncMock())
    ok.append_message = AsyncMock()
    ok.set_intake_gate = AsyncMock()
    ok.append_intake_event = AsyncMock()
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": "go on"}]},
                           session_repo=ok, db_factory=_db)
    ok.append_message.assert_awaited_once()
    assert ok.append_message.await_args.args[-1] == "go on", (
        "the successful turn no longer stores what the assistant actually said")


async def test_the_turns_own_gate_is_stored_by_the_authority():
    repo = MagicMock()
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(repo, _s, AsyncMock())
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_intake_event = AsyncMock()

    gate = {"selection": {"id": "sel-1"}, "admission": {"token": "t"}}
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="hi")
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": "r"}],
                                     "intake_gate": gate},
                           session_repo=repo, db_factory=_db)
    repo.set_intake_gate.assert_awaited_once()
    assert repo.set_intake_gate.await_args.args[-1] == gate


async def test_a_turn_that_clears_the_gate_still_writes_the_clear():
    repo = MagicMock()
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(repo, _s, AsyncMock())
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_intake_event = AsyncMock()

    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="hi")
    await msg.persist_turn(inbound, {"messages": [{"role": "assistant", "content": "r"}],
                                     "intake_gate": None},
                           session_repo=repo, db_factory=_db)
    repo.set_intake_gate.assert_awaited_once()
    assert repo.set_intake_gate.await_args.args[-1] is None


async def test_the_whole_turn_is_written_in_exactly_one_transaction():
    commits = {"n": 0}

    class _DB:
        async def commit(self):
            commits["n"] += 1

    @asynccontextmanager
    async def _counting_db():
        yield _DB()

    repo = MagicMock()
    for _k, _s in msg._REQUIREMENT_WRITES:
        setattr(repo, _s, AsyncMock())
    repo.append_message = AsyncMock()
    repo.set_intake_gate = AsyncMock()
    repo.append_intake_event = AsyncMock()

    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content="hi")
    await msg.persist_turn(inbound, {
        "messages": [{"role": "assistant", "content": "r"}],
        "mesh_engine": "gmsh", "purpose": "internal_cfd",
        "intake_gate": {"selection": {"id": "s"}},
        "_intake_training_event": {"type": "intake_turn", "payload": {"turn": 1}},
        "_public_trace": [{"type": "reasoning", "payload": {}}],
    }, session_repo=repo, db_factory=_counting_db)

    assert commits["n"] == 1, (
        f"the turn was written in {commits['n']} transactions - part of it can become visible "
        "before the rest")
