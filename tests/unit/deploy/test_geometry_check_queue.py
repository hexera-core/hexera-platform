# Responsibility: Pin the geometry check to a queue of its own, with a consumer a mesh job cannot occupy - in the
# routing, in the compose stack, on the worker fleet, and in the depth the autoscaler reads.
# Boundaries: static reads of the routing, the compose file, the startup script and the publisher; nothing is started.
from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
COMPOSE = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
STARTUP = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text(encoding="utf-8")
PUBLISHER = REPO / "deploy" / "gcp" / "worker" / "queue_depth_publisher.py"

GEOMETRY_TASKS = ("worker.tasks.scout_geometry", "worker.tasks.name_geometry")

# THE DEFECT THIS PINS. The scout draws an upload the moment it lands and the naming runs when the
# user says what the part is - seconds of work, with a person watching. Routed to the simulation
# queue, on a fleet that runs one celery slot per instance, a check waited behind whichever mesh
# job that slot was running, for as long as the job took (up to the pipeline deadline), while the
# console said "drawing your part... in a few seconds" (dead-end audit, 2026-09-28).


def _routes() -> dict:
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
    return celery_app.conf.task_routes


# the routing


def test_the_geometry_check_is_routed_to_its_own_queue():
    routes = _routes()
    for task in GEOMETRY_TASKS:
        assert routes[task] == {"queue": "geometry_checks"}, (
            f"{task} is routed to {routes.get(task)}; on the simulation queue an upload waits "
            "behind whichever mesh job the worker's slot is running")


def test_moving_the_check_moved_nothing_else():
    # The run's queue name is on the wire in every in-flight job, and the cleanup queue is what the
    # compose stack's worker-utility drains - neither may move as a side effect.
    routes = _routes()
    assert routes["worker.tasks.run_simulation"] == {"queue": "simulation_jobs"}
    for task in ("tasks.cleanup.purge_expired_workspaces", "tasks.cleanup.reap_stalled_jobs",
                 "tasks.cleanup.reconcile_orphan_artifacts", "tasks.billing.report_pending_usage"):
        assert routes[task] == {"queue": "cleanup_tasks"}, task
    assert routes["tasks.export_conversation_data.export_conversation_sample"] == {"queue": "training_export"}
    assert {r["queue"] for r in routes.values()} == {
        "simulation_jobs", "geometry_checks", "cleanup_tasks", "training_export"}


def test_every_routed_queue_declares_its_priority_ceiling():
    from meshpipeline.adapters.pipeline_execution.celery_app import celery_app
    routed = {r["queue"] for r in celery_app.conf.task_routes.values()}
    missing = routed - set(celery_app.conf.task_queue_max_priority)
    assert missing == set(), f"queues routed to but given no priority ceiling: {sorted(missing)}"


# the compose stack


def _queues_of(service: str) -> set[str]:
    command = " ".join(str(COMPOSE["services"][service]["command"]).split())
    found = re.search(r"--queues\s+(\S+)", command)
    assert found, f"{service} names no --queues"
    return set(found.group(1).split(","))


def test_compose_runs_a_geometry_worker_on_the_geometry_queue_alone():
    assert "worker-geometry" in COMPOSE["services"], "no compose service drains geometry_checks"
    assert _queues_of("worker-geometry") == {"geometry_checks"}
    svc = COMPOSE["services"]["worker-geometry"]
    # The SAME image as the simulation worker: reading CAD and drawing it need the mesh toolchain
    # the API image does not carry.
    assert svc["build"]["target"] == COMPOSE["services"]["worker"]["build"]["target"] == "pipeline"
    assert svc["env_file"] == [".env"]
    assert "${GEOMETRY_CHECK_WORKER_CONCURRENCY}" in str(svc["command"]), (
        "the slot count is a catalogued setting, not a number in the compose file")
    # It reads the upload from the object store and writes its pictures back there.
    assert "minio" in svc["depends_on"] and "redis" in svc["depends_on"]


def test_no_other_worker_takes_the_geometry_queue():
    # A simulation worker that also consumed geometry_checks would let a mesh job occupy the slot a
    # check needs - the arrangement being replaced, with a longer queue list.
    assert _queues_of("worker") == {"simulation_jobs"}
    assert "geometry_checks" not in _queues_of("worker-utility")


def test_the_slot_count_is_a_catalogued_compose_setting():
    from meshpipeline.settings import inventory
    var = inventory.get("GEOMETRY_CHECK_WORKER_CONCURRENCY")
    assert var.consumer == "compose" and var.kind == "int" and var.exposure == "template"
    assert int(var.default) >= 1


# the worker fleet


def _container_block(name: str) -> str:
    start = STARTUP.index(f"docker run -d --name {name} ")
    end = STARTUP.find("\n\n", start)
    return STARTUP[start:end if end != -1 else None]


