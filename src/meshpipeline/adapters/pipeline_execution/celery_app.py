# Responsibility: Configure the Celery application: broker, queues, routes, priorities and the periodic schedule.
# Owns: the acknowledgement policy - a task is acked on receipt, so no long job is ever silently re-run.
# Boundaries: configuration only; the task bodies live in celery.py and maintenance_tasks.py.
from celery import Celery

import meshpipeline.settings.providers as provcfg

celery_app = Celery(
    "meshpipeline",
    broker=provcfg.REDIS_URL,
    backend=provcfg.REDIS_URL,
    include=["meshpipeline.adapters.pipeline_execution.celery",
             "meshpipeline.adapters.pipeline_execution.maintenance_tasks"],
)

celery_app.conf.update(
    # ONE REDIS, MANY DEPLOYMENTS. The queue names in task_routes below are literals, so two
    # deployments pointed at the same Memorystore instance consume each other's tasks - a
    # developer's simulation picked up by somebody else's worker fleet, and neither party with a
    # reason to suspect it. Personal environments share one instance deliberately (it is what
    # makes a first deploy minutes rather than half an hour), so the keyspace is what separates
    # them.
    #
    # EMPTY BY DEFAULT, and that matters more than the feature: an empty prefix is byte-for-byte
    # the behaviour every existing deployment already has, so shared dev, production and the
    # compose stack need no migration. A non-empty default would move their queues to new keys on
    # the next roll and strand whatever was already enqueued under the old ones.
    #
    # BOTH options, not one. The broker carries the queues; the result backend carries the task
    # results. Prefixing only the broker isolates the work and leaves the answers colliding.
    broker_transport_options={"global_keyprefix": provcfg.REDIS_KEY_PREFIX},
    result_backend_transport_options={"global_keyprefix": provcfg.REDIS_KEY_PREFIX},
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "worker.tasks.run_simulation": {"queue": "simulation_jobs"},
        "tasks.cleanup.purge_expired_workspaces":  {"queue": "cleanup_tasks"},
        "tasks.cleanup.reap_stalled_jobs":         {"queue": "cleanup_tasks"},
        "tasks.cleanup.reconcile_orphan_artifacts": {"queue": "cleanup_tasks"},
        "tasks.export_conversation_data.export_conversation_sample": {"queue": "training_export"},
    },
    task_queue_max_priority={
        "training_export": 9,
        "simulation_jobs": 5,
        "cleanup_tasks":   1,
    },
    beat_schedule={
        "purge-expired-workspaces": {
            "task": "tasks.cleanup.purge_expired_workspaces",
            "schedule": 3600.0,
            "options": {"queue": "cleanup_tasks"},
        },
        "reap-stalled-jobs": {
            "task": "tasks.cleanup.reap_stalled_jobs",
            "schedule": 600.0,
            "options": {"queue": "cleanup_tasks"},
        },
        # Uploaded geometry bytes past UPLOAD_RETENTION_DAYS. Hourly like the other byte-level
        # cleanups: the window is measured in days, so the scan only has to be regular.
        "purge-expired-geometry-sources": {
            "task": "tasks.cleanup.purge_expired_geometry_sources",
            "schedule": 3600.0,
            "options": {"queue": "cleanup_tasks"},
        },
        "reconcile-orphan-artifacts": {
            "task": "tasks.cleanup.reconcile_orphan_artifacts",
            "schedule": 600.0,
            "options": {"queue": "cleanup_tasks"},
        },
    },
    worker_prefetch_multiplier=1,
    # NO automatic redelivery. Ack a task on RECEIPT (acks_late=False) so a long-running job is
    # NEVER silently re-queued and re-run from scratch - that wasted hours and reset the staged
    # events.jsonl every time a production mesh ran past the broker's visibility window. A worker
    # that genuinely dies mid-job leaves that job to be marked FAILED by the stalled-job reaper
    # (application.maintenance.cleanup); it is NOT re-executed. Crash-recovery is traded away on
    # purpose - a clean failure is far cheaper than blindly redoing a multi-hour job.
    task_acks_late=False,
    task_reject_on_worker_lost=False,
)
