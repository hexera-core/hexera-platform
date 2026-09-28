# Responsibility: Verify a conversation whose run has ended takes the next message as the start of
# another run on the same geometry, while a run still in flight keeps the session closed to dispatch.
# Boundaries: the message authority and the approval authority with fake repositories; no model, no
# database. The prompt block the intake turn receives is checked as text.
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import meshpipeline.agents.intake.agent as intake
import meshpipeline.agents.intake.message as msg
from meshpipeline.agents.intake import approval as ap
from meshpipeline.persistence.job_state import ACTIVE_STATES, is_active
from meshpipeline.persistence.models import JobStatus

SID = uuid.UUID("eeee1111-2222-4222-b222-eeeeeeeeeeee")
OWNER = "alice"
SOURCE = uuid.uuid4()
OLD_JOB = uuid.uuid4()

#: the snapshot approval.py leaves on the gate once a run has dispatched from it
_DISPATCHED_PAYLOAD = {
    "mesh_engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
    "dimensionality": "3D", "engine_params": {"topology": "external"},
    "patches": [{"name": "wing", "role": "wall"}, {"name": "farfield", "role": "farfield"}],
    "request_txt": "External aero on the wing at 30 m/s, far-field 20 chords.",
    "review_brief_txt": "The wing patch is present; the far-field encloses it.",
    "mesh_fidelity": "standard",
}


def _dispatched_snapshot(job_id=OLD_JOB):
    return {"id": "snap-1", "status": ap.DISPATCHED, "job_id": str(job_id),
            "payload": dict(_DISPATCHED_PAYLOAD), "owner": OWNER, "session": str(SID)}


def _locked(**over):
    base = {"id": SID, "owner_id": OWNER, "job_id": OLD_JOB, "geometry_source_id": SOURCE,
            "geometry_interpretation_id": uuid.uuid4(),
            "intake_gate": {"approval": _dispatched_snapshot(),
                            "selection": {"id": "sel-1", "state": "confirmed"}},
            "messages": [{"role": "user", "content": "external aero, snappy"},
                         {"role": "assistant", "content": "Shall I proceed?"},
                         {"role": "user", "content": "yes"}],
            "request_txt": "", "llm_metadata": []}
    base.update(over)
    return SimpleNamespace(**base)


def _job(status, final_result=None):
    return SimpleNamespace(id=OLD_JOB, status=status, final_result=final_result)


class _Repo:
    """The session repository the authority writes through, recording every consequence."""

    def __init__(self, locked):
        self.locked = locked
        self.released: list = []
        self.appended: list = []
        self.get_for_update = AsyncMock(return_value=locked)
        self.get_for_owner = AsyncMock(return_value=locked)
        self.set_intake_gate = AsyncMock()

    async def append_message(self, db, sid, role, content):
        self.appended.append((role, content))

    async def release_job(self, db, sid):
        self.released.append(sid)
        self.locked.job_id = None


class _Jobs:
    def __init__(self, job):
        self.job = job
        self.reads: list = []

    async def get_internal(self, db, job_id):
        self.reads.append(job_id)
        return self.job


@asynccontextmanager
async def _db():
    yield AsyncMock()


async def _accept(locked, job, content="run it again", monkeypatch=None):
    if monkeypatch is not None:
        # the geometry check is not part of this: the part was confirmed long before the first run
        from meshpipeline.application import geometry_hold as gh
        monkeypatch.setattr(gh, "hold_applies", lambda sid: None)
    repo, jobs = _Repo(locked), _Jobs(job)
    inbound = msg.InboundMessage(session_id=SID, owner_id=OWNER, content=content)
    out = await msg.accept(inbound, session_repo=repo, db_factory=_db, logger=MagicMock(),
                           job_repo=jobs)
    return out, repo, jobs


# the run has ended: the conversation goes on

@pytest.mark.parametrize("status", [JobStatus.succeeded, JobStatus.failed,
                                    JobStatus.pending_review])
async def test_a_message_after_the_run_ended_is_an_intake_turn_not_the_running_line(
        status, monkeypatch):
    out, repo, _ = await _accept(_locked(), _job(status), monkeypatch=monkeypatch)
    assert out.status is msg.MessageStatus.proceed and out.continues_to_intake
    assert "already running" not in out.reply and out.reply == ""
    assert repo.appended == [("user", "run it again")], "the user's words were not recorded"