def test_the_fleet_runs_a_geometry_container_beside_the_simulation_container():
    sim = _container_block("hexera-worker")
    geo = _container_block("hexera-geometry-worker")
    assert "--queues simulation_jobs" in sim and "geometry_checks" not in sim
    assert "--queues geometry_checks" in geo and "simulation_jobs" not in geo
    # The fleet's capacity contract for JOBS is unchanged: one per instance, so a job's cost is a
    # whole instance and the autoscaler's one-instance-per-queued-job arithmetic still holds.
    assert "--concurrency 1" in sim
    # Same image, same settings, same mounts - the check reads CAD with the same toolchain.
    for needle in ('"${WORKER_IMAGE}"', "--env-file /etc/hexera/worker.env",
                   "-v /var/lib/hexera/workspaces:/srv/workspaces", "-v /var/lib/hexera/data:/srv/data",
                   "--restart always"):
        assert needle in geo, f"the geometry container lacks {needle}"


def test_the_geometry_container_is_replaced_on_every_boot_after_its_mounts_are_owned():
    assert STARTUP.index("docker rm -f hexera-geometry-worker") < STARTUP.index(
        "docker run -d --name hexera-geometry-worker")
    assert STARTUP.index("chown ") < STARTUP.index("docker run -d --name hexera-geometry-worker")


# the depth the autoscaler reads


