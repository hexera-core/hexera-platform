# Responsibility: Verify the admin rollout is IAP-gated, never public, and digest-pinned.
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
if has "run services describe"; then
  if has "status.url"; then printf '%s\n' "https://t-admin.run.app"; exit 0; fi
  exit "${FAKE_SVC_EXISTS_RC:-1}"
fi
if has "run services get-iam-policy"; then printf '%s\n' "${FAKE_POLICY_MEMBERS:-}"; exit 0; fi
exit 0
"""

_ADMIN_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/admin@sha256:c0ffee"

_ENV = {
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "europe-west1",
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "CLOUDRUN_ADMIN_SERVICE": "t-admin",
    "ADMIN_SERVICE_ACCOUNT": "t-admin",
    "ADMIN_IMAGE": _ADMIN_DIGEST,
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
        vals = {**_ENV, **(over or {})}
        env_file = tmp_path / "generated.env"
        # Quoted: load_env sources this file with `.`, and an unquoted value containing a shell
        # metacharacter (FAKE_POLICY_MEMBERS's ";" - it mirrors gcloud's own
        # `value(bindings.members)` join character) would otherwise be parsed as a second
        # command rather than carried as part of the assignment.
        env_file.write_text("\n".join(f'{k}="{v}"' for k, v in vals.items() if v != "") + "\n",
                            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file)})
        log = state / "calls.log"
        return done, (log.read_text(encoding="utf-8") if log.exists() else "")

    return _run


def test_no_admin_service_is_a_stated_skip(run):
    done, calls = run({"CLOUDRUN_ADMIN_SERVICE": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run deploy" not in calls


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"ADMIN_IMAGE": "us-central1-docker.pkg.dev/p/r/admin:v1"})
    assert done.returncode != 0
    assert "run deploy" not in calls


def test_the_rollout_is_digest_pinned_and_iap_enabled(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert f"--image {_ADMIN_DIGEST}" in calls
    assert "--iap" in calls, "IAP was not enabled on the service"


def test_the_service_is_never_publicly_invokable(run):
    """The single most important assertion in this file."""
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--no-allow-unauthenticated" in calls
    assert "add-iam-policy-binding" not in calls or "allUsers" not in calls, (
        "the admin service was granted a public invoker binding; IAP in front of that protects "
        "nothing, because the two are separate gates")


def test_an_existing_public_binding_is_removed(run):
    done, calls = run({"FAKE_POLICY_MEMBERS": "allUsers;serviceAccount:x@y"})
    assert done.returncode == 0, done.stderr
    assert "remove-iam-policy-binding" in calls and "allUsers" in calls


def test_the_iap_service_agent_is_granted_invoker(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "service-224734058693@gcp-sa-iap.iam.gserviceaccount.com" in calls, (
        "IAP terminates the request and calls Cloud Run itself; without this grant every request "
        "403s in a way that looks like the IAP policy is wrong when it is not")
    assert "roles/run.invoker" in calls


def test_the_url_is_published_for_the_post_deploy_check(run, tmp_path):
    out = tmp_path / "gh_output"
    out.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    env_file = tmp_path / "generated.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in _ENV.items()) + "\n", encoding="utf-8")
    subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                   env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                        "FAKE_GCP_STATE": str(state), "DEPLOY_ENV_FILE": str(env_file),
                        "GITHUB_OUTPUT": str(out)})
    assert "admin_url=https://t-admin.run.app" in out.read_text(encoding="utf-8")
