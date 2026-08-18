# Responsibility: Verify a chat message and its gate are written under one row lock, so no race dispatches twice.
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests import harness_provisioning as hp

import meshpipeline.agents.intake.admission_token as at
import meshpipeline.agents.intake.approval as ap
import meshpipeline.agents.intake.engine_selection as es
import meshpipeline.agents.intake.message as msg
import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.models import ChatSession, SimulationJob
from meshpipeline.persistence.repositories.session_repository import SessionRepository

repo = SessionRepository()
OWNER = "owner-1"

_PATCHES = [{"name": "inlet", "type": "inlet"}, {"name": "outlet", "type": "outlet"},
            {"name": "wall", "type": "wall"}]
_PAYLOAD = {"domain": "duct", "request_txt": "r" * 120, "review_brief_txt": "b" * 90,
            "dimensionality": "3D", "purpose": "internal_cfd", "input_kind": "fluid-domain",
            "mesh_engine": "gmsh", "mesh_fidelity": "standard", "engine_source": "user_direct",
            "engine_params": {"element_order": "2"}, "patches": _PATCHES}


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


@pytest.fixture()
async def SessionLocal():
    connect_args = {"ssl": True} if provcfg.DB_SSL_REQUIRED else {}
    try:
        engine = create_async_engine(provcfg.POSTGRES_DSN, connect_args=connect_args, pool_size=8)
        await hp.reset_schema(provcfg.POSTGRES_DSN)
        await hp.truncate_tables(provcfg.POSTGRES_DSN, "chat_sessions", "simulation_jobs")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("PostgreSQL not reachable for the intake message concurrency test - it must "
                    f"be PROVISIONED, not skipped ({type(exc).__name__}: {exc})")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _factory(SessionLocal):
    class _Ctx:
        async def __aenter__(self):
            self._s = SessionLocal()
            return await self._s.__aenter__()

        async def __aexit__(self, *a):
            return await self._s.__aexit__(*a)
    return _Ctx


def _snapshot(session_id: str, selection: dict, msg_count: int) -> dict:
    canon = at.canonical_payload("gmsh", "internal_cfd", "fluid-domain", "3D", _PATCHES,
                                 {"element_order": "2"})
    intent = at.approved_intent_canonical(
        engine="gmsh", purpose="internal_cfd", input_kind="fluid-domain", dimensionality="3D",
        patches=_PATCHES, engine_params={"element_order": "2"},
        requested_mesh_fidelity="standard", request_txt=_PAYLOAD["request_txt"], source_ref=None)
    return ap.create(owner_id=OWNER, session_id=session_id, selection_id=selection["id"],
                     token_id="tok", canonical=canon, fingerprint=at.fingerprint(canon),
                     intent_canonical=intent, intent_fingerprint=at.fingerprint(intent),
                     payload=dict(_PAYLOAD), summary=at.CONFIRM_REQUIREMENTS_ASK,
                     proposal_revision="r1", proposal_msg_count=msg_count)


async def _seed(SessionLocal, *, with_approval: bool = True, messages=None):
    msgs = messages if messages is not None else [{"role": "user", "content": "hi"}]
    async with SessionLocal() as s:
        row = ChatSession(owner_id=OWNER, messages=msgs, llm_metadata=[],
                          mesh_engine="gmsh", purpose="internal_cfd",
                          input_kind="fluid-domain", dimensionality="3D",
                          request_txt=_PAYLOAD["request_txt"])
        s.add(row)
        await s.commit()
        sid = row.id
    sel = es.select_from_structured_input("gmsh", session_id=str(sid), owner_id=OWNER,
                                          revision="r1")
    gate = {"selection": sel, "admission": None}
    if with_approval:
        # proposal_msg_count is the count AT THE TIME THE SUMMARY WAS SHOWN; the snapshot is
        # answerable by exactly the next message (`create` adds the +1 itself).
        gate["approval"] = _snapshot(str(sid), sel, msg_count=len(
            [m for m in msgs if m.get("role") == "user"]))
    async with SessionLocal() as s:
        await repo.set_intake_gate(s, sid, gate)
        await s.commit()
    return sid, sel, gate.get("approval")