async def test_the_ended_run_is_released_under_the_same_transaction(monkeypatch):
    out, repo, _ = await _accept(_locked(), _job(JobStatus.failed), monkeypatch=monkeypatch)
    assert repo.released == [SID], "the session still points at a run that is over"
    assert out.previous_run is not None
    assert out.previous_run.job_id == str(OLD_JOB) and out.previous_run.status == "failed"


async def test_the_verdict_the_user_saw_travels_with_the_previous_run(monkeypatch):
    from meshpipeline.application import final_result as fr

    result = fr.FinalResult(
        schema_version=fr.FINAL_RESULT_SCHEMA_VERSION, job_id=str(OLD_JOB), owner_id=OWNER,
        status=fr.TerminalStatus.failed, engine="snappy", purpose="external_cfd",
        review_execution=fr.ReviewExecution.not_reached,
        failure_category=fr.FailureCategory.native_execution_failed.value, attempts=1,
        attempts_max=3)
    out, _, _ = await _accept(_locked(), _job(JobStatus.failed, result.to_dict()),
                              monkeypatch=monkeypatch)
    assert "did not complete successfully" in out.previous_run.outcome
    assert "run it again" in out.previous_run.outcome, (
        "the verdict no longer points the user at the chat that exists")


async def test_a_released_session_still_knows_its_previous_run_on_later_turns(monkeypatch):
    # The second message of the new intake: the link is gone, the dispatched snapshot remains.
    out, repo, jobs = await _accept(_locked(job_id=None), _job(JobStatus.succeeded),
                                    content="make the far-field 40 chords",
                                    monkeypatch=monkeypatch)
    assert out.status is msg.MessageStatus.proceed
    assert out.previous_run is not None and out.previous_run.job_id == str(OLD_JOB)
    assert jobs.reads == [OLD_JOB], "the snapshot's job was not read"
    assert repo.released == [], "a session with no link was released again"


async def test_a_session_that_never_ran_reads_no_job(monkeypatch):
    out, repo, jobs = await _accept(_locked(job_id=None, intake_gate={}), _job(JobStatus.failed),
                                    content="hello", monkeypatch=monkeypatch)
    assert out.status is msg.MessageStatus.proceed and out.previous_run is None
    assert jobs.reads == [] and repo.released == []


async def test_a_run_whose_row_is_gone_does_not_pin_the_session(monkeypatch):
    out, repo, _ = await _accept(_locked(), None, monkeypatch=monkeypatch)
    assert out.status is msg.MessageStatus.proceed
    assert repo.released == [SID]
    assert out.previous_run.status == ""


# the run is still going: closed to dispatch, open about what to do

@pytest.mark.parametrize("status", sorted(ACTIVE_STATES, key=lambda s: s.value))
async def test_a_live_run_still_answers_without_the_model_and_never_releases(status):
    out, repo, _ = await _accept(_locked(), _job(status))
    assert out.status is msg.MessageStatus.already_dispatched and out.answered
    assert out.job_id == OLD_JOB and str(OLD_JOB) in out.reply
    assert repo.released == [], "a session was released from a run still in flight"


async def test_the_live_answer_says_what_the_user_can_do():
    out, _, _ = await _accept(_locked(), _job(JobStatus.running))
    lowered = out.reply.lower()
    assert "still running" in lowered
    assert "once it finishes" in lowered and "run the same requirements again" in lowered
    assert "start a new session" in lowered


async def test_a_yes_while_the_run_is_live_never_approves_anything():
    # A live approval snapshot AND a live job: the job wins. The message is recorded, the
    # snapshot is untouched, and nothing is handed to the approval authority.
    live = {"id": "snap-2", "status": ap.AWAITING, "owner": OWNER, "session": str(SID),
            "expires_at": 4102444800, "expected_confirmation_msg_count": 3}
    out, repo, _ = await _accept(_locked(intake_gate={"approval": live}), _job(JobStatus.pending),
                                 content="yes")
    assert out.status is msg.MessageStatus.already_dispatched
    repo.set_intake_gate.assert_not_awaited()


def test_liveness_is_one_definition():
    from meshpipeline.persistence.repositories.job_repository import JobRepository

    assert set(JobRepository._ACTIVE_STATUSES) == set(ACTIVE_STATES)
    assert all(is_active(s) for s in ACTIVE_STATES)
    assert not any(is_active(s) for s in JobStatus if s not in ACTIVE_STATES)
    assert not is_active(None) and not is_active("") and not is_active("no-such-status")


