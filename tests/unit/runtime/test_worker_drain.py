# Responsibility: Verify "this worker is shutting down" travels from celery's main process to the pool process running
#                 the job, and that a stale mark from a previous run of the container is cleared before any job starts.
# Boundaries: the marker and the celery signal wiring; the hand-back itself is tests/unit/application/test_worker_handoff.py.
from __future__ import annotations

import os

import pytest

from meshpipeline.runtime import worker_drain


@pytest.fixture(autouse=True)
def _own_tmp(monkeypatch, tmp_path):
    import tempfile
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


def test_the_pool_process_sees_its_main_process_start_shutting_down(monkeypatch):
    # Docker's SIGTERM reaches PID 1 - celery's main process - and nothing else. The job runs in a
    # pool process whose parent that is.
    main_pid = 4242
    monkeypatch.setattr(os, "getppid", lambda: main_pid)
    assert worker_drain.drain_requested() is False
    worker_drain.request_drain(main_pid)
    assert worker_drain.drain_requested() is True


def test_another_worker_shutting_down_does_not_drain_this_one(monkeypatch):
    monkeypatch.setattr(os, "getppid", lambda: 4242)
    worker_drain.request_drain(9999)
    assert worker_drain.drain_requested() is False


def test_a_job_run_in_the_main_process_itself_sees_the_mark_too():
    # the solo pool, or a test: there is no parent to ask
    worker_drain.request_drain()
    assert worker_drain.drain_requested() is True


def test_a_mark_left_by_a_previous_run_of_the_container_is_cleared():
    # A container restarted in place keeps its filesystem and its main process is PID 1 again;
    # a stale mark would make every job the new worker takes hand itself straight back.
    worker_drain.request_drain()
    worker_drain.clear_drain()
    assert worker_drain.drain_requested() is False
    worker_drain.clear_drain()          # clearing nothing is not an error


def test_celery_wires_the_mark_to_its_own_shutdown(monkeypatch):
    from celery.signals import worker_init, worker_process_init, worker_shutting_down

    from meshpipeline.runtime import celery_worker

    # (the unit tier's celery stub keeps its receivers - tests/unit/conftest.py)
    assert celery_worker._on_worker_shutting_down in worker_shutting_down.receivers
    assert celery_worker._on_worker_init in worker_init.receivers
    assert celery_worker._on_worker_process_init in worker_process_init.receivers

    # the shutdown handler writes this process's mark...
    celery_worker._on_worker_shutting_down(sig="SIGTERM", how="Warm", exitcode=0)
    assert worker_drain.marker_path(os.getpid()).exists()
    # ...and the start handler removes it before a pool process is forked
    monkeypatch.setattr("meshpipeline.runtime.metrics_server.reset_multiproc_dir", lambda: None)
    monkeypatch.setattr("meshpipeline.runtime.observability.otel.setup_tracing", lambda: None)
    celery_worker._on_worker_init()
    assert not worker_drain.marker_path(os.getpid()).exists()


def test_every_pool_process_installs_the_probe(monkeypatch):
    from meshpipeline.contracts import worker_drain as contract
    from meshpipeline.runtime import celery_worker

    monkeypatch.setattr(contract, "_probe", None)
    monkeypatch.setattr("meshpipeline.runtime.composition.install_adapters", lambda: None)
    celery_worker._on_worker_process_init()
    assert contract._probe is worker_drain.drain_requested
