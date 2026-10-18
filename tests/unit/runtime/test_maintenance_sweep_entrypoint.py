# Responsibility: Verify the scheduled maintenance sweep runs every hosted step, keeps going past a failing
#                 one, and reports the run red when any step raised.
from __future__ import annotations

import json

import pytest

from meshpipeline.application.maintenance import cleanup, reconcile
from meshpipeline.runtime import maintenance_sweep

# Captured at import, before the fixture below replaces it, so the real binding can still be tested.
_REAL_BIND = maintenance_sweep.bind_ports


@pytest.fixture(autouse=True)
def _no_ports(monkeypatch):
    monkeypatch.setattr(maintenance_sweep, "bind_ports", lambda: None)


def _stub(monkeypatch, order: list[str], *, failing: set[str] = frozenset()) -> None:
    def make(name, mod, attr):
        def _run():
            order.append(name)
            if name in failing:
                raise RuntimeError(f"{name} broke")
            return {"count": 1}
        monkeypatch.setattr(mod, attr, _run)
    make("reap", cleanup, "reap_stalled_jobs")
    make("purge", cleanup, "purge_expired_geometry_sources")
    make("reconcile", reconcile, "reconcile_orphan_artifacts")


def test_every_step_runs_in_order_and_a_clean_run_exits_zero(monkeypatch, capsys):
    order: list[str] = []
    _stub(monkeypatch, order)
    assert maintenance_sweep.main([]) == 0
    assert order == ["reap", "purge", "reconcile"]
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == []
    assert list(out["steps"]) == [name for name, _ in maintenance_sweep.HOSTED_STEPS]
    assert all(v == {"count": 1} for v in out["steps"].values())


def test_a_failing_step_does_not_stop_the_others_and_the_run_is_red(monkeypatch, capsys):
    # A reaper that cannot reach the database must not cost the byte sweeps, and the execution
    # must still show as failed so the deploy problem is seen.
    order: list[str] = []
    _stub(monkeypatch, order, failing={"reap"})
    assert maintenance_sweep.main([]) == 1
    assert order == ["reap", "purge", "reconcile"]
    out = json.loads(capsys.readouterr().out)
    assert out["failed"] == ["tasks.cleanup.reap_stalled_jobs"]
    assert out["steps"]["tasks.cleanup.reap_stalled_jobs"] == {"error": "RuntimeError"}
    assert out["steps"]["tasks.cleanup.reconcile_orphan_artifacts"] == {"count": 1}


def test_the_ports_bound_are_the_three_a_sweep_works_through(monkeypatch):
    # The reaper's closing line needs a publisher; the two byte sweeps need the store; the reaper's
    # one automatic re-run of a job whose worker was lost needs the launcher the API dispatches
    # through. Nothing else install_adapters() would bind is touched, so nothing else can stop
    # the sweep from starting.
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.adapters.object_storage import factory
    from meshpipeline.adapters.pipeline_execution import celery as celery_launcher
    from meshpipeline.contracts import event_stream, object_storage, pipeline_execution

    monkeypatch.setattr(event_stream, "_factory", None)
    monkeypatch.setattr(object_storage, "_store", None)
    monkeypatch.setattr(factory, "_instance", None)
    monkeypatch.setattr(pipeline_execution, "_launcher", None)
    _REAL_BIND()
    assert isinstance(event_stream.publisher("job-1"), JobPublisher)
    assert object_storage.get_object_store() is not None
    assert pipeline_execution.get_pipeline_launcher() is celery_launcher
    assert pipeline_execution.launcher_runs_work(), (
        "the sweep's launcher cannot re-run a job, so every lost worker still fails its job")
