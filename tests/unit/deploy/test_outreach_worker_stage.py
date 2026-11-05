# Responsibility: Verify the outreach sender is provisioned dry, scheduled sanely, and never armed by a deploy.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-outreach-worker.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "run jobs describe"; then exit "${FAKE_JOB_RC:-1}"; fi
if has "scheduler jobs describe"; then exit "${FAKE_SCHED_RC:-1}"; fi
exit 0
"""

_ENV = {
    "ADMIN_IMAGE": "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:c0ffee",
    "ADMIN_SERVICE_ACCOUNT": "t-admin",
    "APP_ENV": "prod",
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "1",
    "GCP_REGION": "us-central1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "OUTREACH_DB_HOST": "/cloudsql/fake-proj:us-central1:hexera",
    "OUTREACH_DB_USER": "outreach",
    "OUTREACH_WORKER_JOB": "t-outreach",
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
        env_file.write_text("\n".join(f"{k}={v}" for k, v in vals.items() if v != "") + "\n", encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file), **(fake or {})})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


def test_a_deployment_with_no_outreach_is_a_stated_skip(run):
    done, calls = run({"OUTREACH_WORKER_JOB": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run jobs create" not in calls


def test_it_is_provisioned_dry_by_default(run):
    # THE ASSERTION THAT MATTERS MOST IN THIS FILE. A fresh provision must be a rehearsal: the one
    # thing in this deploy that can email a stranger must never arrive armed.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "DRY_RUN=1" in calls
    assert "DRY_RUN=0" not in calls
    assert "DRY_RUN=false" not in calls


def test_arming_the_first_switch_is_announced_and_still_not_enough(run):
    done, calls = run({"DRY_RUN": "0"})
    assert done.returncode == 0, done.stderr
    assert "DRY_RUN=0" in calls
    combined = done.stdout + done.stderr
    # A deploy that permits sending says so loudly, and says the other switch still has to agree.
    assert "live_sending" in combined
    assert "WARN" in combined or "warn" in combined.lower()


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"ADMIN_IMAGE": "us-central1-docker.pkg.dev/p/r/admin:v1"})
    assert done.returncode != 0
    assert "run jobs create" not in calls


def test_it_retries_at_most_once(run):
    # A tick that failed halfway has already sent whatever it sent. Three retries invite a
    # duplicate send; one covers a transient database blip.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--max-retries 1" in calls


def test_the_schedule_is_business_hours_on_weekdays(run):
    # A cold email arriving at 3am on a Sunday reads as a blast. The engine has per-campaign send
    # windows; this is a second, coarser guard on the same thing.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "8-17" in calls and "1-5" in calls


def test_it_runs_as_the_console_identity(run):
    # The job reads the same tables and opens the same KMS-sealed token. A second identity would
    # need the same grants and would double the places a mailbox credential can be reached from.
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "t-admin@fake-proj.iam.gserviceaccount.com" in calls


def test_an_existing_job_is_updated_not_recreated(run):
    done, calls = run(fake={"FAKE_JOB_RC": "0"})
    assert done.returncode == 0, done.stderr
    assert "run jobs update" in calls
    assert "run jobs create" not in calls
