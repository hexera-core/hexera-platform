# Responsibility: Publish the pipeline queue's depth as a Cloud Monitoring metric the MIG autoscaler can read.
# Owns: the metric name, the value it reports, and the resource it reports against.
# Boundaries: it measures and publishes; it decides nothing about scaling and never touches a job.
"""Export Celery queue depth to Cloud Monitoring so a MIG can autoscale on it.

The autoscaler cannot see into Redis. It scales on a metric, so something has to
put the queue's depth where it can read it. That is all this does.

WHY PER-INSTANCE. The metric is written against this instance's `gce_instance`
resource, not a global one, because a MIG autoscaler using
`--custom-metric-utilization` averages a per-instance series across the group.
Reporting one global series would make the group scale on a number that does not
divide by instance count, and the autoscaler would never converge.

WHAT THE VALUE MEANS. Depth divided by instance count is what each instance is
carrying. The autoscaler is given a target for that, so a backlog of N jobs
across M workers settles at N/M per worker and the group grows until that is at
or under target. Worker concurrency is 1, so a target of 1.0 means "one queued
job per worker".

FAIL-OPEN. A monitoring write that fails must never stop the worker it runs
beside: a metric is an observation, and losing one is a gap in a graph, not an
incident. Every failure is logged and swallowed, and the next tick tries again.
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("queue_depth_exporter")

#: The metric the autoscaler is pointed at. Custom metrics live under this prefix by rule.
METRIC_TYPE = "custom.googleapis.com/hexera/queue_depth"

#: The Celery queue the pipeline's jobs are routed to (celery_app.py task_routes).
#: Celery on a Redis broker stores a queue as a Redis LIST under the queue's own name,
#: so its depth is the list's length.
QUEUE_NAME = os.environ.get("QUEUE_NAME", "simulation_jobs")

INTERVAL_SECONDS = int(os.environ.get("EXPORT_INTERVAL_SECONDS", "30"))


def _metadata(path: str) -> str:
    import urllib.request
    req = urllib.request.Request(
        f"http://metadata.google.internal/computeMetadata/v1/{path}",
        headers={"Metadata-Flavor": "Google"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.read().decode()


def queue_depth(redis_url: str) -> int:
    import redis
    client = redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
    try:
        return int(client.llen(QUEUE_NAME))
    finally:
        client.close()


def publish(depth: int, *, project_id: str, instance_id: str, zone: str) -> None:
    from google.cloud import monitoring_v3

    client = monitoring_v3.MetricServiceClient()
    now = time.time()
    series = monitoring_v3.TimeSeries()
    series.metric.type = METRIC_TYPE
    series.resource.type = "gce_instance"
    series.resource.labels["instance_id"] = instance_id
    series.resource.labels["zone"] = zone
    series.resource.labels["project_id"] = project_id
    series.points = [
        monitoring_v3.Point(
            interval=monitoring_v3.TimeInterval(
                end_time={"seconds": int(now), "nanos": int((now - int(now)) * 10**9)}
            ),
            value=monitoring_v3.TypedValue(double_value=float(depth)),
        )
    ]
    client.create_time_series(name=f"projects/{project_id}", time_series=[series])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    redis_url = os.environ["REDIS_URL"]
    project_id = _metadata("project/project-id")
    instance_id = _metadata("instance/id")
    # The metadata value is a full path; the resource label wants the bare zone name.
    zone = _metadata("instance/zone").rsplit("/", 1)[-1]
    logger.info("exporting %s for queue %r every %ss (instance %s, zone %s)",
                METRIC_TYPE, QUEUE_NAME, INTERVAL_SECONDS, instance_id, zone)

    while True:
        try:
            depth = queue_depth(redis_url)
            publish(depth, project_id=project_id, instance_id=instance_id, zone=zone)
            logger.info("queue_depth=%d published", depth)
        except Exception:  # noqa: BLE001 - see the FAIL-OPEN note in this module's docstring
            logger.warning("could not publish queue depth; retrying next tick", exc_info=True)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