# the new run: a second job, the same geometry, a fresh approval

class _Rec:
    def __init__(self):
        self.jobs: list = []
        self.links: list = []
        self.gates: list = []
        self.commits = 0
        self.dispatches = 0


class _ApprovalSessionRepo:
    def __init__(self, rec, locked): self.rec = rec; self.locked = locked
    async def get_for_update(self, db, sid): return self.locked
    async def set_intake_gate(self, db, sid, gate): self.rec.gates.append(gate)
    async def append_message(self, db, sid, role, content): pass
    async def link_job(self, db, sid, jid): self.rec.links.append(jid)
    async def set_request_txt(self, db, sid, txt): pass


class _ApprovalJobRepo:
    def __init__(self, rec): self.rec = rec
    async def create(self, db, *, owner_id, organization_id=""):
        job = SimpleNamespace(id=uuid.uuid4(), geometry_source_id=None,
                              geometry_interpretation_id=None)
        self.rec.jobs.append(job)
        return job
    async def mark_launch_failed(self, db, jid, detail): pass


class _Tx:
    def __init__(self, rec): self.rec = rec
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def commit(self): self.rec.commits += 1


class _Src:
    def __init__(self, sid): self.source_id = str(sid); self.original_filename = "wing.step"
    def to_payload(self): return {"source_id": self.source_id}


class _Interp:
    def __init__(self): self.interpretation_id = str(uuid.uuid4())
    def to_payload(self): return {"interpretation_id": self.interpretation_id}


async def test_the_new_run_creates_a_second_job_on_the_same_geometry(monkeypatch):
    # 1. the first run ended; the next message releases the session from it
    out, repo, _ = await _accept(_locked(), _job(JobStatus.succeeded), monkeypatch=monkeypatch)
    assert out.status is msg.MessageStatus.proceed and repo.locked.job_id is None

    # 2. the intake proposed again and the user approved: a NEW snapshot, awaiting confirmation
    fresh = {"id": "snap-2", "status": ap.AWAITING, "owner": OWNER, "session": str(SID),
             "fingerprint": "fp", "intent_fingerprint": "ifp", "intent_canonical": {},
             "payload": dict(_DISPATCHED_PAYLOAD)}
    approving = _locked(job_id=None, intake_gate={"approval": fresh, "selection": {"id": "s2"}})
    rec = _Rec()

    import meshpipeline.application.geometry_materializer as gm
    from meshpipeline.application import spend_gate

    async def _src(db, session, owner): return _Src(SOURCE)
    async def _interp(db, session, owner): return _Interp()
    async def _admit(db, *, owner_id, organization_id): return ""
    async def _dispatch(db, jid, payload): rec.dispatches += 1

    monkeypatch.setattr(gm, "source_ref_for_session", _src)
    monkeypatch.setattr(gm, "interpretation_ref_for_session", _interp)
    monkeypatch.setattr(spend_gate, "admit", _admit)
    monkeypatch.setattr(ap, "verify", lambda *a, **k: (True, ""))
    monkeypatch.setattr(ap, "assert_payload_matches_approval", lambda *a, **k: None)
    monkeypatch.setattr(ap, "_build_dispatch_payload", lambda **k: {"job_id": str(k["job_id"])})

    class _Quota:
        async def check_quotas(self, db, owner_id, *, plan=""): pass

    class _Sources:
        async def get_for_owner(self, db, sid, owner): return SimpleNamespace(purged_at=None)

    result = await ap.confirm_pending_approval(
        approving, _ApprovalSessionRepo(rec, approving), OWNER, SID, logger=MagicMock(),
        sessions=lambda: _Tx(rec), job_service=_Quota(), job_repo=_ApprovalJobRepo(rec),
        source_repo=_Sources(), dispatch=_dispatch)

    assert result.status is ap.ConfirmStatus.dispatched and result.dispatched
    assert len(rec.jobs) == 1 and rec.dispatches == 1
    new_job = rec.jobs[0]
    assert new_job.id != OLD_JOB, "the old run was reused"
    assert str(new_job.geometry_source_id) == str(SOURCE), "the new run left the geometry"
    assert rec.links == [new_job.id], "the session is not linked to its new run"
    assert rec.gates[-1]["approval"]["status"] == ap.DISPATCHED
    assert rec.gates[-1]["approval"]["job_id"] == str(new_job.id)


