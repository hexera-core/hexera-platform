# Responsibility: Verify a task acks on receipt and never self-retries, so a crashed job is not re-executed.
import pytest

pytest.importorskip("celery", reason="the real celery package (integration tier only)")

# Real celery package but NO external service (asserts config, not a live broker) - see `hermetic`.
pytestmark = pytest.mark.hermetic


def test_tasks_ack_on_receipt_so_a_crashed_job_is_never_auto_requeued():
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app

    # ack on RECEIPT (not late) + do not re-deliver on worker loss: together these stop
    # a long/crashed job being re-queued and re-run from scratch.
    assert celery_app.conf.task_acks_late is False
    assert celery_app.conf.task_reject_on_worker_lost is False


def test_the_run_simulation_task_never_self_retries():
    from meshpipeline.adapters.pipeline_execution.celery import run_simulation

    # a thin task boundary: the pipeline owns its own retry semantics; the Celery task
    # must not add a second, invisible retry loop on top.
    assert run_simulation.max_retries == 0