async def _state(SessionLocal, sid) -> dict:
    async with SessionLocal() as s:
        row = await repo.get_for_owner(s, sid, OWNER)
        jobs = await s.execute(text("SELECT count(*) FROM simulation_jobs WHERE owner_id = :o"),
                               {"o": OWNER})
    gate = dict(getattr(row, "intake_gate", None) or {})
    approval = gate.get("approval") or {}
    msgs = list(row.messages or [])
    return {
        "messages": len(msgs),
        "user_messages": len([m for m in msgs if m.get("role") == "user"]),
        "revision": at.revision_of(msgs),
        "approval_status": approval.get("status", ""),
        "approval_live": ap.is_live(approval or None),
        "fingerprint": approval.get("fingerprint", ""),
        "linked_job": row.job_id,
        "jobs": int(jobs.scalar_one()),
    }


async def _accept(SessionLocal, sid, content: str, *, owner: str = OWNER):
    return await msg.accept(
        msg.InboundMessage(session_id=sid, owner_id=owner, content=content),
        session_repo=repo, db_factory=_factory(SessionLocal), logger=_Log())


async def _dispatch(SessionLocal, sid) -> str:
    async with SessionLocal() as db:
        locked = await repo.get_for_update(db, sid)
        gate = dict(locked.intake_gate or {})
        approval, selection = gate.get("approval"), gate.get("selection")
        if (approval or {}).get("status") == ap.DISPATCHED:
            await db.commit()
            return "idempotent"
        umc = len([m for m in (locked.messages or []) if m.get("role") == "user"])
        ok, why = ap.verify(approval, owner_id=OWNER, session_id=str(sid),
                            selection=selection, user_msg_count=umc)
        if not ok:
            await db.commit()
            return f"refused: {why[:40]}"
        job = SimulationJob(owner_id=OWNER)
        db.add(job)
        await db.flush()
        await repo.link_job(db, sid, job.id)
        gate["approval"] = {**approval, "status": ap.DISPATCHED, "job_id": str(job.id)}
        await repo.set_intake_gate(db, sid, gate)
        await db.commit()
        return "dispatched"


# what a message does to the gate

async def test_1_an_ordinary_message_records_and_changes_no_gate(SessionLocal):
    sid, _, _ = await _seed(SessionLocal, with_approval=False)
    before = await _state(SessionLocal, sid)

    out = await _accept(SessionLocal, sid, "the inlet is 2 m/s")

    after = await _state(SessionLocal, sid)
    assert out.status is msg.MessageStatus.proceed
    assert out.transition.change is msg.GateChange.none
    assert after["user_messages"] == before["user_messages"] + 1
    assert after["revision"] != before["revision"], "the conversation revision did not advance"
    assert after["jobs"] == 0 and after["linked_job"] is None


async def test_2_a_requirement_changing_message_invalidates_the_approval_atomically(SessionLocal):
    sid, _, snap = await _seed(SessionLocal)
    assert ap.is_live(snap)

    out = await _accept(SessionLocal, sid, "no, change the engine to snappy")

    after = await _state(SessionLocal, sid)
    assert out.status is msg.MessageStatus.approval_invalidated
    assert out.transition.change is msg.GateChange.approval_invalidated
    assert after["approval_status"] == ap.INVALIDATED
    assert after["approval_live"] is False
    # THE point of the stage: the message and the invalidation are one durable fact.
    assert after["user_messages"] == 2, "the message was not recorded with its consequence"
    assert await _dispatch(SessionLocal, sid) != "dispatched", (
        "an approval the user argued with still dispatched")
    assert (await _state(SessionLocal, sid))["jobs"] == 0


