# Responsibility: Verify the broker never re-runs a job by itself: tasks ack on receipt, the pipeline task acks late
#                 only inside a visibility window longer than any run, and no task self-retries.
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


def test_the_pipeline_task_acks_late_inside_a_window_no_run_outlasts():
    # The one exception, as the REAL Celery resolves it: acked late so a busy worker reserves no
    # second job (it used to, and that job then waited out the broker's one-hour default when the
    # busy VM was deleted), with the visibility window past the pipeline deadline so a running job
    # is never handed to a second worker.
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.adapters.pipeline_execution.celery import run_simulation
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app

    assert run_simulation.acks_late is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    window = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert window > int(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS)


def test_the_run_simulation_task_never_self_retries():
    from meshpipeline.adapters.pipeline_execution.celery import run_simulation

    # a thin task boundary: the pipeline owns its own retry semantics; the Celery task
    # must not add a second, invisible retry loop on top.
    assert run_simulation.max_retries == 0
