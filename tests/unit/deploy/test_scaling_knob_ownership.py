# Responsibility: Verify the deploy sets the elastic scaling knobs when it CREATES a resource and
# leaves them alone afterwards, because the admin console owns them from that point on.
# Boundaries: it drives the stages against a fake gcloud and reads what they would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"

# THE DECISION UNDER TEST. Two writers of one autoscaling policy is a race whose loser is silent:
# a warm floor raised in the console and reset by the next unrelated deploy is discovered as a cold
# start under load, weeks later. So ownership is split - the deploy owns fleet SHAPE (image,
# machine type, template, rolling policy) and the console owns the elastic knobs.
#
# These assertions are what make that a property of the deploy rather than a paragraph in a design
# document.

_APP_DIGEST = "us-central1-docker.pkg.dev/fake-proj/mesh/app@sha256:c0ffee"

# FAKE_AUTOSCALER is the MIG's status.autoscaler field: non-empty means the group already has one.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit "${FAKE_SA_RC:-1}"; fi
if has "instance-templates describe"; then exit 1; fi
if has "instance-groups managed describe"; then
  if has "status.autoscaler"; then printf '%s\n' "${FAKE_AUTOSCALER:-}"; exit 0; fi
  if has "versions[0].instanceTemplate"; then printf '%s\n' "${FAKE_LIVE_TEMPLATE:-}"; exit 0; fi
  if has "instanceTemplate"; then printf '%s\n' "${FAKE_LIVE_TEMPLATE:-}"; exit 0; fi
  exit "${FAKE_MIG_RC:-1}"
fi
if has "compute autoscalers describe"; then
  if has "minNumReplicas"; then printf '%s\n' "${FAKE_AS_MIN:-}"; exit 0; fi
  if has "maxNumReplicas"; then printf '%s\n' "${FAKE_AS_MAX:-}"; exit 0; fi
  if has "coolDownPeriodSec"; then printf '%s\n' "${FAKE_AS_COOLDOWN:-}"; exit 0; fi
  if has "singleInstanceAssignment"; then printf '%s\n' "${FAKE_AS_ASSIGNMENT:-}"; exit 0; fi
  exit 0
fi
if has "scheduler jobs describe"; then exit 1; fi
if has "run jobs describe"; then exit 1; fi
if has "run services describe"; then
  if has "containers[0].image"; then printf '%s\n' "${FAKE_LIVE_IMAGE:-}"; exit 0; fi
  if has "containers[0].env";   then printf '%s\n' "${FAKE_LIVE_ENV_NAMES:-}"; exit 0; fi
  if has "status.url";          then printf '%s\n' "https://t.run.app"; exit 0; fi
  exit "${FAKE_SVC_EXISTS_RC:-1}"