def _load_publisher(monkeypatch, env: dict[str, str]):
    for key in ("QUEUE_NAME", "QUEUE_NAMES", "METRIC_NAMESPACE", "METRIC_LOCATION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("queue_depth_publisher_under_test", PUBLISHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_publisher_measures_the_geometry_queue_beside_the_scaling_queue(monkeypatch):
    mod = _load_publisher(monkeypatch, {"QUEUE_NAME": "dev-x:simulation_jobs",
                                        "QUEUE_NAMES": "dev-x:simulation_jobs,dev-x:geometry_checks"})
    assert mod.QUEUE_NAMES == ["dev-x:simulation_jobs", "dev-x:geometry_checks"]


def test_the_scaling_queue_is_always_measured_and_measured_first(monkeypatch):
    # A QUEUE_NAMES that forgot the scaling queue must not silence the one series the autoscaler
    # reads; unset, the publisher does exactly what it did before the second queue existed.
    mod = _load_publisher(monkeypatch, {"QUEUE_NAME": "simulation_jobs", "QUEUE_NAMES": "geometry_checks"})
    assert mod.QUEUE_NAMES == ["simulation_jobs", "geometry_checks"]
    mod = _load_publisher(monkeypatch, {"QUEUE_NAME": "simulation_jobs"})
    assert mod.QUEUE_NAMES == ["simulation_jobs"]


def test_one_write_carries_one_series_per_queue_told_apart_by_task_id(monkeypatch):
    mod = _load_publisher(monkeypatch, {})
    series = mod.build_time_series({"simulation_jobs": 3, "geometry_checks": 1}, project_id="p",
                                   namespace="dev", location="zone-a", end_time="2026-09-28T00:00:00Z")
    by_queue = {s["resource"]["labels"]["task_id"]: s for s in series}
    assert set(by_queue) == {"simulation_jobs", "geometry_checks"}
    assert by_queue["simulation_jobs"]["points"][0]["value"]["doubleValue"] == 3.0
    assert by_queue["geometry_checks"]["points"][0]["value"]["doubleValue"] == 1.0
    for s in series:
        # Everything but task_id is identical, which is what lets ONE autoscaler filter select
        # exactly one of them (create-queue-depth-publisher.sh names task_id = QUEUE_NAME).
        assert s["metric"]["type"] == mod.METRIC_TYPE
        assert s["resource"]["type"] == "generic_task"
        assert s["resource"]["labels"]["namespace"] == "dev"
        assert s["resource"]["labels"]["location"] == "zone-a"
        assert s["resource"]["labels"]["job"] == "queue-depth"
        assert s["points"][0]["interval"]["endTime"] == "2026-09-28T00:00:00Z"


def test_one_connection_measures_every_queue(monkeypatch):
    # The cold VPC attach is paid per connection (see the timeouts in the program), so the second
    # queue must be a second LLEN on the open connection, not a second attach.
    mod = _load_publisher(monkeypatch, {})
    calls: list[str] = []

    class FakeClient:
        def llen(self, queue):
            calls.append(queue)
            return {"a": 2, "b": 0}[queue]

        def close(self):
            calls.append("close")

    class FakeError(Exception):
        pass

    fake_redis = types.SimpleNamespace(
        from_url=lambda *a, **k: FakeClient(),
        exceptions=types.SimpleNamespace(TimeoutError=FakeError, ConnectionError=FakeError))
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    assert mod.queue_depths("redis://broker", ["a", "b"]) == {"a": 2, "b": 0}
    assert calls == ["a", "b", "close"]
    assert mod.queue_depth("redis://broker", "a") == 2


# the checks count towards the fleet's demand (1 Nov)
#
# Shared dev, 1 Nov: two checks ran at a time and the autoscaler never heard of them - its demand counted the
# simulation queue alone - so uploads waited on two slots however long the line grew, and a fleet at zero
# would never have woken for one.


def test_the_demand_is_the_larger_of_the_jobs_and_the_instances_the_checks_need(monkeypatch):
    mod = _load_publisher(monkeypatch, {"QUEUE_NAME": "simulation_jobs"})
    # nothing but checks: five checks at two slots an instance need three instances
    assert mod.demand(0, 0, 3, 2, slots=2, jobs_per_instance=1) == 3
    # a running mesh job and two running checks share one machine: the larger, never the sum
    assert mod.demand(0, 1, 0, 2, slots=2, jobs_per_instance=1) == 1
    assert mod.demand(2, 1, 1, 0, slots=2, jobs_per_instance=1) == 3
    # said in the autoscaler's unit: an instance worth two jobs needs demand 2 for each instance of checks
    assert mod.demand(0, 0, 1, 0, slots=2, jobs_per_instance=2) == 2
    assert mod.demand(0, 0, 0, 0) == 0


def test_the_running_checks_are_counted_from_the_key_every_running_check_keeps(monkeypatch):
    from meshpipeline.adapters.pipeline_execution import geometry_running as gr

    mod = _load_publisher(monkeypatch, {"QUEUE_NAME": "simulation_jobs"})
    # restated in the publisher (it may import nothing of the application): the two must agree
    assert gr.RUNNING_CHECK_KEY_TEMPLATE.format(task_id="*") == mod.RUNNING_CHECK_KEY_PATTERN

    class _Client:
        def scan_iter(self, match, count):
            import fnmatch
            keys = ["dev-x:fleet:geometry-running:geometry-check-a", "dev-x:fleet:geometry-running:geometry-naming-a",
                    "fleet:geometry-running:other-env", "dev-x:fleet:consumers:geometry_checks:n1"]
            return iter([k for k in keys if fnmatch.fnmatchcase(k, match.replace("\\", ""))])
    assert mod.running_checks(_Client(), "dev-x:") == 2
    assert gr.count_running(_Client(), "dev-x:") == 2


def test_the_publisher_is_told_the_check_slots_the_fleet_actually_runs():
    geo = _container_block("hexera-geometry-worker")
    slots = int(re.search(r"--concurrency (\d+)", geo).group(1))
    script = (REPO / "deploy" / "gcp" / "scripts" / "create-queue-depth-publisher.sh").read_text(encoding="utf-8")
    assert f'GEOMETRY_SLOTS_PER_INSTANCE="${{GEOMETRY_SLOTS_PER_INSTANCE:-{slots}}}"' in script
    manifest = (REPO / "deploy" / "gcp" / "cloud-run" / "queue-depth-job.yaml").read_text(encoding="utf-8")
    for var in ("GEOMETRY_QUEUE_NAME", "GEOMETRY_SLOTS_PER_INSTANCE", "JOBS_PER_INSTANCE"):
        assert f"- name: {var}" in manifest and f"${{{var}}}" in manifest
        assert var in script


def test_a_geometry_task_is_marked_running_while_it_runs_and_unmarked_after():
    from meshpipeline.adapters.pipeline_execution import geometry_running as gr

    class _Client:
        def __init__(self):
            self.keys: dict = {}

        def set(self, key, value, ex=None):
            self.keys[key] = ex

        def delete(self, key):
            self.keys.pop(key, None)
    client = _Client()
    with gr.running("geometry-check-s1", client_factory=lambda: client, prefix="dev-x:"):
        assert client.keys == {"dev-x:fleet:geometry-running:geometry-check-s1": gr.RUNNING_TTL_S}
    assert client.keys == {}


def test_a_queued_check_is_found_in_the_queue_with_the_number_ahead_of_it():
    import json as _json

    from meshpipeline.adapters.pipeline_execution import geometry_running as gr

    class _Client:
        # the Redis transport pushes on the left: the right end is taken first
        items = [_json.dumps({"headers": {"id": t}}) for t in ("c", "mine", "b", "a")]

        def exists(self, key):
            return 0

        def llen(self, key):
            return len(self.items)

        def lrange(self, key, start, end):
            return self.items[start:]
    assert gr.queue_state("mine", client_factory=_Client, prefix="") == {"ahead": 2}
    assert gr.queue_state("gone", client_factory=_Client, prefix="") is None
