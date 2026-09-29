# Responsibility: Verify a job whose worker was lost is re-run once, automatically, instead of the user being told to
#                 say "run it again" - and failed as before when it is lost a second time or cannot be re-run.
# Boundaries: the reaper's selection rules and its per-row decision, over a recording session and a stub launcher.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update

from meshpipeline.application.maintenance import cleanup
from meshpipeline.contracts import pipeline_execution
from meshpipeline.persistence.models import FailedReason, JobStatus

NOW = datetime.now(UTC)

# THE DEFECT THIS PINS. Shared dev, 2026-09-29: jobs 4c536ce5, 32423bfa and e5289e02 lost their
# worker VMs to autoscaler scale-in and were failed by this reaper with "please try again: say
# 'run it again' in this chat". The loss was ours - a machine going away - and the approved
# dispatch payload was right there on the row.


def _lost(**over):
    row = {
        "id": "job-lost", "status": JobStatus.running, "owner_id": "owner-1",
        "current_attempt": 1,
        "started_at": NOW - timedelta(minutes=50), "created_at": NOW - timedelta(minutes=51),
        "lease_expires_at": NOW - timedelta(minutes=31),
        "pipeline_deadline_at": NOW + timedelta(hours=5),
        "pipeline_dispatch_state": "submitted",
        "dispatch_payload": {"schema_version": 1, "domain": "wing"},
    }
    row.update(over)
    return SimpleNamespace(**row)


# which lost jobs earn the re-run

def test_a_job_that_lost_its_worker_is_re_run():
    assert cleanup.rerun_eligible(_lost(), NOW) is True


def test_a_release_never_confirmed_queued_is_launched_again_whatever_its_mark():
    # A hand-back (or a re-run) whose process died between the commit and the publish: nothing
    # was lost - no worker ran it - so it is the launch that has to happen, not a verdict.
    unqueued = _lost(status=JobStatus.pending, active_worker_token=None,
                     pipeline_dispatch_state=cleanup.RERUN_MARK)
    assert cleanup.rerun_eligible(unqueued, NOW) is True


@pytest.mark.parametrize("why, over", [
    ("it was already re-run once", {"pipeline_dispatch_state": cleanup.RERUN_MARK}),
    ("it never held a lease - a row from before leases", {"lease_expires_at": None}),
    ("there is nothing to re-launch", {"dispatch_payload": None}),
    ("its deadline is too close for a new run to mesh anything",
     {"pipeline_deadline_at": NOW + timedelta(minutes=10)}),
    ("its deadline has passed", {"pipeline_deadline_at": NOW - timedelta(minutes=1)}),
    ("it never started", {"status": JobStatus.pending, "started_at": None}),
])
def test_a_job_is_failed_instead_when(why, over):
    assert cleanup.rerun_eligible(_lost(**over), NOW) is False, why


# the reaper's decision per row

class _Launcher:
    RUNS_WORK = True

    def __init__(self, *, fails=False):
        self.fails, self.launched = fails, []

    async def launch(self, db, job_id, payload):
        if self.fails:
            raise ConnectionError("broker unreachable")
        self.launched.append((job_id, payload))

    async def revoke(self, job_id):
        pass


async def _reap(monkeypatch, rows, launcher):
    import sqlalchemy.ext.asyncio as sa_aio

    updates: list[Update] = []
    events: list[tuple] = []

    class _Result:
        rowcount = 1
        def scalars(self): return self
        def all(self): return rows

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            if isinstance(stmt, Update):
                updates.append(stmt)
                events.append(("update",))
            return _Result()
        async def commit(self): events.append(("commit",))

    async def _dispose(): pass
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_dispose))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: (lambda: _Session()))
    monkeypatch.setattr(pipeline_execution, "_launcher", launcher)
    real_launch = launcher.launch

    async def _launch(db, job_id, payload):
        events.append(("launch", job_id))
        await real_launch(db, job_id, payload)
    monkeypatch.setattr(launcher, "launch", _launch)
    monkeypatch.setattr(cleanup, "_publish_terminal_log",
                        lambda job_id, text="": events.append(("closing", job_id, text)))
    monkeypatch.setattr(cleanup, "publish_job_note",
                        lambda job_id, text, op_id: events.append(("note", job_id, text)))
    result = await cleanup._reap_stalled_async()
    return result, updates, events


def _values(stmt):
    return {col.name: getattr(bind, "value", bind) for col, bind in stmt._values.items()}


def _guarded_states(stmt):
    return [v for v in stmt.whereclause.compile().params.values() if isinstance(v, list)]


async def test_the_reaper_re_runs_a_lost_job_once_and_tells_the_user(monkeypatch):
    launcher = _Launcher()
    result, updates, events = await _reap(monkeypatch, [_lost()], launcher)

    assert result["rerun"] == ["job-lost"] and result["reaped"] == []
    release, confirm = updates
    values = _values(release)
    # back to waiting for a worker, owned by nobody, and marked so a second loss is not re-run
    assert values["status"] == JobStatus.pending and values["active_worker_token"] is None
    assert values["pipeline_dispatch_state"] == cleanup.RERUN_MARK
    # ...and marked released-but-not-yet-queued until the launch is confirmed
    released_at = values["lease_expires_at"]
    assert released_at is not None
    assert "final_result" not in values, "a re-run job was given a terminal record"
    # compare-and-set on the running row AND the lease it was selected with: a worker that
    # heartbeat in between was late, not lost, and keeps its job
    assert _guarded_states(release) == [[JobStatus.running]]
    assert "lease_expires_at" in str(release.whereclause)
    # launched only AFTER `pending` is committed, so a worker taking the message at once finds
    # the job waiting for it; confirmed only after the launcher took it
    kinds = [e[0] for e in events]
    assert kinds.index("commit") < kinds.index("launch")
    assert kinds.index("launch") < len(kinds) - 1 - kinds[::-1].index("update")
    assert _values(confirm) == {"lease_expires_at": None}
    assert released_at in confirm.whereclause.compile().params.values()
    assert launcher.launched == [("job-lost", _lost().dispatch_payload)]
    notes = [e for e in events if e[0] == "note"]
    assert notes and "again on another machine" in notes[0][2]
    assert "run it again" not in notes[0][2], "the user was still asked to retry by hand"
    assert not [e for e in events if e[0] == "closing"], "a re-run job was closed as failed"


