# Responsibility: Verify the maintenance sweep is provisioned wherever a hosted database is, runs as the API's
#                 identity, reaches the private data tier, proves itself once, and is scheduled.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-maintenance-sweep.sh"
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"
DESTROY = REPO / "deploy" / "gcp" / "scripts" / "destroy-env.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "run jobs describe"; then exit "${FAKE_JOB_RC:-1}"; fi
if has "run jobs execute"; then exit "${FAKE_EXECUTE_RC:-0}"; fi
if has "scheduler jobs describe"; then exit "${FAKE_SCHED_RC:-1}"; fi
if has "run jobs add-iam-policy-binding"; then exit "${FAKE_ADD_IAM_RC:-0}"; fi
if has "run jobs get-iam-policy"; then printf '%s\n' ${FAKE_INVOKERS:-}; exit 0; fi
exit 0
"""

_ENV = {
    "APP_IMAGE": "us-central1-docker.pkg.dev/fake-proj/hexera/app@sha256:c0ffee",
    "APP_ENV": "prod",
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "1",
    "GCP_REGION": "us-central1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "MIGRATE_DB_HOST": "10.0.0.5",
    "POSTGRES_PASSWORD_SECRET": "postgres-password",
    "REDIS_URL": "redis://10.0.0.9:6379/0",
    "REDIS_KEY_PREFIX": "dev-t:",
    "MINIO_ENDPOINT": "storage.googleapis.com",
    "MINIO_ACCESS_KEY": "GOOG1EXAMPLE",
    "MINIO_BUCKET": "t-artifacts",
    "MINIO_REGION": "us-central1",
    "MINIO_SECRET_KEY_SECRET": "minio-secret-key",
}


#: Settings the unit tier itself exports (its own MINIO_ENDPOINT, for one). The stage reads the
#: deployment env file, so the ambient process environment must not stand in for it here.
_AMBIENT = ("MINIO_", "REDIS_", "STALLED_JOB_TIMEOUT_HOURS", "WORKER_LEASE_SECONDS",
            "UPLOAD_RETENTION_DAYS")


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    counter = {"n": 0}
    base = {k: v for k, v in os.environ.items() if not k.startswith(_AMBIENT)}

    def _run(over: dict | None = None, fake: dict | None = None):
        counter["n"] += 1
        state = tmp_path / f"s{counter['n']}"
        env_file = tmp_path / f"g{counter['n']}.env"
        vals = {**_ENV, **(over or {})}
        env_file.write_text("\n".join(f"{k}={v}" for k, v in vals.items() if v != "") + "\n",
                            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**base, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file), **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


def test_a_deployment_with_no_hosted_database_is_a_stated_skip(run):
    # The mesh-only case: the application and its beat service run on the operator's machine, and
    # there is no jobs table here to sweep.
    done, calls = run({"MIGRATE_DB_HOST": ""})
    assert done.returncode == 0, done.stderr
    assert "skipping" in done.stdout.lower()
    for mutation in ("run jobs create", "run jobs update", "run jobs execute",
                     "scheduler jobs create", "scheduler jobs update"):
        assert mutation not in calls


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"APP_IMAGE": "us-central1-docker.pkg.dev/p/r/app:v1"})
    assert done.returncode != 0
    assert "run jobs create" not in calls


def test_it_runs_the_sweep_entrypoint_as_the_api_identity(run):
    # The API's identity already holds the database password and the object-store credential; a
    # second one would need the same grants and double the places a credential can be read from.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--service-account t-api@fake-proj.iam.gserviceaccount.com" in calls
    assert "meshpipeline.runtime.maintenance_sweep" in calls
    assert "--oauth-service-account-email t-api@fake-proj.iam.gserviceaccount.com" in calls


def test_it_reaches_the_private_database_the_broker_and_the_object_store(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--vpc-egress private-ranges-only" in calls
    assert "POSTGRES_HOST=10.0.0.5" in calls
    assert "POSTGRES_PASSWORD=postgres-password:latest" in calls
    # the reaper's closing line goes to the deployment's own keyspace, where the API listens
    assert "REDIS_URL=redis://10.0.0.9:6379/0" in calls and "REDIS_KEY_PREFIX=dev-t:" in calls
    # the upload purge and the orphan reconcile delete objects
    assert "MINIO_ENDPOINT=storage.googleapis.com" in calls and "MINIO_BUCKET=t-artifacts" in calls
    assert "MINIO_SECRET_KEY=minio-secret-key:latest" in calls


def test_a_deployment_without_an_object_store_states_none(run):
    done, calls = run({"MINIO_ENDPOINT": "", "MINIO_SECRET_KEY_SECRET": ""})
    assert done.returncode == 0, done.stderr
    assert "MINIO_ENDPOINT=" not in calls and "MINIO_SECRET_KEY=" not in calls


def test_the_sweeps_knobs_travel_only_when_the_deployment_states_them(run):
    # Otherwise the sweep runs on the code defaults, exactly as the API and the fleet do.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "STALLED_JOB_TIMEOUT_HOURS=" not in calls and "WORKER_LEASE_SECONDS=" not in calls
    done, calls = run({"STALLED_JOB_TIMEOUT_HOURS": "2", "WORKER_LEASE_SECONDS": "600",
                       "UPLOAD_RETENTION_DAYS": "7"})
    assert done.returncode == 0, done.stderr
    for pair in ("STALLED_JOB_TIMEOUT_HOURS=2", "WORKER_LEASE_SECONDS=600", "UPLOAD_RETENTION_DAYS=7"):
        assert pair in calls


def test_it_is_scheduled_every_ten_minutes_and_the_scheduler_may_invoke_it(run):
    # Ten minutes is the beat schedule's cadence for the reaper, the sweep a person waits on.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "scheduler jobs create http t-maintenance-sweep-tick" in calls
    assert "*/10 * * * *" in calls
    assert "jobs/t-maintenance-sweep:run" in calls
    assert "run jobs add-iam-policy-binding t-maintenance-sweep" in calls


def test_an_existing_job_is_updated_not_recreated_and_its_schedule_resumed(run):
    done, calls = run(fake={"FAKE_JOB_RC": "0", "FAKE_SCHED_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "run jobs update t-maintenance-sweep" in calls
    assert "run jobs create" not in calls
    assert "scheduler jobs update http" in calls
    assert "scheduler jobs resume t-maintenance-sweep-tick" in calls


def test_it_sweeps_once_at_deploy_before_the_schedule_exists(run):
    # The proving run: the identity reads its secrets, the database answers, the entrypoint is the
    # entrypoint - and the tenant whose credits a dead job held is freed by the deploy itself.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    lines = calls.splitlines()
    ran = next(i for i, c in enumerate(lines) if "run jobs execute t-maintenance-sweep" in c)
    assert "--wait" in lines[ran]
    scheduled = next(i for i, c in enumerate(lines) if "scheduler jobs create" in c)
    assert ran < scheduled


def test_a_failed_first_run_fails_the_deploy_and_schedules_nothing(run):
    done, calls = run(fake={"FAKE_EXECUTE_RC": "1"})
    assert done.returncode != 0
    assert "not being reaped" in done.stderr.lower()
    assert "scheduler jobs create" not in calls


def test_a_scheduler_that_cannot_invoke_the_job_fails_the_deploy(run):
    # Every tick would get 403 and stalled jobs would go unreaped behind a green deploy.
    done, _ = run(fake={"FAKE_ADD_IAM_RC": "1"})
    assert done.returncode != 0
    assert "unreaped" in done.stderr


def test_a_grant_this_identity_cannot_set_but_already_exists_passes(run):
    done, _ = run(fake={"FAKE_ADD_IAM_RC": "1",
                        "FAKE_INVOKERS": "serviceAccount:t-api@fake-proj.iam.gserviceaccount.com"})
    assert done.returncode == 0, done.stderr


def test_an_argument_list_that_starts_with_a_dash_is_attached_to_its_flag():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--args=-m,meshpipeline.runtime.maintenance_sweep" in text
    for flag in re.findall(r"--(?:args|command)\s+\"?-", text):
        raise AssertionError(f"a dash-leading value is passed as a separate word: {flag}")


def test_the_deploy_ships_the_sweep_with_the_image_after_the_api_it_shares_an_identity_with():
    # AFTER the API stage, which grants the shared identity its secrets. WITH the image, so every
    # merge to main provisions it - no fleet roll to dispatch by hand - and a new application
    # digest never leaves the sweep running last month's code.
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index('"${S}/create-api-service.sh"') < text.index('"${S}/create-maintenance-sweep.sh"')
    stage_at = text.index('stage "Maintenance sweep')
    script_at = text.index('"${S}/create-maintenance-sweep.sh"')
    assert "if want images; then" in text[stage_at:script_at]
    assert 'skipped images "the maintenance sweep' in text, (
        "a stage that is not selected must say so; silence reads as success")


def test_tearing_an_environment_down_removes_the_sweep_and_its_schedule():
    # A schedule left firing at a deleted job logs an error every ten minutes until somebody notices.
    text = DESTROY.read_text(encoding="utf-8")
    assert '"${DEPLOYMENT_ID}-maintenance-sweep-tick"' in text
    assert '"${DEPLOYMENT_ID}-maintenance-sweep"' in text
    assert text.index("maintenance-sweep-tick") < text.index('"${DEPLOYMENT_ID}-maintenance-sweep"'), (
        "the schedule must go before the job it fires at")
