# Responsibility: Verify a failed repair run with items for a person parks, and nothing else does.
# Boundaries: the park rule, its write beside the status, the wording, the cancel and the re-park
#             guard, against recording fakes; no database.
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import meshpipeline.settings.cad_repair as rcfg
from meshpipeline.application import repair_review_park as rrp
from meshpipeline.application.terminal_finalize import (
    TerminalAssembly,
    build_terminal_result,
    finalize_terminal_atomic,
)
from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import FailedReason, JobStatus

JOB = uuid.uuid4()
ITEMS = [{"group_key": "g", "defect_code": "x", "entity_index": i} for i in range(3)]
STATE = {
    "session_id": "sess-1", "repair_review_candidates": ITEMS,
    "geometry": {"ref": {"sha256": "a" * 64}},
    "repair_attempt": {"operations": [
        {"op": "sew", "status": "kept"}, {"op": "fill", "status": "rolled_back"}]},
}


@pytest.fixture(autouse=True)
def review_on(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", True)


@pytest.fixture(autouse=True)
def no_real_model_call():
    """The unit tier installs the REAL router; a park test must never be able to dial it."""
    from meshpipeline.contracts import model_inference

    class _Refuses:
        async def call_repair_review_model(self, *a, **k):
            raise AssertionError("a park test must not reach the model")
    model_inference.set_model_router(_Refuses())
    yield
    model_inference.set_model_router(None)


class _Savepoint:
    def __init__(self, db): self.db = db
    async def commit(self): self.db.log.append("sp_commit")
    async def rollback(self): self.db.log.append("sp_rollback")


class _Db:
    def __init__(self): self.log = []
    async def begin_nested(self): return _Savepoint(self)


class _Repo:
    def __init__(self, fail_create=False):
        self.parks, self.items, self.fail_create = [], [], fail_create

    async def create_park(self, db, job_id, kept, sha, session):
        if self.fail_create:
            raise RuntimeError("insert failed")
        park = SimpleNamespace(id=uuid.uuid4(), job_id=job_id, kept_operations=kept,
                               source_sha256=sha, geometry_session_id=session, status="open")
        self.parks.append(park)
        return park

    async def add_items(self, db, job_id, park_id, items):
        self.items.append((park_id, list(items)))

    async def open_park(self, db, job_id):
        return next((p for p in self.parks if p.status == "open"), None)

    async def close_park(self, db, park_id, closed_by):
        for p in self.parks:
            if p.id == park_id:
                p.status, p.closed_by = "closed", closed_by


class _Jobs:
    def __init__(self, result=TransitionResult.applied):
        self.result, self.log = result, []

    async def transition(self, db, job_id, target, **kw):
        self.log.append(("transition", target))
        return self.result

    async def get_internal(self, db, job_id):
        return SimpleNamespace(status=JobStatus.pending_review, failed_reason=None)

    async def set_final_result(self, db, job_id, fr):
        self.log.append(("final_result", fr["review_parked"]))


class _Outbox:
    def __init__(self): self.rows = []

    async def enqueue(self, db, **kw):
        self.rows.append(kw)
        return True


PLAIN = ({"job_id": str(JOB), "review_parked": 0, "schema_version": 5}, "plain failure")


async def _finalize(status, plan, repo, jobs=None, outbox=None, db=None):
    jobs, outbox = jobs or _Jobs(), outbox or _Outbox()
    out = await finalize_terminal_atomic(
        db or _Db(), ownership=None, plain_failure=PLAIN, intended_status=status, failed_reason=None,
        final_result_dict={"job_id": str(JOB), "review_parked": 3, "schema_version": 5},
        closing_message="m", job_repo=jobs, outbox_repo=outbox, review_plan=plan,
        review_repo=repo)
    return out, jobs, outbox


async def test_a_failed_run_with_candidates_parks_and_persists_them():
    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    assert plan.park and plan.kept_operations == [{"op": "sew", "status": "kept"}]
    assert plan.source_sha256 == "a" * 64 and plan.geometry_session_id == "sess-1"
    repo = _Repo()
    _, jobs, outbox = await _finalize(JobStatus.pending_review, plan, repo)
    (park,) = repo.parks
    assert repo.items == [(park.id, ITEMS)]
    assert jobs.log[0] == ("transition", JobStatus.pending_review)
    assert park.kept_operations == plan.kept_operations
    assert outbox.rows[0]["dedup_key"].endswith(f":park:{park.id}")


async def test_a_failed_run_with_no_candidates_fails_exactly_as_today():
    state = {**STATE, "repair_review_candidates": []}
    assert rrp.plan_review(state, status=JobStatus.failed) is None
    absent = {k: v for k, v in STATE.items() if k != "repair_review_candidates"}
    assert rrp.plan_review(absent, status=JobStatus.failed) is None
    repo = _Repo()
    _, _, outbox = await _finalize(JobStatus.failed, None, repo)
    assert repo.parks == [] and repo.items == []
    assert outbox.rows[0]["dedup_key"] is None


async def test_review_off_never_parks(monkeypatch):
    monkeypatch.setattr(rcfg, "CAD_REPAIR_REVIEW_ENABLED", False)
    assert rrp.plan_review(STATE, status=JobStatus.failed) is None


async def test_a_succeeded_run_with_candidates_stays_succeeded_and_items_have_no_park():
    plan = rrp.plan_review(STATE, status=JobStatus.succeeded)
    assert plan is not None and not plan.park
    repo = _Repo()
    _, jobs, _ = await _finalize(JobStatus.succeeded, plan, repo)
    assert repo.parks == [] and repo.items == [(None, ITEMS)]
    assert jobs.log[0] == ("transition", JobStatus.succeeded)


async def test_a_lost_status_cas_rolls_the_park_back():
    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    db = _Db()
    await _finalize(JobStatus.pending_review, plan, _Repo(), db=db,
                    jobs=_Jobs(TransitionResult.rejected_current_state))
    assert db.log == ["sp_rollback"], "a park outlived the status CAS it belongs to"


async def test_cancel_closes_the_open_park(monkeypatch):
    from tests.unit.application.test_job_cancel import _cancel, _row

    import meshpipeline.persistence.repositories.repair_review_repository as rr
    from meshpipeline.application import job_cancel
    repo = _Repo()
    await repo.create_park(None, JOB, [], "a" * 64, "s")
    monkeypatch.setattr(rr, "RepairReviewRepository", lambda: repo)
    out, _, _ = await _cancel(_row(status=JobStatus.pending_review, token=None),
                              _wired(monkeypatch))
    assert out.result is job_cancel.CancelResult.cancelled
    assert repo.parks[0].status == "closed" and repo.parks[0].closed_by == "cancel"


def _wired(monkeypatch):
    import meshpipeline.application.outbox_publisher as obp
    import meshpipeline.contracts.pipeline_execution as pe

    class _L:
        async def revoke(self, job_id): return None
    monkeypatch.setattr(pe, "_launcher", _L())

    async def _deliver(sf, job_id, *, repo=None):
        return obp.PublishStats(published=1)
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _deliver)
    return {"log": [], "revoked": [], "delivered": []}


