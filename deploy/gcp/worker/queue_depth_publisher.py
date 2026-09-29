# Responsibility: Publish each queue's depth, and the pipeline queue's demand (queued + running) the MIG autoscaler scales on, as per-group series.
# Owns: the metric names, the monitored resource that identifies each series, and the values they report.
# Boundaries: it measures and publishes once per invocation; it decides nothing about scaling and never touches a job.
"""Publish Celery queue depth to Cloud Monitoring as a single per-group time series.

The autoscaler cannot see into Redis. It scales on a metric, so something has to
put the queue's depth where it can read it. That is all this does.

WHY IT IS NOT ON THE FLEET. The previous exporter ran as a systemd unit beside
each worker, which made the fleet the only writer of the number that wakes the
fleet: at zero instances nobody reports a backlog and the group can never come
back. Decision 4 of the build-out plan is scale-to-zero in dev, so the publisher
had to move off the instances it scales. It now runs as a Cloud Run job that
Cloud Scheduler invokes, and it publishes whether or not any worker exists.

WHY ONE SERIES, NOT ONE PER INSTANCE. Every instance used to publish the SAME
group-wide total against its own `gce_instance` resource, and the autoscaler
averaged those identical series - so a backlog of one job read as "one job per
worker" no matter how many workers there were, and the group jumped to max on any
backlog instead of scaling proportionally. A single writer has no instance to
attribute the number to, so it publishes against a `generic_task` resource that
names the deployment and the queue. The autoscaler reads it with
`--stackdriver-metric-single-instance-assignment`, which divides the total by the
work one instance can carry - the arithmetic the per-instance arrangement could
not do.

WHICH QUEUES. QUEUE_NAME is the queue the fleet is SIZED on - the simulation
queue, where one queued job is one instance's worth of work. QUEUE_NAMES lists
every queue one run measures, one series each, told apart by the `task_id` label:
the geometry-check queue rides along so a backlog of checks is visible on the same
metric, but the autoscaler's filter names QUEUE_NAME alone. A check is seconds of
work and an instance takes minutes to arrive, so a fleet sized on that queue would
add machines to a backlog that had already drained; every instance carries slots
for it instead (deploy/gcp/worker/startup.sh).

WHAT THE VALUE MEANS. The length of the Redis list Celery routes a queue's
tasks to: work QUEUED, not work in flight. A task a worker has already picked up
is out of the list, so a lone running job reports depth 0. That is the right
signal for scaling UP and the WRONG one for scaling down.

WHAT THE AUTOSCALER READS INSTEAD: DEMAND. Shared dev, 2026-09-29: the fleet
scaled in three times in two hours (05:41, 06:39, 07:32 UTC), each time to its
floor of 1, because a burst of queued jobs had drained and the depth read 0 -
while three mesh jobs were running on the VMs it deleted. Every one of them was
failed half an hour later as "worker lost". So the scaling queue gets a second
metric, `worker_demand` = queued + RUNNING, and that is what the autoscaler's
filter names (create-queue-depth-publisher.sh). A running job is counted by its
execution fence - the Redis mirror of its PostgreSQL claim, one key per owned
job, kept alive by the owner's heartbeat and gone within one lease of the owner
going away (src/meshpipeline/persistence/lease.py) - so a VM whose worker died
stops being counted without anyone having to notice. The depth series stays
exactly as it was: the admin console charts it, and it still means "queued".

WHY IT FAILS LOUDLY. The old exporter swallowed every error because it ran in a
loop beside a worker and the next tick would try again. This runs once per
invocation, so a swallowed failure would be a silent success: the job would go
green while the metric went stale and the fleet stopped scaling. Every failure
exits non-zero, which Cloud Scheduler records and retries.

WHY THE STANDARD LIBRARY. Deployment promotes the validated application image and
never builds one, so this program travels to the job in its spec rather than
inside the image - and may only use what that image already has: python, the
redis client, and urllib against the metadata server. `google-cloud-monitoring`
is not in requirements/runtime.txt, so the metric is written over the Monitoring
REST API directly.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import urllib.request

logger = logging.getLogger("queue_depth_publisher")

#: The queue depth per queue - what is WAITING. Charted by the admin console. Custom metrics live
#: under this prefix by rule.
METRIC_TYPE = "custom.googleapis.com/hexera/queue_depth"

#: What the autoscaler is pointed at: the scaling queue's jobs that need a worker NOW - queued plus
#: running. Sized on this, the group never shrinks below the number of jobs in flight.
DEMAND_METRIC_TYPE = "custom.googleapis.com/hexera/worker_demand"

#: The key every running job's execution fence lives under, inside the deployment's keyspace:
#: `<REDIS_KEY_PREFIX>jobs:<job id>:eventfence` (src/meshpipeline/events/channels.py
#: fence_key_for). Restated rather than imported because this program may use only the standard
#: library and the redis client; tests/unit/deploy/test_worker_scale_in.py holds the two together.
FENCE_KEY_PATTERN = "jobs:*:eventfence"

#: The deployment's Redis keyspace (Celery's global_keyprefix and every other key). Empty on shared
#: dev and production; `dev-<slug>:` in a personal environment sharing the same instance.
REDIS_KEY_PREFIX = os.environ.get("REDIS_KEY_PREFIX", "")

#: The Celery queue the fleet is SIZED on (celery_app.py task_routes): the simulation queue, where
#: one queued job is one instance's worth of work. Celery on a Redis broker stores a queue as a
#: Redis LIST under the queue's own name, so its depth is the list's length.
QUEUE_NAME = os.environ.get("QUEUE_NAME", "simulation_jobs")


def _queues(scaling_queue: str, listed: str) -> list[str]:
    # The scaling queue FIRST and ALWAYS, whatever the list says: a QUEUE_NAMES that forgot it must
    # not silence the one series the autoscaler reads. Ordered and de-duplicated after that.
    names = [scaling_queue] + [q.strip() for q in listed.split(",") if q.strip()]
    return list(dict.fromkeys(names))


#: Every queue this run measures, comma-separated in the environment: QUEUE_NAME and whatever
#: rides along with it - the geometry-check queue, whose backlog is worth seeing where the fleet's
#: is. One series each, told apart by the `task_id` label below; the autoscaler's filter names
#: QUEUE_NAME's and reads nothing else, because a check is seconds of work and an instance takes
#: minutes to arrive - a fleet sized on that queue would add machines to a backlog that had already
#: drained (deploy/gcp/worker/startup.sh gives every instance slots for it instead). Unset, this
#: is QUEUE_NAME alone: exactly what the publisher did before the second queue existed.
QUEUE_NAMES = _queues(QUEUE_NAME, os.environ.get("QUEUE_NAMES", ""))

#: The `generic_task` labels that IDENTIFY the series. The autoscaler's filter has to select
#: exactly one time series, and these four are what it selects on, so they are deployment state
#: rather than defaults: two deployments in one project publish two distinct series, and one
#: deployment's two queues publish two more.
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE", "")
METRIC_LOCATION = os.environ.get("METRIC_LOCATION", "")
METRIC_JOB = "queue-depth"

_METADATA_ROOT = "http://metadata.google.internal/computeMetadata/v1/"


def _metadata(path: str) -> str:
    req = urllib.request.Request(_METADATA_ROOT + path, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - fixed metadata host
        return resp.read().decode()


def _access_token() -> str:
    # The job's own runtime identity, from the metadata server. No key material is placed in the
    # spec, in an environment variable, or on any disk this program can reach.
    token = json.loads(_metadata("instance/service-accounts/default/token"))["access_token"]
    return str(token)


# HOW LONG A COLD CONNECTION IS ALLOWED TO TAKE, and why it is not 5 seconds.
#
# This job runs on Cloud Run with Direct VPC egress, and the network interface is attached when the
# task starts - the first connection out of a cold task pays for that. Measured against Memorystore
# over private service access, from this job's own image, service account and egress settings: 11.2
# seconds to complete the TCP handshake, then an immediate +PONG. The 5 seconds this used to allow
# was less than half of it, so the publisher failed roughly four runs in five and succeeded only
# when the interface happened to attach quickly.
#
# It went unnoticed because the environment it has always run in does not have this cost: shared
# dev's Memorystore predates the provisioning script, was built in DIRECT_PEERING mode, and its
# publisher runs every two minutes and is never cold.
#
# 30s is measured headroom over 11.2s, not a guess, and it is bounded by the job's own task
# timeout. READ timeout stays low: once connected, an LLEN that takes seconds means something is
# wrong and waiting longer will not fix it. The two are separated for that reason.
REDIS_CONNECT_TIMEOUT_SECONDS = 30
REDIS_READ_TIMEOUT_SECONDS = 5
REDIS_CONNECT_ATTEMPTS = 3


def queue_depths(redis_url: str, queues: list[str] | None = None) -> dict[str, int]:
    import redis

    queues = list(queues if queues is not None else QUEUE_NAMES)
    # RETRIED, because the cost above is paid once per cold task and is variable rather than fixed.
    # A generous timeout alone leaves the run dependent on a single attempt; three attempts make an
    # unusually slow attach a slow success instead of a failed deploy. The last failure is re-raised
    # so a genuinely unreachable broker still fails loudly, with the real exception.
    last: Exception | None = None
    for attempt in range(1, REDIS_CONNECT_ATTEMPTS + 1):
        client = redis.from_url(
            redis_url,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_READ_TIMEOUT_SECONDS,
        )
        try:
            # ONE connection for every queue. The cold attach is paid per connection, so a second
            # queue is a second LLEN on the connection already open, not a second attach.
            return {queue: int(client.llen(queue)) for queue in queues}
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as exc:
            last = exc
            logger.warning(
                "redis connect attempt %d/%d failed: %s: %s",
                attempt, REDIS_CONNECT_ATTEMPTS, type(exc).__name__, exc,
            )
        finally:
            client.close()
    assert last is not None
    raise last


def queue_depth(redis_url: str, queue: str = QUEUE_NAME) -> int:
    return queue_depths(redis_url, [queue])[queue]


def _glob_escape(text: str) -> str:
    # A prefix is a literal; Redis MATCH is a glob. Escaped, a prefix holding `*`, `?` or `[`
    # still matches only its own keys.
    return "".join("\\" + ch if ch in "*?[]\\" else ch for ch in text)


def fence_pattern(prefix: str = "") -> str:
    return _glob_escape(prefix) + FENCE_KEY_PATTERN


def running_jobs(client, prefix: str = "") -> int:
    """How many jobs a worker owns right now: one execution fence per owned job.

    SCAN, never KEYS: this runs against a Memorystore instance other environments share, and KEYS
    would block it for every key it holds. A key seen twice while the keyspace resizes under the
    scan is counted once."""
    return len(set(client.scan_iter(match=fence_pattern(prefix), count=500)))


def measure(redis_url: str, queues: list[str] | None = None,
            prefix: str | None = None) -> tuple[dict[str, int], int]:
    """(depth per queue, running jobs) on ONE connection - the cold attach is paid once."""
    import redis

    queues = list(queues if queues is not None else QUEUE_NAMES)
    prefix = REDIS_KEY_PREFIX if prefix is None else prefix
    last: Exception | None = None
    for attempt in range(1, REDIS_CONNECT_ATTEMPTS + 1):
        client = redis.from_url(
            redis_url,
            socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=REDIS_READ_TIMEOUT_SECONDS,
        )
        try:
            depths = {queue: int(client.llen(queue)) for queue in queues}
            return depths, running_jobs(client, prefix)
        except (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError) as exc:
            last = exc
            logger.warning(
                "redis connect attempt %d/%d failed: %s: %s",
                attempt, REDIS_CONNECT_ATTEMPTS, type(exc).__name__, exc,
            )
        finally:
            client.close()
    assert last is not None
    raise last


def build_time_series(depths: dict[str, int], *, project_id: str, namespace: str, location: str,
                      end_time: str) -> list[dict]:
    # One series per queue. They differ in `task_id` and nothing else, which is what lets one
    # autoscaler filter select exactly one of them and a dashboard show them side by side.
    return [{
        "metric": {"type": METRIC_TYPE},
        "resource": {
            "type": "generic_task",
            "labels": {
                "project_id": project_id,
                "location": location,
                "namespace": namespace,
                "job": METRIC_JOB,
                "task_id": queue,
            },
        },
        "metricKind": "GAUGE",
        "valueType": "DOUBLE",
        "points": [{
            "interval": {"endTime": end_time},
            "value": {"doubleValue": float(depth)},
        }],
    } for queue, depth in depths.items()]


def build_demand_series(queued: int, running: int, *, queue: str, project_id: str,
                        namespace: str, location: str, end_time: str) -> dict:
    # The same resource labels as the scaling queue's depth series - the autoscaler's filter names
    # them exactly as before - under the demand metric type, valued queued + running.
    return {
        "metric": {"type": DEMAND_METRIC_TYPE},
        "resource": {
            "type": "generic_task",
            "labels": {
                "project_id": project_id,
                "location": location,
                "namespace": namespace,
                "job": METRIC_JOB,
                "task_id": queue,
            },
        },
        "metricKind": "GAUGE",
        "valueType": "DOUBLE",
        "points": [{
            "interval": {"endTime": end_time},
            "value": {"doubleValue": float(int(queued) + int(running))},
        }],
    }


def publish(depths: dict[str, int], *, project_id: str, namespace: str, location: str,
            running: int | None = None) -> None:
    # ONE write for every series: the same timestamp on each, and one request where a request per
    # series would multiply the chance of two writes to one series arriving out of order.
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    end_time = now.isoformat().replace("+00:00", "Z")
    series = build_time_series(depths, project_id=project_id, namespace=namespace,
                               location=location, end_time=end_time)
    if running is not None:
        series.append(build_demand_series(
            depths.get(QUEUE_NAME, 0), running, queue=QUEUE_NAME, project_id=project_id,
            namespace=namespace, location=location, end_time=end_time))
    body = {"timeSeries": series}
    req = urllib.request.Request(
        f"https://monitoring.googleapis.com/v3/projects/{project_id}/timeSeries",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_access_token()}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed API host
        resp.read()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    redis_url = os.environ["REDIS_URL"]
    namespace = METRIC_NAMESPACE or os.environ["METRIC_NAMESPACE"]   # KeyError names what is missing
    location = METRIC_LOCATION or os.environ["METRIC_LOCATION"]
    project_id = _metadata("project/project-id")

    depths, running = measure(redis_url)
    publish(depths, project_id=project_id, namespace=namespace, location=location,
            running=running)
    for queue, depth in depths.items():
        logger.info("queue_depth=%d published for %s/%s (queue %s)", depth, namespace, location, queue)
    logger.info("worker_demand=%d published for %s/%s (queue %s: %d queued + %d running)",
                depths.get(QUEUE_NAME, 0) + running, namespace, location, QUEUE_NAME,
                depths.get(QUEUE_NAME, 0), running)


if __name__ == "__main__":
    main()
