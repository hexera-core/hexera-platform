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

# A BROKER CONNECTION THAT CAN NEVER HANG. Shared dev, 2026-10-16 06:58 UTC: Redis reset the
# geometry worker's connection (ConnectionError 104), the consumer logged "Trying to re-establish
# the connection..." and then waited on a socket that never answered - with no timeout, for three
# days, and no geometry check ran. Reproduced off-cloud by resetting a worker's connection and leaving
# the reconnect unanswered: celery's defaults reconnect into it and block forever, silently.
#
# So every socket the broker and the result backend open has a bound: a connect that does not
# complete fails, a read that gets no reply fails, TCP keepalive notices a peer that vanished
# without a reset, and redis-py pings an idle connection before trusting it. A failure is then an
# ordinary connection error, which celery retries and logs. BRPOP waits 1 s and the event loop only
# reads a readable socket, so 30 s is far past any healthy wait.
BROKER_SOCKET_TIMEOUT_SECONDS = 30
BROKER_CONNECT_TIMEOUT_SECONDS = 10
BROKER_HEALTH_CHECK_SECONDS = 25
#: After this many failed reconnects in a row (about six minutes of celery's 2 s -> 30 s backoff)
#: the worker gives up and EXITS, and its restart policy starts a fresh process - rather than
#: retrying for ever inside one that may be in a bad state. Celery's default is 100.
BROKER_CONNECTION_MAX_RETRIES = 20


def _keepalive_options() -> dict[int, int]:
    """Probe an idle connection after 60 s, every 15 s, and give up after 4 missed probes - so a
    peer that disappeared without a reset is found in about two minutes, not the kernel's two
    hours. Linux names these; elsewhere (a developer's Mac) the kernel defaults stand."""
    import socket
    wanted = {"TCP_KEEPIDLE": 60, "TCP_KEEPINTVL": 15, "TCP_KEEPCNT": 4}
    return {getattr(socket, name): value for name, value in wanted.items() if hasattr(socket, name)}


_SOCKET_OPTIONS = {
    "socket_timeout": BROKER_SOCKET_TIMEOUT_SECONDS,
    "socket_connect_timeout": BROKER_CONNECT_TIMEOUT_SECONDS,
    "socket_keepalive": True,
    "socket_keepalive_options": _keepalive_options(),
    "health_check_interval": BROKER_HEALTH_CHECK_SECONDS,
    "retry_on_timeout": True,
}

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
                              "visibility_timeout": BROKER_VISIBILITY_TIMEOUT_SECONDS,
                              **_SOCKET_OPTIONS},
    result_backend_transport_options={"global_keyprefix": provcfg.REDIS_KEY_PREFIX},
    # The broker connection's lifecycle (see _SOCKET_OPTIONS above): retry at startup as well as
    # after a loss - the Celery 6 default flips, and a worker that boots before Redis answers must
    # wait for it, not exit - and stop retrying after a bounded run so the process is replaced.
    broker_connection_retry=True,
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=BROKER_CONNECTION_MAX_RETRIES,
    broker_connection_timeout=BROKER_CONNECT_TIMEOUT_SECONDS,
    # The result backend is the same Redis, so it gets the same bounds on connect, keepalive and
    # idle health. Its read timeout keeps celery's 120 s: a caller may legitimately wait on a result.
    redis_socket_connect_timeout=BROKER_CONNECT_TIMEOUT_SECONDS,
    redis_socket_keepalive=True,
    redis_retry_on_timeout=True,
    redis_backend_health_check_interval=BROKER_HEALTH_CHECK_SECONDS,
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