async def test_a_rerun_that_parks_again_opens_a_second_park_and_the_first_stays_closed():
    repo = _Repo()
    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    await _finalize(JobStatus.pending_review, plan, repo)
    first = repo.parks[0]
    # the run route closes the first park before the re-run
    await repo.close_park(None, first.id, "run")
    await _finalize(JobStatus.pending_review, plan, repo)
    assert [p.status for p in repo.parks] == ["closed", "open"]
    assert repo.parks[0].closed_by == "run" and repo.parks[0].id != repo.parks[1].id


async def test_an_open_park_at_park_time_fails_plainly_through_the_whole_terminal_path(monkeypatch):
    # the guard lives INSIDE the finalize transaction; assert what pipeline_run reads back
    import meshpipeline.application.outbox_publisher as obp
    import meshpipeline.application.terminal_finalize as tf
    import meshpipeline.persistence.repositories.repair_review_repository as rr
    repo, jobs, outbox = _Repo(), _Jobs(), _Outbox()
    await repo.create_park(None, JOB, [], "a" * 64, "s")      # still open: a defect
    monkeypatch.setattr(rr, "RepairReviewRepository", lambda: repo)
    monkeypatch.setattr(tf, "TerminalOutboxRepository", lambda: outbox)

    async def _noop(sf, job_id): return None
    monkeypatch.setattr(obp, "deliver_own_terminal_event", _noop)

    class _S(_Db):
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def commit(self): self.log.append("commit")

    class _J(_Jobs):
        async def get_internal(self, db, job_id):
            return SimpleNamespace(status=JobStatus.failed, failed_reason=None,
                                   created_at=None, ended_at=None)
    jobs = _J()
    asm = TerminalAssembly(job_id=str(JOB), owner_id="o", status=JobStatus.pending_review,
                           failed_reason=FailedReason.unhandled, engine="cfmesh",
                           review_plan=rrp.plan_review(STATE, status=JobStatus.failed))

    class _Lg:
        def warning(self, *a, **k): pass
    pub = await tf.assemble_and_finalize(_S, asm, ownership=None, lease_repo=None,
                                         job_repo=jobs, jlog=_Lg())
    assert pub.status == JobStatus.failed and len(repo.parks) == 1
    assert jobs.log[0] == ("transition", JobStatus.failed)
    assert pub.result.review_parked == 0 and "waiting" not in pub.closing_message


