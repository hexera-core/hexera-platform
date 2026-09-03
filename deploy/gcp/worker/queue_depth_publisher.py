# Responsibility: Publish the pipeline queue's depth as ONE per-group series the MIG autoscaler scales on.
# Owns: the metric name, the monitored resource that identifies the series, and the value it reports.
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

WHAT THE VALUE MEANS. The length of the Redis list Celery routes this queue's
tasks to: work QUEUED, not work in flight. A task a worker has already picked up
is out of the list, so a lone running job reports depth 0. That is the right
signal for scaling UP and an incomplete one for scaling DOWN - a group whose
minimum is zero must not be allowed to delete an instance that is mid-job on the
strength of this number alone (see the scale-in contract in the build-out plan,
item 6).

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

#: The metric the autoscaler is pointed at. Custom metrics live under this prefix by rule.
METRIC_TYPE = "custom.googleapis.com/hexera/queue_depth"

#: The Celery queue the pipeline's jobs are routed to (celery_app.py task_routes).
#: Celery on a Redis broker stores a queue as a Redis LIST under the queue's own name,
#: so its depth is the list's length.
QUEUE_NAME = os.environ.get("QUEUE_NAME", "simulation_jobs")

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


def queue_depth(redis_url: str, queue: str = QUEUE_NAME) -> int:
    import redis
    client = redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
    try:
        return int(client.llen(queue))
    finally:
        client.close()


def publish(depth: int, *, project_id: str, namespace: str, location: str, queue: str) -> None:
    now = datetime.datetime.now(datetime.UTC).replace(microsecond=0)
    body = {
        "timeSeries": [{
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
                "interval": {"endTime": now.isoformat().replace("+00:00", "Z")},
                "value": {"doubleValue": float(depth)},
            }],
        }]
    }
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

    depth = queue_depth(redis_url)
    publish(depth, project_id=project_id, namespace=namespace, location=location, queue=QUEUE_NAME)
    logger.info("queue_depth=%d published for %s/%s (queue %s)", depth, namespace, location, QUEUE_NAME)


if __name__ == "__main__":
    main()
