# Responsibility: Prove every queue the application publishes to is drained by a worker in every deployment target.
# Boundaries: it parses the deployment files as text and drives the gate in a subprocess; it starts no worker and calls no cloud.
#
# THE DEFECT. The Surveyor's look was published to `geometry_look` and the only worker fleet a real
# deployment starts ran `--queues simulation_jobs`. The one consumer of `geometry_look` anywhere in
# the repository was a docker-compose service, which exists on a laptop. So on the deployed platform
# the look was queued and never run, and every place the Surveyor would have named came from the look.
#
# WHY IT NEEDED A TEST RATHER THAN CARE. An undrained queue raises NOTHING. The publish succeeds, the
# broker holds the message, the caller logs "queued", and the only evidence is a row that never
# changes. There is no exception, no metric and no failing assertion anywhere in that story, which is
# why the shortest word in startup.sh outlived eleven audits of the code around it.
#
# WHY THE GATE RUNS IN A SUBPROCESS. This tier replaces `celery` with a stub (tests/unit/conftest.py),
# so in THIS process there is no router, no beat schedule and no task registry to read. A test that
# asked the stub which queues are published would be told "none" and would pass against a fleet that
# drains nothing, which is precisely the shape of defect this file is about. So the half that needs the
# real configuration is asked of a fresh interpreter that imports the installed celery, and a gate that
# cannot run there FAILS this test rather than skipping it.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "devtools" / "quality"))

import check_queue_consumers as gate  # noqa: E402 - the gate lives outside the package

GATE = REPO / "devtools" / "quality" / "check_queue_consumers.py"


def _run_gate(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(GATE), *args], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300, check=False)


def _target(name: str):
    found = [t for t in gate.targets() if t.name == name]
    assert found, f"no deployment target called {name!r}; the targets are {[t.name for t in gate.targets()]}"
    return found[0]


def _consumed(name: str) -> frozenset[str]:
    target = _target(name)
    for path in target.files:
        target.workers.extend(gate._workers_in(path))
    return target.consumed


# ---------------------------------------------------------------- what the application publishes

def _published() -> list[str]:
    """The published queues, asked of an interpreter where the Celery app is real."""
    program = ("from meshpipeline.adapters.pipeline_execution.queues import published_queues;"
               "print(' '.join(sorted(published_queues())))")
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300, check=False)
    assert done.returncode == 0, f"published_queues() would not run:\n{done.stderr}"
    return done.stdout.split()


def test_the_published_queues_are_read_off_the_live_configuration():
    published = _published()
    assert "geometry_look" in published, "the look is published somewhere or the Surveyor has no vision half"
    assert "geometry_measurement" in published
    assert "simulation_jobs" in published


def test_all_three_places_a_queue_can_be_named_are_read():
    # The router is not the whole story. `@task(queue=...)` puts the name on the task class, so a task
    # with a decorator queue and no route would be invisible to a check that read task_routes alone -
    # and `purge_expired_geometry_sources` is the mirror case, a beat entry with no route. Between them
    # all three sources are exercised by real tasks rather than by a fixture.
    program = (
        "from meshpipeline.adapters.pipeline_execution.celery_app import celery_app;"
        "celery_app.loader.import_default_modules();"
        "print(celery_app.tasks['tasks.geometry.look_at_source'].queue,"
        " 'tasks.cleanup.purge_expired_geometry_sources' in (celery_app.conf.task_routes or {}),"
        " celery_app.conf.beat_schedule['purge-expired-geometry-sources']['options']['queue'])")
    done = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                          cwd=str(REPO), timeout=300, check=False)
    assert done.returncode == 0, done.stderr
    decorator_queue, routed, beat_queue = done.stdout.split()
    assert decorator_queue == "geometry_look", "the look's queue is on its decorator"
    assert routed == "False", "this task has no route, so only the beat schedule names its queue"
    assert beat_queue == "cleanup_tasks"


def test_published_queues_refuses_to_answer_with_an_empty_set():
    # The blind spot this whole file exists to close: a gate that compared two empty sets would pass on
    # a deployment that drains nothing. This tier's celery stub is exactly that situation, so asking it
    # here is the real case and not a contrived one.
    from meshpipeline.adapters.pipeline_execution.queues import published_queues

    with pytest.raises(RuntimeError, match="not a real Celery configuration"):
        published_queues()


# ---------------------------------------------------------------- the gate, against the real celery

def test_the_gate_passes_on_this_tree_with_the_real_celery():
    done = _run_gate()
    assert done.returncode == 0, (
        "the queue gate does not pass on this tree, or could not run at all. Exit 2 means it could "
        f"not read the published queues, which is a failure and not a pass:\n{done.stdout}{done.stderr}")
    # The report has to NAME the queues, or a future reader cannot tell a real pass from a vacuous one.
    for queue in ("geometry_look", "geometry_measurement", "simulation_jobs", "cleanup_tasks",
                  "training_export"):
        assert queue in done.stdout, f"{queue} is not in the gate's report:\n{done.stdout}"


