# Responsibility: Verify the meter sweep is provisioned only where billing is, runs as the API's identity,
#                 reaches the private database, and is scheduled.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-meter-sweep.sh"
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "run jobs describe"; then exit "${FAKE_JOB_RC:-1}"; fi
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
    "STRIPE_API_KEY_SECRET": "stripe-api-key",
    "STRIPE_WEBHOOK_SECRET_SECRET": "stripe-webhook-secret",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    counter = {"n": 0}

    def _run(over: dict | None = None, fake: dict | None = None):
        counter["n"] += 1
        state = tmp_path / f"s{counter['n']}"
        env_file = tmp_path / f"g{counter['n']}.env"
        vals = {**_ENV, **(over or {})}
        env_file.write_text("\n".join(f"{k}={v}" for k, v in vals.items() if v != "") + "\n",
                            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file), **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


@pytest.mark.parametrize("holder", ["STRIPE_API_KEY_SECRET", "STRIPE_WEBHOOK_SECRET_SECRET"])
def test_a_deployment_that_does_not_charge_is_a_stated_skip(run, holder):
    # Both credentials, because settings/billing.enabled() requires both - a sweep with a key and
    # no webhook secret would run against a gateway the API itself refuses to build.
    done, calls = run({holder: ""})
    assert done.returncode == 0, done.stderr
    assert "skipping" in done.stdout.lower()
    for mutation in ("run jobs create", "run jobs update", "scheduler jobs create",
                     "scheduler jobs update", "pause"):
        assert mutation not in calls


def test_billing_without_a_database_is_refused_rather_than_provisioned(run):
    # Overage would be recorded and never billed, silently, by a job that can only fail.
    done, calls = run({"MIGRATE_DB_HOST": ""})
    assert done.returncode != 0
    assert "run jobs create" not in calls


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"APP_IMAGE": "us-central1-docker.pkg.dev/p/r/app:v1"})
    assert done.returncode != 0
    assert "run jobs create" not in calls


def test_it_runs_the_sweep_entrypoint_as_the_api_identity(run):
    # The API's identity already holds the database password and both Stripe secrets; a second one
    # would double the places a payment credential can be read from.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--service-account t-api@fake-proj.iam.gserviceaccount.com" in calls
    assert "meshpipeline.runtime.meter_sweep" in calls
    assert "--oauth-service-account-email t-api@fake-proj.iam.gserviceaccount.com" in calls


def test_it_reaches_the_private_database_with_its_password_and_both_stripe_secrets(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--vpc-egress private-ranges-only" in calls
    assert "POSTGRES_HOST=10.0.0.5" in calls
    for binding in ("POSTGRES_PASSWORD=postgres-password:latest",
                    "STRIPE_API_KEY=stripe-api-key:latest",
                    "STRIPE_WEBHOOK_SECRET=stripe-webhook-secret:latest"):
        assert binding in calls


def test_it_is_scheduled_and_the_scheduler_may_invoke_it(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "scheduler jobs create http t-meter-sweep-tick" in calls
    assert "*/15 * * * *" in calls
    assert "jobs/t-meter-sweep:run" in calls
    assert "run jobs add-iam-policy-binding t-meter-sweep" in calls


def test_an_existing_job_is_updated_not_recreated(run):
    done, calls = run(fake={"FAKE_JOB_RC": "0", "FAKE_SCHED_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "run jobs update t-meter-sweep" in calls
    assert "run jobs create" not in calls
    assert "scheduler jobs update http" in calls


def test_the_deploy_runs_the_sweep_stage_after_the_api_it_depends_on():
    # The API stage grants the shared identity the Stripe secrets; a sweep provisioned first would
    # reference secrets its identity cannot yet read.
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index('"${S}/create-api-service.sh"') < text.index('"${S}/create-meter-sweep.sh"')


def test_turning_billing_off_pauses_an_existing_schedule(run):
    # The job keeps its own Stripe secret references; left scheduled it would go on reporting to the
    # meter after the API stopped charging.
    done, calls = run({"STRIPE_API_KEY_SECRET": ""}, fake={"FAKE_SCHED_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "scheduler jobs pause t-meter-sweep-tick" in calls
    assert "run jobs create" not in calls and "run jobs update" not in calls


def test_a_deployment_that_never_charged_pauses_nothing(run):
    done, calls = run({"STRIPE_API_KEY_SECRET": ""})
    assert done.returncode == 0, done.stderr
    assert "pause" not in calls


def test_turning_billing_back_on_resumes_the_schedule(run):
    done, calls = run(fake={"FAKE_JOB_RC": "0", "FAKE_SCHED_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "scheduler jobs resume t-meter-sweep-tick" in calls


def test_a_scheduler_that_cannot_invoke_the_job_fails_the_deploy(run):
    # Every tick would get 403 and overage would go unbilled behind a green deploy.
    done, _ = run(fake={"FAKE_ADD_IAM_RC": "1"})
    assert done.returncode != 0
    assert "unbilled" in done.stderr


def test_a_grant_this_identity_cannot_set_but_already_exists_passes(run):
    done, _ = run(fake={"FAKE_ADD_IAM_RC": "1",
                        "FAKE_INVOKERS": "serviceAccount:t-api@fake-proj.iam.gserviceaccount.com"})
    assert done.returncode == 0, done.stderr
