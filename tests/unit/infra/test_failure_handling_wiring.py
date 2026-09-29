# Responsibility: Verify a system failure is reported blamelessly and never becomes a verdict or a requeue.
from pathlib import Path

import pytest


async def test_failure_sink_emits_a_blameless_taxonomy_message_not_a_verdict():
    from meshpipeline.errors import classify_api_failure, user_message_for
    from meshpipeline.pipeline.graph import node_failure_handler

    marker = "builder: rate limited by the provider"
    out = await node_failure_handler({"api_failure": marker, "job_id": "job-1"})

    # exactly the taxonomy's message for this failure class - a hardcoded/wrong
    # message (or a handler that stops classifying) makes this inequality fail.
    assert out == {"outcome_message": user_message_for(classify_api_failure(marker))}
    # blameless + no fabricated verdict for an un-produced mesh
    assert marker not in out["outcome_message"]
    assert "reviewer_verdict" not in out


def test_a_gate_crash_is_a_system_failure_while_a_rejection_stays_a_rejection():
    from meshpipeline.engines.gates import GateCtx, GateSpec, run_gates
    from meshpipeline.errors import FailureClass, SystemFailure

    ctx = GateCtx(workspace=Path("/tmp"))

    def _boom(_ctx):
        raise ValueError("the gate blew up")

    with pytest.raises(SystemFailure) as ei:
        run_gates((GateSpec(key="explode", check=_boom),), ctx)
    # a crash is INTERNAL and keyed to the gate - swallowing it (or turning it into a
    # rejection) fails one of these assertions.
    assert ei.value.failure_class == FailureClass.INTERNAL
    assert "explode" in ei.value.dependency

    ok, key, feedback = run_gates(
        (GateSpec(key="quality", check=lambda _c: (False, "negative-volume cells")),), ctx)
    assert ok is False and key == "quality" and "negative-volume" in feedback


async def test_router_fails_fast_when_the_breaker_is_open_and_never_falls_back(monkeypatch):
    import meshpipeline.agents.builder.settings as bcfg
    from meshpipeline.adapters._shared.resilience import get_breaker, reset_breakers
    from meshpipeline.adapters.model_inference import providers, router

    reset_breakers()
    # The builder's OWN declared circuit, asked for rather than restated. This test is about
    # failing fast on an open circuit; with the name spelled out here it would quietly stop
    # opening the circuit the router consults the day that name changes - as it just did, when
    # `deepinfra_builder` became `builder`.
    breaker = get_breaker(bcfg.BUILDER_ROUTE.primary.circuit_group)
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()
    assert not breaker.allow(), "precondition: the circuit must be OPEN"

    def _must_not_resolve(_target):
        raise AssertionError("the router reached for a provider while the circuit was open")
    monkeypatch.setattr(providers, "client_for", _must_not_resolve)

    try:
        round_result = await router.call_builder_model([{"role": "user", "content": "x"}])
    finally:
        reset_breakers()

    assert not round_result.ok
    assert round_result.failure_marker == "<<API_FAILURE:builder_circuit_open>>"


async def test_the_reaper_marks_a_stalled_running_job_failed_not_requeued(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import sqlalchemy.ext.asyncio as sa_aio
    from sqlalchemy.sql.dml import Update

    import meshpipeline.application.maintenance.cleanup as cleanup
    from meshpipeline.persistence.models import FailedReason, JobStatus

    stalled = SimpleNamespace(
        id="job-stalled", status=JobStatus.running, owner_id="owner-1", current_attempt=2,
        started_at=datetime.now(UTC) - timedelta(hours=9999),
        created_at=datetime.now(UTC) - timedelta(hours=9999))
    updates: list = []
    published: list = []

    class _Result:
        rowcount = 1     # the reaper's CAS reads rowcount: 1 == it performed the transition
        def scalars(self): return self
        def all(self): return [stalled]

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            if isinstance(stmt, Update):
                updates.append(stmt)
            return _Result()
        async def commit(self): pass

    async def _dispose(): pass
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_dispose))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: (lambda: _Session()))
    monkeypatch.setattr(cleanup, "_publish_terminal_log",
                        lambda job_id, text="": published.append((job_id, text)))

    result = await cleanup._reap_stalled_async()

    assert result["reaped"] == ["job-stalled"] and result["count"] == 1
    # the single UPDATE sets the terminal FAILED status - not a re-queue, not 'running'
    assert len(updates) == 1
    values = {col.name: getattr(bind, "value", bind) for col, bind in updates[0]._values.items()}
    assert values["status"] == JobStatus.failed
    assert values["failed_reason"] == FailedReason.unhandled
    # THE DURABLE ACCOUNT. A reaped job used to carry no record, so once its event log expired
    # the only thing left to show was "Job already failed." The record says the worker was lost.
    record = values["final_result"]
    assert record["failure_category"] == "worker_lost" and record["status"] == "failed"
    assert record["owner_id"] == "owner-1" and record["attempts"] == 2
    assert record["required_ready"] is False and record["reviewer_verdict"] is None
    # ... and the live closing says the same thing as the record
    assert published and published[0][0] == "job-stalled"
    text = published[0][1]
    assert "worker" in text and "lost" in text and "run it again" in text