async def test_a_park_write_that_raises_still_lands_a_plain_failed_status():
    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    repo = _Repo(fail_create=True)
    out, jobs, outbox = await _finalize(JobStatus.pending_review, plan, repo)
    assert out.fell_back_to_failed
    assert jobs.log[0] == ("transition", JobStatus.failed)
    assert repo.parks == [] and repo.items == []
    assert outbox.rows[0]["event_payload"]["closing_message"] == "plain failure"
    assert outbox.rows[0]["dedup_key"] is None


@pytest.mark.parametrize("flags", [{"system_failure": True}])
def test_a_system_failure_or_timeout_never_parks_and_keeps_the_blameless_note(flags):
    assert rrp.plan_review(STATE, status=JobStatus.failed, **flags) is None
    note = "We hit a problem on our side; this is not your file."
    asm = TerminalAssembly(job_id=str(JOB), owner_id="o", status=JobStatus.failed,
                           failed_reason=FailedReason.unhandled, engine="cfmesh",
                           api_failure="llm_down", pre_composed_message=note,
                           review_plan=rrp.plan_review(STATE, status=JobStatus.failed, **flags))
    out = build_terminal_result(asm, delivered_types=[])
    assert out.result.review_parked == 0 and out.closing_message == note


def test_the_parked_outcome_message_names_the_count_and_promises_nothing_was_changed():
    asm = TerminalAssembly(
        job_id=str(JOB), owner_id="o", status=JobStatus.pending_review,
        failed_reason=FailedReason.unhandled, engine="cfmesh",
        review_plan=rrp.plan_review(STATE, status=JobStatus.failed))
    out = build_terminal_result(asm, delivered_types=[])
    assert out.result.review_parked == 3
    assert "3 things" in out.closing_message
    assert "waiting" in out.closing_message
    assert "nothing was changed without you" in out.closing_message
    assert out.result.to_dict()["review_parked"] == 3


async def test_a_parked_ending_charges_nothing_and_the_reruns_real_ending_charges_once(monkeypatch):
    # THE CHARGE-ONCE PIN. The re-run is the SAME job, re-dispatched without admission, so the
    # only thing that keeps the purchase single is that the park does not charge.
    from meshpipeline.application import terminal_finalize as tf
    charges = []

    async def _charge(db, row):
        charges.append(row)
    monkeypatch.setattr(tf, "_charge_fail_open", _charge)

    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    await _finalize(JobStatus.pending_review, plan, _Repo())
    assert charges == [], "a parked run was charged before its re-run finished it"

    await _finalize(JobStatus.succeeded, None, _Repo())      # the re-run's real ending
    assert len(charges) == 1