async def test_a_job_lost_a_second_time_is_failed_as_before(monkeypatch):
    result, updates, events = await _reap(
        monkeypatch, [_lost(pipeline_dispatch_state=cleanup.RERUN_MARK)], _Launcher())

    assert result["reaped"] == ["job-lost"] and result["rerun"] == []
    (stmt,) = updates
    values = _values(stmt)
    assert values["status"] == JobStatus.failed and values["failed_reason"] == FailedReason.unhandled
    assert values["final_result"]["failure_category"] == "worker_lost"
    assert [e for e in events if e[0] == "closing"]


async def test_a_re_run_that_cannot_be_launched_is_tried_again_by_the_next_sweep(monkeypatch):
    # A broker outage is transient. The release stays marked unconfirmed, which is exactly what
    # the next sweep selects a lease later; the deadline bounds how long that can go on.
    result, updates, events = await _reap(monkeypatch, [_lost()], _Launcher(fails=True))

    assert result["reaped"] == [] and result["rerun"] == []
    (release,) = updates
    assert _values(release)["status"] == JobStatus.pending
    assert _values(release)["lease_expires_at"] is not None, "the release was not left marked"
    assert not [e for e in events if e[0] in ("closing", "note")], (
        "the user was told something that did not happen")


async def test_an_unconfirmed_release_is_launched_again_without_spending_the_re_run(monkeypatch):
    # The process that released it died before its message was sent. No worker ran it, so this is
    # not a second loss: the mark it carries is left exactly as it was.
    unqueued = _lost(status=JobStatus.pending, active_worker_token=None,
                     pipeline_dispatch_state="submitted")
    launcher = _Launcher()
    result, updates, _events = await _reap(monkeypatch, [unqueued], launcher)

    assert result["rerun"] == ["job-lost"] and result["reaped"] == []
    release = _values(updates[0])
    assert "pipeline_dispatch_state" not in release
    assert _guarded_states(updates[0]) == [[JobStatus.pending]]
    assert launcher.launched


async def test_a_deployment_that_cannot_re_run_keeps_failing_lost_jobs(monkeypatch):
    # The deferred launcher records a command for an operator; a re-run through it would wait
    # for a person, so a lost job is failed exactly as before.
    class _Deferred(_Launcher):
        RUNS_WORK = False
    result, updates, _events = await _reap(monkeypatch, [_lost()], _Deferred())
    assert result["rerun"] == [] and result["reaped"] == ["job-lost"]
    assert _values(updates[0])["status"] == JobStatus.failed


async def test_a_handed_back_job_that_nothing_finished_is_told_its_worker_was_lost(monkeypatch):
    # Handed back on a scale-in (pending again, but it HAD started), then never finished before
    # its deadline. "No worker picked this run up" would be false of it.
    handed_back = _lost(status=JobStatus.pending, lease_expires_at=None,
                        pipeline_deadline_at=NOW - timedelta(hours=1))
    result, updates, events = await _reap(monkeypatch, [handed_back], _Launcher())
    assert result["reaped"] == ["job-lost"]
    values = _values(updates[0])
    assert values["final_result"]["failure_category"] == "worker_lost"
    assert _guarded_states(updates[0]) == [[JobStatus.pending]]


# the selection rules

def _clause_sql(monkeypatch):
    import meshpipeline.settings.policy as polcfg
    import meshpipeline.settings.runtime as rtcfg
    monkeypatch.setattr(rtcfg, "WORKER_LEASE_SECONDS", 900)
    monkeypatch.setattr(polcfg, "STALLED_JOB_TIMEOUT_HOURS", 4)
    compiled = cleanup.stalled_jobs_clause(NOW).compile()
    return str(compiled), dict(compiled.params)


def test_the_never_started_ceiling_reaches_only_a_job_that_never_started(monkeypatch):
    # A handed-back job is pending again but has run - its creation time says nothing about it,
    # and a four-hour creation ceiling would fail a job that simply ran for four hours first.
    sql, _ = _clause_sql(monkeypatch)
    assert "started_at IS NULL AND simulation_jobs.created_at <" in sql, sql


def test_a_release_never_confirmed_queued_is_selected_a_lease_later(monkeypatch):
    sql, params = _clause_sql(monkeypatch)
    assert ("started_at IS NOT NULL AND simulation_jobs.active_worker_token IS NULL "
            "AND simulation_jobs.lease_expires_at IS NOT NULL "
            "AND simulation_jobs.lease_expires_at <") in sql, sql


def test_a_handed_back_job_is_bounded_by_its_pipeline_deadline(monkeypatch):
    sql, params = _clause_sql(monkeypatch)
    assert ("started_at IS NOT NULL AND simulation_jobs.pipeline_deadline_at IS NOT NULL "
            "AND simulation_jobs.pipeline_deadline_at <") in sql, sql
    assert NOW - timedelta(seconds=900) in params.values()