def test_delivery_counter_increments_and_fails_open():
    import meshpipeline.application.pipeline_run as wt
    from meshpipeline.contracts import delivery_guard

    class _FakeGuard:
        def __init__(self): self.counts: dict = {}
        def record_attempt(self, job_id):
            self.counts[job_id] = self.counts.get(job_id, 0) + 1
            return self.counts[job_id]

    try:
        delivery_guard.set_delivery_guard(_FakeGuard())
        assert wt._incr_delivery_count("job-x") == 1
        assert wt._incr_delivery_count("job-x") == 2
        assert wt._incr_delivery_count("job-x") == 3

        class _BrokenGuard:
            def record_attempt(self, job_id): raise RuntimeError("guard store down")
        delivery_guard.set_delivery_guard(_BrokenGuard())
        assert wt._incr_delivery_count("job-y") == 1     # fails OPEN

        delivery_guard.set_delivery_guard(None)          # not composed at all
        assert wt._incr_delivery_count("job-z") == 1     # also fails open
    finally:
        delivery_guard.set_delivery_guard(None)


async def test_reviewer_without_a_renderable_mesh_is_a_system_failure_not_a_verdict(tmp_path):
    from meshpipeline.agents.reviewer.visual import node_reviewer
    from meshpipeline.errors import FailureClass, classify_api_failure

    out = await node_reviewer({
        "job_id": "job-novis", "openfoam_workspace": str(tmp_path),
        "engine": "snappy", "purpose": "external_cfd",
        "mesh_manifest": {}, "retry_count": 0,
    })

    # A pre-loop failure now ALSO leaves the canonical run record - that is what makes an
    # unreviewable mesh diagnosable after the fact. It carries no verdict.
    assert out["api_failure"] == "reviewer_evidence_missing"
    assert set(out) == {"api_failure", "agent_run_records"}
    assert len(out["agent_run_records"]) == 1
    assert "accepted_verdict" not in out["agent_run_records"][0]["extension"]
    # still a system failure (we could not verify) - never provider downtime, never a verdict.
    fc = classify_api_failure(out["api_failure"])
    assert fc is FailureClass.REVIEW_EVIDENCE_MISSING and fc.is_system
    assert fc is not FailureClass.PROVIDER_DOWN
    assert "reviewer_verdict" not in out and "reviewer_result" not in out


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made


async def _reap_one(monkeypatch, job):
    """Run the reaper over one selected row; return (update values, where params, published)."""
    from types import SimpleNamespace

    import sqlalchemy.ext.asyncio as sa_aio
    from sqlalchemy.sql.dml import Update

    import meshpipeline.application.maintenance.cleanup as cleanup

    updates: list = []
    published: list = []

    class _Result:
        rowcount = 1
        def scalars(self): return self
        def all(self): return [job]

    class _Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt):
            if isinstance(stmt, Update):
                updates.append(stmt)
            return _Result()
        async def commit(self): pass

    async def _dispose(): pass
    monkeypatch.setattr(sa_aio, "create_async_engine",
                        lambda *a, **k: SimpleNamespace(dispose=_dispose))
    monkeypatch.setattr(sa_aio, "async_sessionmaker", lambda **k: (lambda: _Session()))
    monkeypatch.setattr(cleanup, "_publish_terminal_log",
                        lambda job_id, text="": published.append((job_id, text)))
    await cleanup._reap_stalled_async()
    assert len(updates) == 1
    stmt = updates[0]
    values = {col.name: getattr(bind, "value", bind) for col, bind in stmt._values.items()}
    where = stmt.whereclause.compile()
    # the status predicate's bound list: the states the compare-and-set accepts
    guarded = [v for v in where.params.values() if isinstance(v, list)]
    assert len(guarded) == 1, f"expected one status IN predicate, got {where.params}"
    return values, str(where), guarded[0], published


async def test_a_job_no_worker_ever_picked_up_is_not_told_its_worker_was_lost(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from meshpipeline.persistence.models import JobStatus

    never = SimpleNamespace(
        id="job-unclaimed", status=JobStatus.pending, owner_id="owner-1", current_attempt=0,
        started_at=None, lease_expires_at=None,
        created_at=datetime.now(UTC) - timedelta(hours=9999))
    values, _where_sql, accepted, published = await _reap_one(monkeypatch, never)

    assert values["status"] == JobStatus.failed
    record = values["final_result"]
    assert record["failure_category"] == "never_started", "an unclaimed job was blamed on a worker"
    text = published[0][1]
    assert "No worker picked this run up" in text and "lost" not in text
    # the compare-and-set is on the state the row was SELECTED in: a pending job a worker claims
    # in the window is running by then, and "no worker picked this run up" would be false of it
    assert accepted == [JobStatus.pending], accepted


async def test_a_running_job_whose_worker_is_gone_keeps_the_lost_worker_record(monkeypatch):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from meshpipeline.persistence.models import JobStatus

    gone = SimpleNamespace(
        id="job-gone", status=JobStatus.running, owner_id="owner-1", current_attempt=1,
        started_at=datetime.now(UTC) - timedelta(hours=1),
        lease_expires_at=datetime.now(UTC) - timedelta(hours=1),
        created_at=datetime.now(UTC) - timedelta(hours=1))
    values, _where_sql, accepted, published = await _reap_one(monkeypatch, gone)
    assert values["final_result"]["failure_category"] == "worker_lost"
    assert accepted == [JobStatus.running], accepted
    assert "lost" in published[0][1]
