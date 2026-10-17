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


class _Repo:
    def __init__(self):
        self.parks, self.items = [], []

    async def create_park(self, db, job_id, kept, sha, session):
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


async def _finalize(status, plan, repo, jobs=None, outbox=None):
    jobs, outbox = jobs or _Jobs(), outbox or _Outbox()
    out = await finalize_terminal_atomic(
        None, ownership=None, intended_status=status, failed_reason=None,
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
    assert "dedup_key" not in outbox.rows[0]


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


async def test_a_lost_status_cas_writes_no_park():
    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    repo = _Repo()
    await _finalize(JobStatus.pending_review, plan, repo,
                    jobs=_Jobs(TransitionResult.rejected_current_state))
    assert repo.parks == [] and repo.items == []


async def test_cancel_closes_the_open_park(monkeypatch):
    from tests.unit.application.test_job_cancel import (
        _cancel,
        _row,
        wired,  # noqa: F401
    )

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


class _Log:
    def __init__(self): self.errors = []
    def error(self, *a, **k): self.errors.append(a)


class _Session:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


async def test_a_rerun_that_parks_again_opens_a_second_park_and_the_first_stays_closed():
    repo = _Repo()
    plan = rrp.plan_review(STATE, status=JobStatus.failed)
    await _finalize(JobStatus.pending_review, plan, repo)
    first = repo.parks[0]
    # the run route closes the first park before the re-run
    await repo.close_park(None, first.id, "run")
    assert await rrp.refuse_second_open_park(_Session, plan, JOB, _Log(), repo) is plan
    await _finalize(JobStatus.pending_review, plan, repo)
    assert [p.status for p in repo.parks] == ["closed", "open"]
    assert repo.parks[0].closed_by == "run" and repo.parks[0].id != repo.parks[1].id
    # but an open park at park time is a defect: no second one, a plain failure
    log = _Log()
    assert await rrp.refuse_second_open_park(_Session, plan, JOB, log, repo) is None
    assert log.errors and len(repo.parks) == 2


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
