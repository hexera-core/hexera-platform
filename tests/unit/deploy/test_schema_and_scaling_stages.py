# Responsibility: Verify the deploy applies the schema once before the image rolls, and publishes the fleet's depth off the fleet.
# Boundaries: it drives the two stages against a fake gcloud and reads what they would have mutated; it touches no cloud.
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"
PROGRAM = REPO / "deploy" / "gcp" / "worker" / "queue_depth_publisher.py"

# A fake gcloud that logs every call and answers the read paths these two scripts take. The three
# `describe` calls report ABSENT so the create paths run; everything else succeeds, so the log is a
# complete record of what a real run would have mutated.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "secrets describe"; then exit 1; fi
if has "scheduler jobs describe"; then exit 1; fi
if has "artifacts docker images describe"; then echo "REG/app@sha256:deadbeef"; exit 0; fi
exit 0
"""

_FAKE_ENVSUBST = r"""#!/usr/bin/env python3
import os, re, sys
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\$(\w+)",
                        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                        sys.stdin.read()))
"""

_APP_DIGEST = "us-central1-docker.pkg.dev/fake-proj/mesh/app@sha256:c0ffee"

_BASE = {
    "DEPLOYMENT_ID": "isolated-deploy",
    "GCP_PROJECT_ID": "fake-proj", "GCP_PROJECT_NUMBER": "778899",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "isolated-deploy-mesh",
    "APP_IMAGE": _APP_DIGEST,
}
_DATABASE = {
    "MIGRATE_DB_HOST": "10.66.0.3", "MIGRATE_DB_PORT": "5432",
    "MIGRATE_DB_NAME": "meshpipeline", "MIGRATE_DB_USER": "meshpipeline",
    "POSTGRES_PASSWORD_SECRET": "postgres-password",
    "CLOUDRUN_MIGRATE_JOB": "isolated-deploy-migrate",
    "MIGRATE_SERVICE_ACCOUNT": "isolated-deploy-migrate",
}
_FLEET = {
    "REDIS_URL": "redis://10.108.144.235:6379/0",
    "WORKER_MIG": "isolated-workers", "WORKER_MIG_ZONE": "europe-west1-b",
    "WORKER_MIG_MIN_REPLICAS": "1", "WORKER_MIG_MAX_REPLICAS": "5",
    "WORKER_MIG_COOLDOWN_SECONDS": "180", "WORKER_JOBS_PER_INSTANCE": "1",
    "CLOUDRUN_QUEUE_DEPTH_JOB": "isolated-deploy-queue-depth",
    "QUEUE_DEPTH_SERVICE_ACCOUNT": "isolated-deploy-qd",
    "QUEUE_DEPTH_SCHEDULER_JOB": "isolated-deploy-queue-depth",
    "QUEUE_DEPTH_SCHEDULE": '"* * * * *"',
    "QUEUE_NAME": "simulation_jobs",
}


def _run(script, tmp_path, values, extra_env=None):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (b / "gcloud").write_text(_FAKE_GCLOUD); (b / "gcloud").chmod(0o755)
    (b / "envsubst").write_text(_FAKE_ENVSUBST); (b / "envsubst").chmod(0o755)
    env_file = tmp_path / "generated.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    state = tmp_path / "state"; state.mkdir(exist_ok=True)
    env = {"PATH": f"{b}:{os.environ.get('PATH', '')}", "HOME": str(tmp_path),
           "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1",
           # Rendered manifests stay in this test's tmp_path rather than the checkout.
           "RENDER_DIR": str(tmp_path / "rendered"), **(extra_env or {})}
    p = subprocess.run(["bash", str(SCRIPTS / script)], env=env,
                       capture_output=True, text=True, timeout=120)
    log = (state / "calls.log").read_text() if (state / "calls.log").exists() else ""
    return p, log


# the schema stage

def test_no_declared_database_skips_the_migration_and_says_so(tmp_path):
    p, log = _run("run-migrations.sh", tmp_path, _BASE)
    assert p.returncode == 0, p.stderr
    assert "skipping" in p.stdout.lower()
    assert "jobs replace" not in log and "jobs execute" not in log


def test_automation_may_not_skip_the_schema_step(tmp_path):
    # The dangerous silence: a pinned-target run that quietly ships code against a database nobody
    # migrated. Under DEPLOY_NONINTERACTIVE a missing migration target is a mistake, not a choice.
    p, log = _run("run-migrations.sh", tmp_path, _BASE, {"DEPLOY_NONINTERACTIVE": "1"})
    assert p.returncode != 0
    assert "must not skip the schema step" in p.stderr
    assert "jobs replace" not in log


def test_a_local_database_host_is_refused_before_any_mutation(tmp_path):
    p, log = _run("run-migrations.sh", tmp_path, {**_BASE, **_DATABASE, "MIGRATE_DB_HOST": "localhost"})
    assert p.returncode != 0
    assert "names a LOCAL database" in p.stderr
    assert "jobs replace" not in log


def test_a_tag_is_refused_where_a_digest_is_required(tmp_path):
    values = {**_BASE, **_DATABASE, "APP_IMAGE": "us-central1-docker.pkg.dev/fake-proj/mesh/app:latest"}
    p, log = _run("run-migrations.sh", tmp_path, values)
    assert p.returncode != 0
    assert "is a TAG, not a digest" in p.stderr
    assert "jobs replace" not in log


def test_the_migration_is_applied_and_waited_on(tmp_path):
    p, log = _run("run-migrations.sh", tmp_path, {**_BASE, **_DATABASE})
    assert p.returncode == 0, p.stderr
    assert "run jobs replace" in log
    # WAITED ON, not fired and forgotten: the exit code of this execution is the deploy's verdict on
    # whether the schema reached head, and a deploy that did not wait has no verdict to report.
    assert "run jobs execute isolated-deploy-migrate --region europe-west1 --wait" in log


def test_the_migration_job_names_its_database_and_references_its_password(tmp_path):
    _run("run-migrations.sh", tmp_path, {**_BASE, **_DATABASE})
    doc = yaml.safe_load((tmp_path / "rendered" / "migrate-job.yaml").read_text())
    container = doc["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == _APP_DIGEST, "the migration must run the promoted application bytes"
    assert container["command"] == ["python", "-m", "meshpipeline.runtime.migrate"]
    env = {e["name"]: e for e in container["env"]}
    assert env["POSTGRES_HOST"]["value"] == "10.66.0.3"
    # The credential is a REFERENCE and the spec holds no value for it - the property
    # devtools/quality/check_deploy_secrets.py enforces over every spec in the repository.
    assert "value" not in env["POSTGRES_PASSWORD"]
    assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"] == "postgres-password"


# the queue-depth stage

def test_no_declared_fleet_skips_the_publisher(tmp_path):
    p, log = _run("create-queue-depth-publisher.sh", tmp_path, _BASE)
    assert p.returncode == 0, p.stderr
    assert "set-autoscaling" not in log and "scheduler jobs" not in log


def test_the_publisher_is_proven_before_the_autoscaler_is_pointed_at_it(tmp_path):
    p, log = _run("create-queue-depth-publisher.sh", tmp_path, {**_BASE, **_FLEET})
    assert p.returncode == 0, p.stderr + p.stdout
    calls = log.splitlines()
    ran = next(i for i, c in enumerate(calls) if "run jobs execute isolated-deploy-queue-depth" in c)
    scaled = next(i for i, c in enumerate(calls) if "set-autoscaling" in c)
    # Publish first, THEN repoint the group: an autoscaler aimed at a series that has never had a
    # value is the CUSTOM_METRIC_INVALID state the live group already spent time in.
    assert ran < scaled, log
    assert "--wait" in calls[ran]


def test_the_group_scales_proportionally_on_one_series_not_on_a_per_instance_average(tmp_path):
    _, log = _run("create-queue-depth-publisher.sh", tmp_path, {**_BASE, **_FLEET})
    line = next(c for c in log.splitlines() if "set-autoscaling" in c)
    assert "--stackdriver-metric-single-instance-assignment 1" in line
    # The arrangement being replaced. A utilization target over a per-instance series averages N
    # identical copies of the group-wide total, so any backlog reads as "at target on every
    # instance" and the group jumps straight to max.
    assert "--custom-metric-utilization" not in line
    assert "--stackdriver-metric-utilization-target" not in line
    # The filter must select exactly ONE time series; that is the contract single-instance
    # assignment is defined against.
    assert 'resource.type = "generic_task"' in line
    assert 'resource.labels.namespace = "isolated-deploy"' in line
    assert 'resource.labels.task_id = "simulation_jobs"' in line
    assert "--min-num-replicas 1" in line and "--max-num-replicas 5" in line


def test_the_schedule_invokes_the_job_as_the_publisher_identity(tmp_path):
    _, log = _run("create-queue-depth-publisher.sh", tmp_path, {**_BASE, **_FLEET})
    line = next(c for c in log.splitlines() if "scheduler jobs create http" in c)
    assert ("https://europe-west1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/"
            "fake-proj/jobs/isolated-deploy-queue-depth:run") in line
    assert "--oauth-service-account-email isolated-deploy-qd@fake-proj.iam.gserviceaccount.com" in line
    assert "--schedule * * * * *" in line


def test_the_embedded_program_is_the_repositorys_program(tmp_path):
    # The publisher cannot be a file in the image - deployment promotes a validated image and never
    # builds one - so it travels in the spec. That is only safe while the spec carries exactly what
    # the repository holds, byte for byte.
    _run("create-queue-depth-publisher.sh", tmp_path, {**_BASE, **_FLEET})
    doc = yaml.safe_load((tmp_path / "rendered" / "queue-depth-job.yaml").read_text())
    container = doc["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == _APP_DIGEST
    arg = container["args"][0]
    encoded = arg.split("b64decode('")[1].split("')")[0]
    assert base64.b64decode(encoded) == PROGRAM.read_bytes()