fi
if has "run services get-iam-policy"; then printf '%s\n' "${FAKE_POLICY_MEMBERS:-}"; exit 0; fi
exit 0
"""

_FLEET_ENV = {
    "APP_IMAGE": _APP_DIGEST,
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "REDIS_URL": "redis://10.0.0.3:6379",
    "WORKER_ENV_URI": "gs://fake-bucket/worker.env",
    "WORKER_MIG": "t-workers",
    "WORKER_MIG_ZONE": "europe-west1-b",
    "WORKER_SERVICE_ACCOUNT": "t-worker",
}

_ADMIN_ENV = {
    "ADMIN_IMAGE": "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:c0ffee",
    "APP_ENV": "dev",
    "CLOUDRUN_ADMIN_SERVICE": "t-admin",
    "ADMIN_SERVICE_ACCOUNT": "t-admin",
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)

    counter = {"n": 0}

    def _run(script: str, env_vals: dict, fake: dict | None = None):
        counter["n"] += 1
        state = tmp_path / f"state{counter['n']}"
        env_file = tmp_path / f"generated{counter['n']}.env"
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env_vals.items() if v != "") + "\n",
            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPTS / script)], capture_output=True, text=True,
            env={**os.environ,
                 "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state),
                 "DEPLOY_ENV_FILE": str(env_file),
                 **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


# ---------------------------------------------------------------------------
# The worker fleet's autoscaler
# ---------------------------------------------------------------------------

def test_a_new_group_gets_its_autoscaling_policy(run):
    # A fresh environment must still come up correctly configured, so the env values survive as
    # CREATION defaults even though they are no longer reconciled.
    done, calls = run("create-worker-fleet.sh", _FLEET_ENV,
                      fake={"FAKE_MIG_RC": "1", "FAKE_AUTOSCALER": ""})
    assert done.returncode == 0, done.stderr
    assert "set-autoscaling" in calls, (
        "a group created with no autoscaler would never scale at all")


def test_an_existing_autoscaler_is_not_reconciled(run):
    # THE ASSERTION THIS FILE EXISTS FOR. A deploy that re-applied the policy here would silently
    # undo every scaling change made from the admin console.
    done, calls = run(
        "create-worker-fleet.sh", _FLEET_ENV,
        fake={
            "FAKE_MIG_RC": "0",
            "FAKE_AUTOSCALER": (
                "https://www.googleapis.com/compute/v1/projects/fake-proj/zones/"
                "europe-west1-b/autoscalers/t-workers"
            ),
            "FAKE_LIVE_TEMPLATE": "t-workers-tpl-existing",
        })
    assert done.returncode == 0, done.stderr
    assert "set-autoscaling" not in calls, (
        "the deploy reconciled an autoscaler the admin console owns; a warm floor raised in the "
        "console would be reset by the next unrelated deploy")


def test_the_skip_says_who_owns_the_knobs(run):
    done, _calls = run(
        "create-worker-fleet.sh", _FLEET_ENV,
        fake={"FAKE_MIG_RC": "0", "FAKE_AUTOSCALER": "zones/europe-west1-b/autoscalers/t-workers",
              "FAKE_LIVE_TEMPLATE": "t-workers-tpl-existing"})
    combined = done.stdout + done.stderr
    assert "admin console" in combined.lower(), (
        "a silent skip is indistinguishable from a bug; the operator must be told where the "
        "scaling knobs now live")


def test_the_group_is_still_rolled_onto_a_new_template(run):
    # Shape still belongs to the deploy. Ownership of the knobs must not become ownership of
    # nothing.
    done, calls = run(
        "create-worker-fleet.sh", _FLEET_ENV,
        fake={"FAKE_MIG_RC": "0", "FAKE_AUTOSCALER": "zones/z/autoscalers/t-workers",
              "FAKE_LIVE_TEMPLATE": "t-workers-tpl-stale"})
    assert done.returncode == 0, done.stderr
    assert "rolling-action start-update" in calls


# ---------------------------------------------------------------------------
# Cloud Run min/max instances
# ---------------------------------------------------------------------------

def test_a_new_cloud_run_service_is_given_its_scaling(run):
    done, calls = run("create-admin-service.sh", _ADMIN_ENV, fake={"FAKE_SVC_EXISTS_RC": "1"})
    assert done.returncode == 0, done.stderr
    assert "--min-instances" in calls
    assert "--max-instances" in calls


def test_an_existing_cloud_run_service_keeps_the_scaling_it_has(run):
    # Omitting the flag from `gcloud run deploy` leaves the live value untouched, which is exactly
    # the required behaviour once the console owns the warm floor.
    done, calls = run(
        "create-admin-service.sh", _ADMIN_ENV,
        fake={"FAKE_SVC_EXISTS_RC": "0",
              "FAKE_LIVE_IMAGE": _ADMIN_ENV["ADMIN_IMAGE"],
              "FAKE_LIVE_ENV_NAMES": ""})
    assert done.returncode == 0, done.stderr
    assert "run deploy" in calls
    assert "--min-instances" not in calls, (
        "the deploy re-applied a warm floor the admin console owns")
    assert "--max-instances" not in calls


# ---------------------------------------------------------------------------
# The queue-depth publisher, which is the THIRD writer of the same policy
# ---------------------------------------------------------------------------

_PUBLISHER_ENV = {
    **_FLEET_ENV,
    "QUEUE_NAME": "simulation_jobs",
    "WORKER_MIG_MIN_REPLICAS": "1",
    "WORKER_MIG_MAX_REPLICAS": "5",
}


def test_the_publisher_preserves_sizing_it_did_not_set(run):
    # create-queue-depth-publisher.sh must keep repointing the METRIC FILTER - a publisher that
    # moved zone or queue would otherwise leave the autoscaler reading a series nobody writes - but
    # set-autoscaling replaces the whole policy, so it has to carry the console's numbers through
    # rather than re-apply the deployment's.
    done, calls = run(
        "create-queue-depth-publisher.sh", _PUBLISHER_ENV,
        fake={
            "FAKE_MIG_RC": "0",
            "FAKE_AUTOSCALER": "zones/europe-west1-b/autoscalers/t-workers",
            "FAKE_AS_MIN": "3",
            "FAKE_AS_MAX": "9",
            "FAKE_AS_COOLDOWN": "240",
            "FAKE_AS_ASSIGNMENT": "2",
        })
    assert done.returncode == 0, done.stderr
    assert "set-autoscaling" in calls, "the metric wiring must still be reconciled"
    assert "--min-num-replicas 3" in calls, (
        "the publisher re-applied the deployment's floor of 1 over the console's 3")
    assert "--max-num-replicas 9" in calls
    assert "--cool-down-period 240" in calls
    assert "--stackdriver-metric-single-instance-assignment 2" in calls


def test_the_publisher_uses_deployment_values_when_there_is_no_autoscaler(run):
    done, calls = run(
        "create-queue-depth-publisher.sh", _PUBLISHER_ENV,
        fake={"FAKE_MIG_RC": "0", "FAKE_AUTOSCALER": ""})
    assert done.returncode == 0, done.stderr
    assert "--min-num-replicas 1" in calls
    assert "--max-num-replicas 5" in calls
