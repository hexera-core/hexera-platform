# Responsibility: Configure the Celery application: broker, queues, routes, priorities and the periodic schedule.
# Owns: the acknowledgement policy and the broker's visibility window - no long job is ever silently re-run by the broker.
# Boundaries: configuration only; the task bodies live in celery.py and maintenance_tasks.py.
from celery import Celery

import meshpipeline.settings.providers as provcfg
import meshpipeline.settings.runtime as rtcfg

#: How long Redis waits before it hands an UNACKNOWLEDGED message to another worker. The pipeline
#: task is acknowledged late (celery.py, so a busy worker reserves no second job), which makes this
#: the moment a still-running job would be delivered twice - so it must outlast any job that can
#: still be running: the pipeline's whole deadline, plus the reaper's hour to fail a run that
#: outlived it. Redis's own default is one hour, shorter than an ordinary mesh job.
#: A message that DOES come back after this finds its job terminal and is skipped at the claim.
BROKER_VISIBILITY_TIMEOUT_SECONDS = int(rtcfg.PIPELINE_TOTAL_TIMEOUT_SECONDS) + 2 * 3600

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
    broker_transport_options={"global_keyprefix": provcfg.REDIS_KEY_PREFIX,
                              "visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS},
    result_backend_transport_options={"global_keyprefix": provcfg.REDIS_KEY_PREFIX},
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "worker.tasks.run_simulation": {"queue": "simulation_jobs"},
        # THE GEOMETRY CHECK HAS A QUEUE OF ITS OWN. The scout draws an upload the moment it lands
        # and the naming runs when the user says what the part is - seconds of work, with a person
        # watching. On the simulation queue they sat behind whichever mesh job the fleet's one slot
        # was running, for as long as that job took (up to the pipeline deadline), while the
        # console said "drawing your part... in a few seconds". Every worker now drains this queue
        # from a slot the simulation queue cannot take (docker-compose.yml `worker-geometry`,
        # deploy/gcp/worker/startup.sh), so a check never waits behind a mesh.
        # tests/unit/deploy/test_geometry_check_queue.py holds the contract end to end.
        "worker.tasks.scout_geometry": {"queue": "geometry_checks"},
        "worker.tasks.name_geometry": {"queue": "geometry_checks"},
        "tasks.cleanup.purge_expired_workspaces":  {"queue": "cleanup_tasks"},
        "tasks.cleanup.reap_stalled_jobs":         {"queue": "cleanup_tasks"},
        "tasks.cleanup.reconcile_orphan_artifacts": {"queue": "cleanup_tasks"},
        "tasks.billing.report_pending_usage":       {"queue": "cleanup_tasks"},
        "tasks.export_conversation_data.export_conversation_sample": {"queue": "training_export"},
    },
    task_queue_max_priority={
        "training_export": 9,
        "geometry_checks": 5,
        "simulation_jobs": 5,
        "cleanup_tasks":   1,
    },
    # THE SCHEDULE, AND WHERE IT RUNS. The compose stack runs it with the `beat` service and a
    # `worker-utility` on the cleanup queue. A hosted deployment runs NO beat - the fleet is
    # simulation workers only (deploy/gcp/worker/startup.sh) - so runtime/maintenance_sweep.py
    # carries these entries as a scheduled Cloud Run job, and runtime/meter_sweep.py carries
    # report-pending-usage. An entry added here needs a hosted home too:
    # tests/unit/deploy/test_fleet_schedule_contract.py refuses one that has none.
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
        # THE METER SWEEP. Five minutes, which is far more often than a bill is drawn - the point is
        # not freshness but BACKLOG: each run is bounded, so a long provider outage drains over
        # several runs instead of one that has to carry the whole thing. A deployment with no
        # provider configured makes this a single query that finds nothing and returns.
        "report-pending-usage": {
            "task": "tasks.billing.report_pending_usage",
            "schedule": 300.0,
            "options": {"queue": "cleanup_tasks"},
        },
    },
    worker_prefetch_multiplier=1,
    # NO automatic redelivery by the broker. Tasks are acked on RECEIPT by default (acks_late=False)
    # so a long-running job is never silently re-queued and re-run from scratch - that wasted hours
    # and reset the staged events.jsonl every time a production mesh ran past the broker's
    # visibility window. The pipeline task alone acks late (celery.py) so a busy worker reserves no
    # second job, and the visibility window above outlasts the pipeline deadline so that cannot
    # redeliver a running job either. A worker that dies mid-job is not the broker's business: the
    # stalled-job reaper (application.maintenance.cleanup) re-runs the job ONCE, deliberately, and
    # fails it if it is lost again; a worker that is shut down on purpose hands its job back first
    # (application/worker_handoff.py).
    task_acks_late=False,
    task_reject_on_worker_lost=False,
)
