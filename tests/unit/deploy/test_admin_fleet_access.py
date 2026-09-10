# Responsibility: Verify the admin console is told which fleet to read and granted exactly the
# authority its pages need - no more.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-admin-service.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "iam roles describe"; then exit "${FAKE_ROLE_RC:-1}"; fi
if has "iam roles create"; then exit "${FAKE_IAM_ROLE_CREATE_FAILS:-0}"; fi
if has "run services describe"; then
  if has "containers[0].image"; then printf '%s\n' "${FAKE_LIVE_IMAGE:-}"; exit 0; fi
  if has "containers[0].env";   then printf '%s\n' "${FAKE_LIVE_ENV_NAMES:-}"; exit 0; fi
  if has "status.url";          then printf '%s\n' "https://t-admin.run.app"; exit 0; fi
  exit "${FAKE_SVC_EXISTS_RC:-1}"
fi
if has "run services get-iam-policy"; then printf '%s\n' "${FAKE_POLICY_MEMBERS:-}"; exit 0; fi
exit 0
"""

_ADMIN_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:c0ffee"

_ENV = {
    "ADMIN_IMAGE": _ADMIN_DIGEST,
    "ADMIN_SERVICE_ACCOUNT": "t-admin",
    "APP_ENV": "dev",
    "CLOUDRUN_ADMIN_SERVICE": "t-admin",
    "CLOUDRUN_API_SERVICE": "t-api",
    "CLOUDRUN_CONSOLE_SERVICE": "t-console",
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "QUEUE_NAME": "simulation_jobs",
    "WORKER_MIG": "t-workers",
    "WORKER_MIG_ZONE": "europe-west1-b",
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
        state = tmp_path / f"state{counter['n']}"
        env_file = tmp_path / f"generated{counter['n']}.env"
        env_vals = {**_ENV, **(over or {})}
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env_vals.items() if v != "") + "\n",
            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ,
                 "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state),
                 "DEPLOY_ENV_FILE": str(env_file),
                 **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


def _deploy_line(calls: str) -> str:
    # lib.sh's `gc` wrapper prepends `--project <id>`, so the rollout is the line CONTAINING
    # "run deploy", not one starting with it.
    return next(line for line in calls.splitlines() if "run deploy" in line)


def test_the_console_is_told_which_fleet_to_read(run):
    # The console reads the group and the services BY NAME rather than discovering them. Discovery
    # would mean listing every group in the project and guessing which is ours - a wider grant and a
    # worse failure mode, since a renamed group would silently show a different fleet.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    deploy = _deploy_line(calls)
    for expected in (
        "GCP_PROJECT_ID=fake-proj",
        "GCP_PROJECT_NUMBER=224734058693",
        "GCP_REGION=europe-west1",
        "WORKER_MIG=t-workers",
        "WORKER_MIG_ZONE=europe-west1-b",
        "QUEUE_NAME=simulation_jobs",
        "CLOUDRUN_API_SERVICE=t-api",
        "CLOUDRUN_ADMIN_SERVICE=t-admin",
    ):
        assert expected in deploy, f"{expected} missing from the admin service environment"


def test_the_project_number_is_present_because_writes_depend_on_it(run):
    # GCP_PROJECT_NUMBER is half of the audience IAP signs its assertion for. Without it the console
    # cannot verify who is making a change, and it refuses every mutation rather than trusting the
    # plain email header.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "GCP_PROJECT_NUMBER=224734058693" in _deploy_line(calls)


def test_a_deployment_with_no_fleet_still_deploys(run):
    done, calls = run({"WORKER_MIG": "", "WORKER_MIG_ZONE": ""})
    assert done.returncode == 0, done.stderr
    assert "run deploy t-admin" in calls


def test_the_apis_the_console_reads_are_enabled(run):
    # A role grants permission to CALL an API; it does not turn the API on. A disabled API answers
    # PERMISSION_DENIED with a message about enablement that reads exactly like a missing role -
    # which is what the first real dev deploy produced on the Costs page while every binding it
    # named was correct.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    for api in ("cloudbilling.googleapis.com", "billingbudgets.googleapis.com"):
        assert f"services enable {api}" in calls, f"{api} is never enabled, so the Costs page 403s"


def test_read_roles_are_granted(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    for role in ("roles/compute.viewer", "roles/monitoring.viewer", "roles/run.viewer"):
        assert role in calls, f"{role} was not granted; the pages that need it render an error"


def test_no_project_binding_is_attempted_for_a_billing_account_role(run):
    # roles/billing.viewer exists on a billing ACCOUNT, not on a project: the API refuses it with
    # "Role roles/billing.viewer is not supported for this resource". Attempting it on the project
    # could only ever emit a warning that never becomes a grant, which trains an operator to ignore
    # the warnings that do matter.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    project_bindings = [line for line in calls.splitlines() if "projects add-iam-policy-binding" in line]
    assert project_bindings, "no project bindings were attempted at all"
    assert not any("roles/billing.viewer" in line for line in project_bindings)
    # It must still be stated, as the one grant a human has to make.
    assert "roles/billing.viewer" in done.stdout


def test_a_failed_custom_role_is_not_reported_as_created(run):
    # The summary line is read as the record of what happened. Saying "created" for a call that
    # failed contradicts the warning printed three lines above it.
    done, _calls = run(fake={"FAKE_ROLE_RC": "1", "FAKE_IAM_ROLE_CREATE_FAILS": "1"})
    combined = done.stdout + done.stderr
    assert "could not create the custom role" in combined, "the fake did not reach the failure path"

    authority = next(line for line in combined.splitlines() if "custom role" in line)
    assert "(created)" not in authority, (
        f"the summary claims the role was created while the warning says it was not: {authority!r}")
    assert "ABSENT" in authority


def test_the_write_authority_is_a_custom_role_not_a_broad_one(run):
    # roles/compute.instanceAdmin.v1 would work and would also grant disk and instance CREATION
    # this console never performs. roles/run.admin would let it deploy arbitrary revisions.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "iam roles create" in calls or "iam roles update" in calls
    for forbidden in ("roles/compute.instanceAdmin", "roles/run.admin", "roles/editor", "roles/owner"):
        assert forbidden not in calls, f"{forbidden} is broader than this console's pages need"


def test_the_custom_role_carries_the_mutations_the_controls_make(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    for permission in (
        "compute.autoscalers.update",
        "compute.instanceGroupManagers.update",
        "compute.instanceTemplates.get",
        "compute.zoneOperations.get",
        "run.services.update",
    ):
        assert permission in calls, f"{permission} missing; the control that needs it 403s"


def test_an_existing_custom_role_is_updated_rather_than_recreated(run):
    done, calls = run(fake={"FAKE_ROLE_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "iam roles update" in calls
    assert "iam roles create" not in calls


def test_bigquery_access_is_granted_only_where_a_billing_export_exists(run):
    without, without_calls = run()
    assert without.returncode == 0, without.stderr
    assert "bigquery" not in without_calls.lower(), (
        "a grant for a billing export this deployment does not have is authority nobody asked for")

    with_export, with_calls = run({"BILLING_EXPORT_TABLE": "acct.export.gcp_billing_export_v1_ABC"})
    assert with_export.returncode == 0, with_export.stderr
    assert "roles/bigquery.jobUser" in with_calls
    assert "BILLING_EXPORT_TABLE=acct.export.gcp_billing_export_v1_ABC" in _deploy_line(with_calls)


def test_an_iam_failure_does_not_abort_the_rollout(run):
    # A deploy identity may not hold resourcemanager.projectIamAdmin. The grant is attempted, the
    # failure is reported with the command that fixes it, and the console itself is the verdict -
    # a page that cannot read its metric says so.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "run deploy" in calls