def test_the_gate_reports_the_fleet_draining_the_surveyor_queues():
    done = _run_gate()
    fleet = [line for line in done.stdout.splitlines() if "startup.sh" in line]
    assert fleet, f"the gate read no worker out of the fleet's startup script:\n{done.stdout}"
    drained = " ".join(fleet)
    assert "geometry_look" in drained and "geometry_measurement" in drained, (
        f"the deployed fleet does not drain the Surveyor's queues:\n{done.stdout}")


# ---------------------------------------------------------------- what each deployment drains

def test_every_deployment_target_drains_every_published_queue():
    # The published set comes from the subprocess above; this half checks the same relation from the
    # deployment files alone, so a parser regression cannot be hidden by the gate agreeing with itself.
    for target in gate.targets():
        consumed = _consumed(target.name)
        missing = {"simulation_jobs", "cleanup_tasks", "training_export", "geometry_measurement",
                   "geometry_look"} - consumed
        assert not missing, (
            f"{target.name} drains {sorted(consumed)} and leaves {sorted(missing)} to nobody. Work "
            f"sent to a queue with no consumer is accepted, logged as queued, and never runs.")


def test_the_gcp_fleet_drains_the_look_and_the_measurement():
    # Said again on its own, because this is the exact regression: the fleet is the ONLY worker any
    # real deployment starts, and these two queues are the whole Surveyor.
    consumed = _consumed("gcp-worker-fleet")
    assert "geometry_look" in consumed
    assert "geometry_measurement" in consumed


def test_the_mesh_worker_keeps_the_long_work_to_itself():
    # The fix must not be "put every queue on the one worker". A measurement somebody is waiting on
    # must not sit behind a multi-hour mesh, which is what the separate queues exist for.
    target = _target("gcp-worker-fleet")
    workers = [w for p in target.files for w in gate._workers_in(p)]
    assert len(workers) == 2, f"expected a mesh worker and a short-work worker, found {workers}"
    mesh = [w for w in workers if "simulation_jobs" in w.queues]
    assert len(mesh) == 1 and mesh[0].queues == ("simulation_jobs",), (
        "the mesh worker consumes simulation_jobs and nothing else, so a queued measurement is never "
        f"behind a mesh: {mesh}")


# ---------------------------------------------------------------- the gate cannot be fooled

def test_the_gate_fails_when_a_target_drops_a_queue(tmp_path, monkeypatch, capsys):
    # The pre-fix fleet, exactly: one worker on simulation_jobs. The gate has to name the four queues
    # that then go nowhere, or it is decoration.
    script = tmp_path / "startup.sh"
    script.write_text("docker run img celery -A meshpipeline.runtime.celery_worker worker "
                      "--queues simulation_jobs --concurrency 1\n", encoding="utf-8")
    monkeypatch.setattr(gate, "targets", lambda: [
        gate.Target(name="pre-fix-fleet", why="the fleet as it shipped", files=(script,))])
    monkeypatch.setattr(gate, "published", lambda: frozenset(
        {"simulation_jobs", "cleanup_tasks", "training_export", "geometry_measurement", "geometry_look"}))
    assert gate.check(quiet=True) == 1
    out = capsys.readouterr().out
    assert "geometry_look" in out and "geometry_measurement" in out


def test_the_gate_fails_when_a_target_starts_no_worker_at_all(tmp_path, monkeypatch, capsys):
    # The blind-spot case. A parser that found nothing would compare an empty consumed set against an
    # empty published set and report a pass, which is how a check acquires the hole it is checking for.
    empty = tmp_path / "startup.sh"
    empty.write_text("# a fleet that starts nothing\n", encoding="utf-8")
    monkeypatch.setattr(gate, "targets", lambda: [
        gate.Target(name="empty-fleet", why="a target with no worker", files=(empty,))])
    monkeypatch.setattr(gate, "published", lambda: frozenset({"simulation_jobs"}))
    assert gate.check(quiet=True) == 1
    assert "no celery worker invocation found" in capsys.readouterr().out


def test_the_gate_refuses_rather_than_passes_when_it_cannot_read_the_queues(monkeypatch, capsys):
    # Exit 2, not 0. An interpreter without celery, or a stubbed one, tells us nothing about the
    # deployment, and "I could not tell" must never read as "nothing to drain".
    def no_answer():
        raise RuntimeError("this is not a real Celery configuration")

    monkeypatch.setattr(gate, "published", no_answer)
    assert gate.check(quiet=True) == 2
    assert "could not be read" in capsys.readouterr().out