async def test_3_an_approval_relevant_message_that_is_a_bare_yes_does_not_invalidate(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    out = await _accept(SessionLocal, sid, "yes, proceed")
    after = await _state(SessionLocal, sid)
    assert out.status is msg.MessageStatus.approve
    assert after["approval_status"] == ap.AWAITING, "a bare approval invalidated its own snapshot"
    assert after["approval_live"] is True


async def test_3b_a_message_with_no_live_approval_invalidates_nothing(SessionLocal):
    sid, _, _ = await _seed(SessionLocal, with_approval=False)
    out = await _accept(SessionLocal, sid, "no, change the engine to snappy")
    after = await _state(SessionLocal, sid)
    assert out.status is msg.MessageStatus.proceed
    assert after["approval_status"] == "", "an approval was invented for a session that had none"


# corrections and geometry

@pytest.mark.parametrize("correction", [
    "change the engine to snappy",             # engine
    "actually the purpose is external flow",   # purpose
    "make it 2D instead",                      # dimensionality
    "use a finer mesh please",                 # fidelity
])
async def test_4_5_every_kind_of_correction_invalidates(SessionLocal, correction):
    sid, _, _ = await _seed(SessionLocal)
    out = await _accept(SessionLocal, sid, correction)
    after = await _state(SessionLocal, sid)
    assert out.transition.invalidated_approval, f"{correction!r} left the approval live"
    assert after["approval_live"] is False
    assert await _dispatch(SessionLocal, sid) != "dispatched"


async def test_6_the_user_selected_engine_is_never_changed_by_the_message_authority(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    await _accept(SessionLocal, sid, "actually the purpose is external flow")
    async with SessionLocal() as s:
        row = await repo.get_for_owner(s, sid, OWNER)
    assert row.mesh_engine == "gmsh", "the message authority silently changed the chosen engine"
    assert row.purpose == "internal_cfd", "the message authority rewrote approved requirements"


# duplicates and concurrency

async def test_7_the_same_message_twice_invalidates_once_and_stays_invalid(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    first = await _accept(SessionLocal, sid, "no, change the engine")
    second = await _accept(SessionLocal, sid, "no, change the engine")

    after = await _state(SessionLocal, sid)
    assert first.transition.invalidated_approval
    # The second message finds no LIVE approval, so it is an ordinary message - and critically it
    # does not resurrect, re-invalidate or overwrite the audit record of the first.
    assert second.status is msg.MessageStatus.proceed
    assert after["approval_status"] == ap.INVALIDATED
    assert after["user_messages"] == 3
    assert after["jobs"] == 0


async def test_8_two_concurrent_messages_serialize_on_the_row_lock(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)

    results = await asyncio.gather(
        _accept(SessionLocal, sid, "no, change the engine to snappy"),
        _accept(SessionLocal, sid, "actually make it 2D"),
    )
    after = await _state(SessionLocal, sid)

    assert after["user_messages"] == 3, "a concurrent message was lost"
    assert after["approval_status"] == ap.INVALIDATED
    # Exactly one of them saw the live approval; the other arrived after it was already withdrawn.
    invalidated = [r for r in results if r.transition.invalidated_approval]
    assert len(invalidated) == 1, f"{len(invalidated)} messages each invalidated the same snapshot"
    assert await _dispatch(SessionLocal, sid) != "dispatched"


async def test_8b_a_second_message_blocks_on_the_row_lock_until_the_first_commits(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    holding = asyncio.Event()
    release = asyncio.Event()
    second_finished = asyncio.Event()

    async def _hold_the_lock():
        async with SessionLocal() as db:
            await repo.get_for_update(db, sid)
            holding.set()
            await release.wait()
            await db.commit()

    async def _second_message():
        await _accept(SessionLocal, sid, "no, change the engine to snappy")
        second_finished.set()

    holder = asyncio.create_task(_hold_the_lock())
    await asyncio.wait_for(holding.wait(), timeout=10)

    second = asyncio.create_task(_second_message())
    # Give the second task real opportunities to run. It must still be blocked in the database.
    for _ in range(50):
        await asyncio.sleep(0)
    assert not second_finished.is_set(), (
        "a second message proceeded while the session row was locked - the messages are not "
        "serialised and two could classify against the same gate")

    release.set()
    await holder
    await asyncio.wait_for(second, timeout=10)
    assert second_finished.is_set()
    assert (await _state(SessionLocal, sid))["approval_status"] == ap.INVALIDATED


async def test_8c_the_lock_covers_the_READ_so_two_messages_cannot_classify_the_same_gate(
        SessionLocal, monkeypatch):
    sid, _, _ = await _seed(SessionLocal)

    reached_write = asyncio.Event()
    may_write = asyncio.Event()
    second_read_done = asyncio.Event()
    reads = {"n": 0}

    real_append = repo.append_message
    real_for_update = repo.get_for_update
    real_for_owner = repo.get_for_owner
    first = {"done": False}

    async def _count_read(coro):
        row = await coro
        reads["n"] += 1
        if reads["n"] >= 2:
            second_read_done.set()
        return row

    async def _watched_for_update(db, session_id):
        return await _count_read(real_for_update(db, session_id))

    async def _watched_for_owner(db, session_id, owner_id):
        return await _count_read(real_for_owner(db, session_id, owner_id))

    async def _pausing_append(db, session_id, role, content):
        if role == "user" and not first["done"]:
            first["done"] = True
            reached_write.set()
            await may_write.wait()
        return await real_append(db, session_id, role, content)

    monkeypatch.setattr(repo, "get_for_update", _watched_for_update)
    monkeypatch.setattr(repo, "get_for_owner", _watched_for_owner)
    monkeypatch.setattr(repo, "append_message", _pausing_append)

    task_a = asyncio.create_task(_accept(SessionLocal, sid, "no, change the engine to snappy"))
    await asyncio.wait_for(reached_write.wait(), timeout=10)
    assert reads["n"] == 1, "the first task had not yet read the gate"

    task_b = asyncio.create_task(_accept(SessionLocal, sid, "actually make it 2D"))
    locked_out = False
    try:
        await asyncio.wait_for(second_read_done.wait(), timeout=3)
    except TimeoutError:
        locked_out = True

    may_write.set()
    monkeypatch.setattr(repo, "get_for_owner", real_for_owner)
    out_a, out_b = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=30)

    assert locked_out, (
        "a second message read the intake gate while the first held the row - the lock does not "
        "cover the read, so both classify against the same snapshot")
    invalidated = [o for o in (out_a, out_b) if o.transition.invalidated_approval]
    assert len(invalidated) == 1, f"{len(invalidated)} messages acted on one snapshot"
    after = await _state(SessionLocal, sid)
    assert after["approval_status"] == ap.INVALIDATED
    assert after["user_messages"] == 3, "a concurrent message was lost"


# racing the approval lifecycle

async def test_9_a_correction_racing_a_confirmation_never_lets_the_stale_snapshot_dispatch(
        SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    approving = await _accept(SessionLocal, sid, "yes, proceed")
    assert approving.status is msg.MessageStatus.approve
    assert (await _state(SessionLocal, sid))["approval_live"] is True

    correction, dispatch = await asyncio.gather(
        _accept(SessionLocal, sid, "no, change the engine to snappy"),
        _dispatch(SessionLocal, sid),
    )
    after = await _state(SessionLocal, sid)

    if dispatch == "dispatched":
        # The confirmation took the lock first and committed the exact intent the user had
        # approved at that moment. The correction then found a dispatched session.
        assert after["jobs"] == 1
        assert correction.status is msg.MessageStatus.already_dispatched
    else:
        # The correction took the lock first, so the snapshot was already withdrawn.
        assert correction.transition.invalidated_approval
        assert after["jobs"] == 0, "a withdrawn approval still created a job"
        assert after["approval_live"] is False
    assert after["jobs"] <= 1


async def test_10_a_message_arriving_after_dispatch_is_recorded_and_changes_nothing(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    assert (await _accept(SessionLocal, sid, "yes, proceed")).status is msg.MessageStatus.approve
    assert await _dispatch(SessionLocal, sid) == "dispatched"
    before = await _state(SessionLocal, sid)

    out = await _accept(SessionLocal, sid, "actually, stop")

    after = await _state(SessionLocal, sid)
    assert out.status is msg.MessageStatus.already_dispatched
    assert out.job_id == before["linked_job"]
    assert after["user_messages"] == before["user_messages"] + 1, "the message was not recorded"
    assert after["jobs"] == 1, "a second job was created"
    assert after["linked_job"] == before["linked_job"], "the linked job was replaced"


async def test_11_a_message_racing_dispatch_never_produces_two_jobs(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)

    await asyncio.gather(
        _accept(SessionLocal, sid, "hmm, maybe"),
        _dispatch(SessionLocal, sid),
        _dispatch(SessionLocal, sid),
    )
    after = await _state(SessionLocal, sid)
    assert after["jobs"] <= 1, f"{after['jobs']} jobs created for one approval"


# staleness, failure, identity

async def test_12_a_deferred_snapshot_still_answers_the_users_next_clear_reply(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    before = await _state(SessionLocal, sid)

    hedge = await _accept(SessionLocal, sid, "maybe")
    mid = await _state(SessionLocal, sid)
    assert hedge.status is msg.MessageStatus.approval_deferred
    assert mid["revision"] != before["revision"], "the conversation revision did not advance"
    assert mid["approval_status"] == ap.AWAITING, "a hedge invalidated the snapshot"
    assert await _dispatch(SessionLocal, sid) != "dispatched", (
        "the deferred snapshot dispatched without a clear answer")

    assert (await _accept(SessionLocal, sid, "yes, proceed")).status is msg.MessageStatus.approve
    assert await _dispatch(SessionLocal, sid) == "dispatched"
    assert (await _state(SessionLocal, sid))["jobs"] == 1


async def test_12b_the_transition_reports_the_revision_the_message_produced(SessionLocal):
    sid, _, _ = await _seed(SessionLocal, with_approval=False)
    out = await _accept(SessionLocal, sid, "the inlet is 2 m/s")

    async with SessionLocal() as s:
        row = await repo.get_for_owner(s, sid, OWNER)
    expected = at.revision_of(row.messages)
    assert out.transition.revision == expected, (
        "the transition reported a revision that does not match the stored conversation")
    assert out.transition.revision, "the revision is blank"

    second = await _accept(SessionLocal, sid, "and the outlet is atmospheric")
    assert second.transition.revision != out.transition.revision, (
        "the revision did not advance with the message")


async def test_13_a_failure_before_the_gate_write_leaves_no_message_and_no_transition(
        SessionLocal, monkeypatch):
    sid, _, _ = await _seed(SessionLocal)
    before = await _state(SessionLocal, sid)

    async def _boom(*a, **k):
        raise RuntimeError("gate write failed")
    monkeypatch.setattr(repo, "set_intake_gate", _boom)

    with pytest.raises(RuntimeError, match="gate write failed"):
        await _accept(SessionLocal, sid, "no, change the engine")

    after = await _state(SessionLocal, sid)
    assert after == before, (
        "a failed transaction left durable residue - the message and the gate transition are "
        "not atomic")


async def test_14_a_failure_after_the_message_write_rolls_the_message_back_too(SessionLocal,
                                                                              monkeypatch):
    sid, _, _ = await _seed(SessionLocal)
    before = await _state(SessionLocal, sid)

    real = repo.set_intake_gate
    calls = {"n": 0}

    async def _fail_after_message(db, session_id, gate):
        calls["n"] += 1
        raise RuntimeError("crash between the message and its consequence")
    monkeypatch.setattr(repo, "set_intake_gate", _fail_after_message)

    with pytest.raises(RuntimeError):
        await _accept(SessionLocal, sid, "no, change the engine")
    monkeypatch.setattr(repo, "set_intake_gate", real)

    after = await _state(SessionLocal, sid)
    assert calls["n"] == 1, "the gate write was never attempted"
    assert after["user_messages"] == before["user_messages"], (
        "the user message was committed without its gate consequence - the exact window H-11 "
        "removed")
    assert after["approval_status"] == ap.AWAITING
    assert after["approval_live"] is True, (
        "the snapshot was left in a state it can never leave")


async def test_15_another_owner_cannot_post_into_this_session(SessionLocal):
    sid, _, _ = await _seed(SessionLocal)
    before = await _state(SessionLocal, sid)

    out = await _accept(SessionLocal, sid, "yes, proceed", owner="someone-else")

    after = await _state(SessionLocal, sid)
    assert out.status is msg.MessageStatus.not_found, (
        "another tenant's message was accepted into this session")
    assert after == before, "an unauthorized message changed durable state"


async def test_15b_an_unknown_session_is_indistinguishable_from_an_unowned_one(SessionLocal):
    out = await _accept(SessionLocal, uuid.uuid4(), "hello")
    assert out.status is msg.MessageStatus.not_found