async def test_a_live_run_still_refuses_a_second_dispatch_in_the_approval_authority():
    # The authority's own lock is unchanged: a session still linked never creates a second job.
    rec = _Rec()
    linked = _locked()
    with pytest.raises(ap.ApprovalTransactionError) as ei:
        await ap.confirm_pending_approval(
            _locked(job_id=None), _ApprovalSessionRepo(rec, linked), OWNER, SID,
            logger=MagicMock(), sessions=lambda: _Tx(rec), job_service=None,
            job_repo=_ApprovalJobRepo(rec), source_repo=None, dispatch=None)
    assert ei.value.outcome.status is ap.ConfirmStatus.already_running
    assert rec.jobs == [] and rec.links == []


# what the intake turn is told

def _state(previous=None, gate=None, **over):
    base = {"job_id": str(SID), "session_id": str(SID), "user_id": OWNER,
            "messages": [{"role": "user", "content": "run it again"}],
            "request_txt": "", "engine": "snappy", "purpose": "external_cfd",
            "input_kind": "body-surface", "dimensionality": "3D", "engine_params": {},
            "intake_patches": [], "requested_mesh_fidelity": None,
            "intake_gate": gate if gate is not None else {"approval": _dispatched_snapshot()},
            "previous_run": previous or {"job_id": str(OLD_JOB), "status": "failed",
                                          "outcome": "Mesh generation did not complete "
                                                     "successfully.\nThe mesh did not pass "
                                                     "review."}}
    base.update(over)
    return base


def test_the_block_states_the_ended_run_and_what_it_was_approved_with():
    block = intake._previous_run_block(_state())
    assert str(OLD_JOB) in block and "failed" in block
    assert "The mesh did not pass review." in block
    # the approved payload, not the moved session columns
    assert "engine:         snappy" in block
    assert '"topology": "external"' in block
    assert "wing(wall)" in block and "farfield(farfield)" in block
    assert "far-field 20 chords" in block
    assert "mesh detail:    standard" in block


def test_the_block_falls_back_to_the_session_when_no_snapshot_dispatched():
    block = intake._previous_run_block(_state(gate={}, intake_patches=[{"name": "in", "type": "inlet"}]))
    assert "engine:         snappy" in block and "in(inlet)" in block
    assert "request_txt:    (unset)" in block


def test_the_block_demands_a_fresh_submission_and_never_claims_a_start():
    block = intake._previous_run_block(_state())
    assert "preview_selected_admission" in block and "submit_requirements" in block
    assert "fresh approval" in block
    assert "never claim it has started" in block
    assert "NOTHING above is recorded" in block


def test_the_turn_carries_the_block_only_when_a_run_ended_and_nothing_awaits_confirmation(
        monkeypatch):
    import asyncio

    import meshpipeline.adapters.model_inference.router as llm_router
    from meshpipeline.contracts.model_inference import ModelRoundResult

    seen: list = []

    async def _call(**kw):
        seen.append(kw["messages"][0]["content"])
        return ModelRoundResult(tool_calls=(), assistant_text="Same as last time?",
                                finish_reason="stop")
    monkeypatch.setattr(llm_router, "call_intake_model", _call)

    asyncio.run(intake.node_intake(_state()))
    assert "ANOTHER RUN ON THE SAME GEOMETRY" in seen[-1]

    asyncio.run(intake.node_intake(_state(previous_run=None, previous=None) | {"previous_run": {}}))
    assert "ANOTHER RUN ON THE SAME GEOMETRY" not in seen[-1]

    asyncio.run(intake.node_intake(_state(request_txt="submitted", awaiting_confirmation=True)))
    assert "CONFIRMATION TURN" in seen[-1]
    assert "ANOTHER RUN ON THE SAME GEOMETRY" not in seen[-1], (
        "two blocks argued over the same turn")


def test_the_previous_run_is_context_not_state():
    prev = msg.PreviousRun(job_id="j", status="failed", outcome="text")
    assert prev.as_state() == {"job_id": "j", "status": "failed", "outcome": "text"}
    assert set(msg.PreviousRun.__dataclass_fields__) == {"job_id", "status", "outcome"}, (
        "the previous run grew a field the intake could mistake for a recorded requirement")
