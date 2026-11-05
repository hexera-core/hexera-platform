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


class _Nested:
    """SQLAlchemy's AsyncSessionTransaction in miniature: `begin_nested()` is a PLAIN call whose
    result is both awaited (`sp = await db.begin_nested()`) and entered (`async with
    db.begin_nested():`, the charge's savepoint). An `async def begin_nested` would hand the charge
    a coroutine, `async with` would raise TypeError, and the fail-open would swallow it - every
    charge assertion through it vacuous."""
    def __init__(self, db, logged):
        self.db, self.logged = db, logged

    def __await__(self):
        async def _self():
            return self
        return _self().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, *a):
        await (self.rollback() if exc_type else self.commit())
        return False

    async def commit(self):
        if self.logged:
            self.db.log.append("sp_commit")

    async def rollback(self):
        if self.logged:
            self.db.log.append("sp_rollback")


class _Db:
    """The FIRST savepoint is the review write's (logged); later ones are the history lookup's own
    and the charge's."""
    def __init__(self): self.log, self.opened = [], 0

    def begin_nested(self):
        self.opened += 1
        return _Nested(self, logged=self.opened == 1)


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

    async def decided_items(self, db, job_id):
        return []

    async def record_outcome(self, db, item_id, outcome):
        return True

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
    # only thing that keeps the purchase single is that the park does not charge. The charge is
    # observed BELOW the fail-open and its savepoint, so a charge that never ran cannot pass.
    from meshpipeline.application import metering_service
    charges = []

    async def _charge(db, *, job):
        charges.append(job)
        return 1
    monkeypatch.setattr(metering_service, "charge_for_job", _charge)

    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    await _finalize(JobStatus.pending_review, plan, _Repo())
    assert charges == [], "a parked run was charged before its re-run finished it"

    await _finalize(JobStatus.succeeded, None, _Repo())      # the re-run's real ending
    assert len(charges) == 1


# ---- "leave it" is an answer: a re-run never parks on the same questions again ----------------

FP_ITEMS = [{"group_key": "small_face:none", "defect_code": "small_face", "entity_index": i,
             "fingerprint": f"face:{i}"} for i in range(3)]
FP_STATE = {**STATE, "repair_review_candidates": FP_ITEMS}


class _History:
    """The job's decided items, as the repository returns them (oldest decision first)."""
    def __init__(self, decided): self.decided = decided
    async def decided_items(self, db, job_id):
        return [SimpleNamespace(fingerprint=d[0], state=d[1],
                                defect_code=d[2] if len(d) > 2 else "small_face")
                for d in self.decided]


class _Sessions:
    def __call__(self): return self
    async def __aenter__(self): return object()
    async def __aexit__(self, *a): return False


async def test_park_leave_all_run_fail_ends_as_an_ordinary_failure_that_says_what_was_left():
    plan = rrp.plan_review(FP_STATE, status=JobStatus.failed)
    assert plan.park
    history = _History([(f"face:{i}", "left") for i in range(3)])
    settled, left = await rrp.settle_left(plan, _Sessions(), str(JOB), repo=history)
    assert settled is None and left == {"count": 3, "codes": {"small_face": 3}}
    asm = TerminalAssembly(job_id=str(JOB), owner_id="o", status=JobStatus.failed,
                           failed_reason=FailedReason.unhandled, engine="cfmesh",
                           review_plan=settled, review_left=left)
    out = build_terminal_result(asm, delivered_types=[])
    assert out.result.review_parked == 0 and "waiting" not in out.closing_message
    assert "leave 3 things" in out.closing_message and "3 small face" in out.closing_message
    assert "same questions" in out.closing_message
    assert out.result.to_dict()["review_left"] == left
    repo = _Repo()
    _, jobs, _ = await _finalize(JobStatus.failed, settled, repo)
    assert repo.parks == [] and jobs.log[0] == ("transition", JobStatus.failed)


async def test_leave_some_approve_some_parks_again_only_on_what_was_not_left():
    plan = rrp.plan_review(FP_STATE, status=JobStatus.failed)
    # face:0 left; face:1 accepted but its fix was rolled back; face:2 never answered
    history = _History([("face:0", "left"), ("face:1", "accepted"), ("face:2", "unreviewed")])
    settled, left = await rrp.settle_left(plan, _Sessions(), str(JOB), repo=history)
    assert left == {} and settled.park
    assert [i["fingerprint"] for i in settled.items] == ["face:1", "face:2"]


async def test_a_later_answer_replaces_an_earlier_leave_and_an_unreadable_history_parks_as_before():
    plan = rrp.plan_review(FP_STATE, status=JobStatus.failed)
    history = _History([("face:0", "left"), ("face:0", "redirected")])
    settled, _ = await rrp.settle_left(plan, _Sessions(), str(JOB), repo=history)
    assert len(settled.items) == 3

    class _Broken:
        async def decided_items(self, db, job_id): raise RuntimeError("db down")
    settled, left = await rrp.settle_left(plan, _Sessions(), str(JOB), repo=_Broken())
    assert settled is plan and left == {}


async def test_leave_it_on_one_code_never_retires_another_code_on_the_same_face():
    """One face can carry two items (the mint keys by fingerprint AND code). Leaving the small face
    must not settle the open shell on that face, which nobody answered."""
    items = [{"group_key": "small_face:none", "defect_code": "small_face", "entity_index": 1,
              "fingerprint": "face:1"},
             {"group_key": "open_shell:sew_faces", "defect_code": "open_shell", "entity_index": 1,
              "fingerprint": "face:1"}]
    plan = rrp.plan_review({**STATE, "repair_review_candidates": items}, status=JobStatus.failed)
    history = _History([("face:1", "left", "small_face")])
    settled, left = await rrp.settle_left(plan, _Sessions(), str(JOB), repo=history)
    assert left == {} and settled.park
    assert [i["defect_code"] for i in settled.items] == ["open_shell"]


async def test_a_delivered_run_still_records_every_item_whatever_was_left_before():
    plan = rrp.plan_review(FP_STATE, status=JobStatus.succeeded)
    history = _History([(f"face:{i}", "left") for i in range(3)])
    settled, left = await rrp.settle_left(plan, _Sessions(), str(JOB), repo=history)
    assert settled is plan and len(settled.items) == 3 and left == {}


async def test_the_mint_caps_dropped_count_survives_the_state_merge_into_the_parks_record():
    import typing

    from meshpipeline.contracts.pipeline_state import PipelineState
    from meshpipeline.pipeline.data_contract import STATE_FIELDS
    assert "repair_review_truncated" in typing.get_type_hints(PipelineState)
    assert "repair_review_truncated" in STATE_FIELDS
    plan = rrp.plan_review({**STATE, "repair_review_truncated": 1200}, status=JobStatus.failed)
    assert plan.park and plan.truncated == 1200
    asm = TerminalAssembly(job_id=str(JOB), owner_id="o", status=JobStatus.pending_review,
                           failed_reason=FailedReason.unhandled, engine="cfmesh", review_plan=plan)
    record = build_terminal_result(asm, delivered_types=[]).result.to_dict()
    assert record["review_truncated"] == 1200 and record["review_parked"] == 3
