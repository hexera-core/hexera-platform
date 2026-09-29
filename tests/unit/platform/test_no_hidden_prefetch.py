# Responsibility: Verify a worker busy with a mesh job reserves no second job, and that the broker can never hand a
#                 still-running job to a second worker.
# Boundaries: the Celery configuration as recorded by the unit tier's stub, and the fleet's startup script; no broker.
from __future__ import annotations

from pathlib import Path

import meshpipeline.settings.runtime as rtcfg
from meshpipeline.adapters.pipeline_execution import celery as tasks
from meshpipeline.adapters.pipeline_execution import celery_app as app_mod

REPO = Path(__file__).resolve().parents[3]

# THE DEFECT THIS PINS. Shared dev, 2026-09-29: job e5289e02 (ahmed_variant_001) was created at
# 05:24:32 UTC and started at 06:27:59; job ee8267b6 (tee_wye_003) was created at 06:27:06 and
# started at 07:28:18. An hour each, with idle workers up for part of it and the published queue
# depth reading 0 the whole time. Early acknowledgement acks a task when it STARTS, which frees the
# worker's one prefetch slot, so a worker busy with a mesh job reserved the next job at once and sat
# on it - out of the queue list (so out of the metric), out of every idle worker's reach. When the
# busy VM was deleted the reserved message stayed in Redis's unacked set until the one-hour
# visibility timeout restored it: 05:24 + 1 h, 06:27 + 1 h.


def test_the_pipeline_task_holds_its_slot_until_it_is_done():
    # acked late, the running job's message stays unacknowledged, the worker's prefetch count of
    # one stays used, and nothing else is reserved behind it
    assert tasks.run_simulation.task_options.get("acks_late") is True
    assert app_mod.celery_app.conf.worker_prefetch_multiplier == 1


def test_only_the_pipeline_task_acks_late():
    # Everything else keeps the early ack the application chose: a geometry check is seconds of
    # work, and the cleanup and export tasks have no worker loss worth a redelivery.
    assert app_mod.celery_app.conf.task_acks_late is False
    assert app_mod.celery_app.conf.task_reject_on_worker_lost is False
    for other in (tasks.scout_geometry, tasks.name_geometry):
        assert not other.task_options.get("acks_late"), other


def test_the_broker_never_redelivers_a_job_that_can_still_be_running():
    # The redelivery the early ack was chosen to prevent: a long job re-run from scratch because it
    # outran the broker's visibility window. The window must outlast the whole pipeline deadline
    # and the reaper's hour after it, by which time the job is terminal and a returned message is
    # skipped at its claim.
    window = app_mod.celery_app.conf.broker_transport_options["visibility_timeout"]
    assert window >= int(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS) + 3600, window
    # ...and it still travels beside the key prefix that keeps deployments apart
    assert "global_keyprefix" in app_mod.celery_app.conf.broker_transport_options


def test_the_fleet_runs_one_job_per_worker():
    # One slot per worker is what makes "acked late" mean "reserves nothing else".
    startup = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text("utf-8")
    sim = startup[startup.index("docker run -d --name hexera-worker "):]
    sim = sim[:sim.index("\n\n")]
    assert "--queues simulation_jobs --concurrency 1" in sim
