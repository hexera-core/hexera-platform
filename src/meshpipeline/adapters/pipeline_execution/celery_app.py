# Responsibility: Configure the Celery application: broker, queues, routes, priorities and the periodic schedule.
# Owns: the acknowledgement policy - a task is acked on receipt, so no long job is ever silently re-run.
# Owns: the QUEUE NAMES, and the one function that says which queues this application publishes to.
# Boundaries: configuration only; the task bodies live in celery.py and maintenance_tasks.py.
from __future__ import annotations

from celery import Celery

import meshpipeline.settings.providers as provcfg

# THE QUEUE NAMES, spelled once each. A queue is named in three places for every task that uses it -
# the decorator, the route, and the apply_async that publishes it - and before these constants
# existed each place spelled it as a literal. That is survivable for a rename, because a rename
# breaks loudly. It is not survivable for the mistake this file actually made: `geometry_look` was
# published by two literals here and consumed by one literal in docker-compose.yml, and no reader of
# either file could see that the deployed fleet consumed neither. One name per queue is what lets
# devtools/quality/check_queue_consumers.py ask the question at all.
QUEUE_SIMULATION = "simulation_jobs"
QUEUE_CLEANUP = "cleanup_tasks"
QUEUE_TRAINING_EXPORT = "training_export"
QUEUE_GEOMETRY_MEASUREMENT = "geometry_measurement"
QUEUE_GEOMETRY_LOOK = "geometry_look"

celery_app = Celery(
    "meshpipeline",
    broker=provcfg.REDIS_URL,
    backend=provcfg.REDIS_URL,
    include=["meshpipeline.adapters.pipeline_execution.celery",
             "meshpipeline.adapters.pipeline_execution.maintenance_tasks",
             "meshpipeline.adapters.pipeline_execution.geometry_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "worker.tasks.run_simulation": {"queue": QUEUE_SIMULATION},
        "tasks.cleanup.purge_expired_workspaces":  {"queue": QUEUE_CLEANUP},
        "tasks.cleanup.reap_stalled_jobs":         {"queue": QUEUE_CLEANUP},
        "tasks.cleanup.reconcile_orphan_artifacts": {"queue": QUEUE_CLEANUP},
        "tasks.export_conversation_data.export_conversation_sample": {"queue": QUEUE_TRAINING_EXPORT},
        # Its own queue: a measurement is seconds of native tessellation and it must never sit
        # behind a multi-hour mesh, nor delay one. A deployment that has not turned the feature on
        # never enqueues to it and the queue stays empty.
        "tasks.geometry.measure_source": {"queue": QUEUE_GEOMETRY_MEASUREMENT},
        # And its own again: a look is a render plus a provider call, tens of seconds, and nothing
        # is waiting on it. Behind its own queue it can back up without holding a measurement, which
        # somebody IS waiting on. Off by default, so with the feature off this queue stays empty.
        "tasks.geometry.look_at_source": {"queue": QUEUE_GEOMETRY_LOOK},
    },
    task_queue_max_priority={
        QUEUE_TRAINING_EXPORT: 9,
        # Above a mesh, because somebody is waiting on the conversation this answers and nobody is
        # watching a queued mesh start one minute sooner.
        QUEUE_GEOMETRY_MEASUREMENT: 7,
        # Below the measurement and below a mesh: a description is the last thing to run and the
        # first thing to give way.
        QUEUE_GEOMETRY_LOOK: 3,
        QUEUE_SIMULATION: 5,
        QUEUE_CLEANUP:   1,
    },
    beat_schedule={
        "purge-expired-workspaces": {
            "task": "tasks.cleanup.purge_expired_workspaces",
            "schedule": 3600.0,
            "options": {"queue": QUEUE_CLEANUP},
        },
        "reap-stalled-jobs": {
            "task": "tasks.cleanup.reap_stalled_jobs",
            "schedule": 600.0,
            "options": {"queue": QUEUE_CLEANUP},
        },
        # Uploaded geometry bytes past UPLOAD_RETENTION_DAYS. Hourly like the other byte-level
        # cleanups: the window is measured in days, so the scan only has to be regular.
        "purge-expired-geometry-sources": {
            "task": "tasks.cleanup.purge_expired_geometry_sources",
            "schedule": 3600.0,
            "options": {"queue": QUEUE_CLEANUP},
        },
        "reconcile-orphan-artifacts": {
            "task": "tasks.cleanup.reconcile_orphan_artifacts",
            "schedule": 600.0,
            "options": {"queue": QUEUE_CLEANUP},
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


def published_queues() -> frozenset[str]:
    """Every queue this application publishes work to, read off the LIVE configuration.

    THE QUESTION THIS EXISTS TO ANSWER. A queue nobody consumes is silent by nature: the publish
    succeeds, the broker accepts the message, the caller logs "queued", and nothing ever runs. That
    is how `geometry_look` came to be published by the platform and consumed only by
    docker-compose.yml, so on the deployed fleet every look was queued and dropped, and with it
    every place the Surveyor would have named. Nothing in the repository could state the set of
    queues that need a consumer, so nothing could check it.

    THREE SOURCES, because a queue reaches the broker by three routes and any one of them alone is a
    blind spot:

      1. `task_routes` - the router's opinion, which is what an `apply_async` with no explicit queue
         gets.
      2. `beat_schedule` - a periodic task's `options.queue`, which the router never sees.
      3. THE TASKS THEMSELVES - `@task(queue=...)` puts the name on the task class, so a task with a
         decorator queue and no route is published to a queue this file does not mention. Reading it
         off the registered task is following the call rather than trusting the configuration to
         describe it.

    The task modules are imported here (`import_default_modules` walks `conf.include`), because an
    unimported task is an unregistered task and a registry that is merely empty would make every
    caller of this function agree that nothing needs consuming. An import failure is raised, not
    swallowed: not knowing the answer must never read as "no queues".
    """
    routes = getattr(celery_app.conf, "task_routes", None)
    schedule = getattr(celery_app.conf, "beat_schedule", None)
    loader = getattr(celery_app, "loader", None)
    if routes is None or schedule is None or loader is None:
        # The hermetic unit tier replaces `celery` with a stub whose conf carries nothing, so this is
        # reachable from a test process and only from one. Refusing is the point: a stubbed app would
        # otherwise report that no queue needs a consumer, and a gate built on that answer would pass
        # on a deployment that drains nothing.
        raise RuntimeError(
            "this is not a real Celery configuration - task_routes, beat_schedule or the loader is "
            "absent, which is what the hermetic test tier's celery stub looks like. Ask this question "
            "in a process that imports the installed celery.")
    names: set[str] = set()
    for route in (routes or {}).values():
        queue = route.get("queue") if isinstance(route, dict) else None
        if queue:
            names.add(str(queue))
    for entry in (schedule or {}).values():
        queue = (entry.get("options") or {}).get("queue") if isinstance(entry, dict) else None
        if queue:
            names.add(str(queue))
    loader.import_default_modules()
    for name, task in celery_app.tasks.items():
        # celery's own bookkeeping tasks (celery.chord, celery.backend_cleanup, ...) run on the
        # default queue and are not this application's work.
        if str(name).startswith("celery."):
            continue
        queue = getattr(task, "queue", None)
        if queue:
            names.add(str(queue))
    if not names:
        raise RuntimeError(
            "published_queues() found no queue at all, which cannot be true of an application that "
            "routes five of them. Something imported an empty Celery app, and an empty answer here "
            "reads as 'no queue needs a consumer' - the exact silence this function exists to break.")
    return frozenset(names)