def test_the_gate_reads_queues_across_shell_and_yaml_line_breaks(tmp_path):
    # Every real invocation wraps: startup.sh over backslashes, docker-compose.yml over a folded
    # scalar. A parser that only read one line would find no --queues and report a clean empty set.
    shell = tmp_path / "startup.sh"
    shell.write_text("docker run img \\\n  celery -A app worker \\\n    --queues a,b \\\n"
                     "    --concurrency 1\n", encoding="utf-8")
    assert gate._workers_in(shell)[0].queues == ("a", "b")
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("    command: >\n      celery -A app worker\n      --queues c,d\n"
                       "      --concurrency 2\n", encoding="utf-8")
    assert gate._workers_in(compose)[0].queues == ("c", "d")


# ---------------------------------------------------------------- a fleet that cannot be woken

#: The smallest configuration validate-config.sh will read without falling over on something else.
#: Every other error it reports is beside the point here; what is asserted is the presence or absence
#: of one message.
_MINIMAL_FLEET_ENV = {
    "DEPLOYMENT_ID": "isolated", "GCP_PROJECT_ID": "p", "GCP_PROJECT_NUMBER": "1",
    "GCP_REGION": "europe-west2", "ARTIFACT_REGISTRY_REPOSITORY": "r",
    "CLOUDRUN_MESH_JOB": "m", "MESH_SERVICE_ACCOUNT": "mesh-sa", "GCP_MESH_BUCKET": "bucket-x",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created",
    "MESH_BUCKET_DISPOSITION": "created", "WORKER_MIG": "workers",
    "WORKER_MIG_ZONE": "europe-west2-a", "REDIS_URL": "redis://h:6379/0",
    "QUEUE_DEPTH_SERVICE_ACCOUNT": "queue-depth-sa", "QUEUE_NAME": "simulation_jobs",
    "WORKER_MIG_MAX_REPLICAS": "5", "WORKER_MIG_COOLDOWN_SECONDS": "180",
    "WORKER_JOBS_PER_INSTANCE": "1",
}


def _validate_config(tmp_path: Path, floor: str) -> str:
    import os

    env_file = tmp_path / "generated.env"
    values = {**_MINIMAL_FLEET_ENV, "WORKER_MIG_MIN_REPLICAS": floor}
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    done = subprocess.run(
        ["bash", str(REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh")],
        capture_output=True, text=True, timeout=120, check=False,
        env={**os.environ, "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1"})
    return done.stdout + done.stderr


def test_a_fleet_the_autoscaler_cannot_wake_is_refused_at_a_floor_of_zero(tmp_path):
    # The fleet is now the only consumer of four queues and the autoscaler watches one of them, so at
    # zero instances a queued measurement or look sits in the broker with nothing that will ever start
    # a worker for it. A queue nobody drains and a queue whose only drainer is asleep are the same
    # silence to a customer, so the deploy refuses rather than leaving it to be discovered.
    out = _validate_config(tmp_path, "0")
    assert "WORKER_MIG_MIN_REPLICAS is 0" in out, out
    assert "geometry_look" in out, out


def test_a_warm_pool_is_accepted(tmp_path):
    # The prod floor. The rule must name a real condition, not refuse every fleet.
    out = _validate_config(tmp_path, "1")
    assert "WORKER_MIG_MIN_REPLICAS is 0" not in out, out


# ---------------------------------------------------------------- no worker escapes a target

#: Where a celery worker could be started from. Scanned rather than trusted, so a new deployment
#: arrangement cannot be added in a file the gate does not look at.
_SEARCHED = ("deploy", "docker-compose.yml", "Dockerfile", "Makefile")


def _files_that_start_a_worker() -> set[Path]:
    found: set[Path] = set()
    for entry in _SEARCHED:
        path = REPO / entry
        candidates = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else [path]
        for candidate in candidates:
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if "celery" in text and gate._WORKER.search(gate._flatten(text)):
                found.add(candidate)
    return found


def test_every_file_that_starts_a_worker_belongs_to_a_declared_target():
    declared = {p.resolve() for t in gate.targets() for p in t.files}
    stray = sorted(p.relative_to(REPO).as_posix() for p in _files_that_start_a_worker()
                   if p.resolve() not in declared)
    assert not stray, (
        f"these files start a celery worker and belong to no deployment target, so the queues they "
        f"drain are unchecked: {stray}. Add them to check_queue_consumers.targets().")


def test_no_deployment_relies_on_the_image_default_command():
    # The Dockerfile's CMD drains simulation_jobs only. That is right for the image's default role and
    # wrong for any deployment, so every target has to state its own command. A target that said
    # nothing would inherit the mesh worker's queue list and drop the other four in silence.
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    startup = (REPO / "deploy" / "gcp" / "worker" / "startup.sh").read_text(encoding="utf-8")
    assert compose.count("celery -A meshpipeline.runtime.celery_worker worker") == 2
    assert startup.count("celery -A meshpipeline.runtime.celery_worker worker") == 2
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert '"--queues", "simulation_jobs"' in dockerfile, (
        "the image default is the mesh worker; if that changes, the targets above must still name "
        "their own queues rather than inheriting whatever the CMD became")
