# Responsibility: Verify the console rollout is digest-pinned, credential-free in its spec, and skippable.
# Boundaries: it drives the stage against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPT = REPO / "deploy" / "gcp" / "scripts" / "create-console-service.sh"

_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "iam service-accounts describe"; then exit 1; fi
if has "secrets describe"; then exit 0; fi
if has "run services describe"; then exit 1; fi
if has "run services get-iam-policy"; then exit 0; fi
exit 0
"""

_CONSOLE_DIGEST = "us-central1-docker.pkg.dev/fake-proj/hexera/console@sha256:c0ffee"

_ENV = {
    "DEPLOYMENT_ID": "t",
    "GCP_PROJECT_ID": "fake-proj",
    "GCP_REGION": "europe-west1",
    # lib.sh's load_env() unconditionally derives MESH_SA_EMAIL from MESH_SERVICE_ACCOUNT under
    # `set -u`, even for a script - this one - that never reads MESH_SA_EMAIL itself. A real
    # generated.env always carries it (bootstrap-env.sh writes it unconditionally); supplied here
    # for the same reason every other deploy-script test in this directory supplies it.
    "MESH_SERVICE_ACCOUNT": "t-mesh",
    "CLOUDRUN_CONSOLE_SERVICE": "t-console",
    "CONSOLE_SERVICE_ACCOUNT": "t-console",
    "CONSOLE_IMAGE": _CONSOLE_DIGEST,
    "HEXERA_API_BASE_URL": "https://api.example",
    "NEXT_PUBLIC_HEXERA_API_BASE_URL": "https://api.example",
    "AUTH_SECRET_SECRET": "console-auth-secret",
    "CONSOLE_AUTH_USERS_SECRET": "console-auth-users",
    "MESH_API_KEY_SECRET": "mesh-api-key",
    "USER_TOKEN_SECRET_SECRET": "user-token-secret",
}


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    state = tmp_path / "state"

    def _run(over: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
        env_vals = {**_ENV, **(over or {})}
        env_file = tmp_path / "generated.env"
        env_file.write_text(
            "\n".join(f"{k}={v}" for k, v in env_vals.items() if v != "") + "\n",
            encoding="utf-8")
        done = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True,
            env={**os.environ,
                 "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "FAKE_GCP_STATE": str(state),
                 "DEPLOY_ENV_FILE": str(env_file)})
        log_path = state / "calls.log"
        return done, (log_path.read_text(encoding="utf-8") if log_path.exists() else "")

    return _run


def test_no_console_service_is_a_stated_skip(run):
    done, calls = run({"CLOUDRUN_CONSOLE_SERVICE": ""})
    assert done.returncode == 0
    assert "skipping" in done.stdout.lower()
    assert "run deploy" not in calls, "it mutated the cloud for a deployment with no console"


def test_a_tag_only_image_is_refused(run):
    done, calls = run({"CONSOLE_IMAGE": "us-central1-docker.pkg.dev/p/r/console:v1"})
    assert done.returncode != 0
    assert "run deploy" not in calls


def test_the_rollout_is_digest_pinned(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert f"--image {_CONSOLE_DIGEST}" in calls


def test_credentials_reach_the_spec_only_as_references(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "--set-secrets" in calls
    assert "AUTH_SECRET=console-auth-secret:latest" in calls
    assert "CONSOLE_AUTH_USERS=console-auth-users:latest" in calls
    # The env list is declarative and must never carry the values themselves.
    for forbidden in ("AUTH_SECRET=", "CONSOLE_AUTH_USERS="):
        for chunk in calls.split("--set-env-vars")[1:]:
            assert forbidden not in chunk.split("--set-secrets")[0], (
                f"{forbidden!r} appears in the declarative environment, not as a reference")


def test_the_api_origins_reach_the_container(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "HEXERA_API_BASE_URL=https://api.example" in calls
    assert "NEXT_PUBLIC_HEXERA_API_BASE_URL=https://api.example" in calls


def test_the_console_is_publicly_invokable_when_stated(run):
    done, calls = run()
    assert done.returncode == 0, done.stderr
    assert "add-iam-policy-binding t-console" in calls
    assert "allUsers" in calls


def test_the_public_binding_is_removed_when_not_stated(run):
    done, calls = run({"CONSOLE_ALLOW_UNAUTHENTICATED": "0"})
    assert done.returncode == 0, done.stderr
    assert "add-iam-policy-binding" not in calls or "allUsers" not in calls.split(
        "add-iam-policy-binding")[-1]
