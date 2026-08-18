# Responsibility: Verify a deploy aborts before any mutation unless the project is typed, or targets are pinned.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"

# Fake gcloud that answers every call deploy makes UP TO the confirmation gate (discovery +
# read-only preflight), logging each call so the test can prove nothing was mutated on abort.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "auth print-access-token"; then echo "fake-token"; exit 0; fi
if has "auth list"; then echo "deployer@example.com"; exit 0; fi
if has "config get-value auth/impersonate_service_account"; then echo "svc@fake-proj.iam.gserviceaccount.com"; exit 0; fi
if has "config get-value project"; then echo "fake-proj"; exit 0; fi
if has "config get-value run/region"; then echo "us-fake1"; exit 0; fi
if has "billing projects describe"; then echo "True"; exit 0; fi
if has "projects describe"; then echo "123456"; exit 0; fi
if has "services list"; then echo "run.googleapis.com"; exit 0; fi
if has "services enable"; then exit 0; fi
if has "run jobs list"; then if has location; then echo "us-fake1"; else echo "fake-mesh-job"; fi; exit 0; fi
if has "run jobs describe"; then echo "mesh-sa@fake-proj.iam.gserviceaccount.com"; exit 0; fi
if has "run services describe"; then echo "https://mesh-api-fake.run.app"; exit 0; fi
if has "storage buckets describe"; then echo "us-fake1"; exit 0; fi
if has "storage buckets list"; then echo "fake-xfer"; exit 0; fi
if has "secrets describe"; then exit 1; fi
if has "secrets versions list"; then exit 0; fi
if has "artifacts"; then exit 1; fi
exit 0
"""

# Fake curl: the read-only testIamPermissions probe reports every required permission granted.
_FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s' '{"permissions":["run.services.setIamPolicy","run.services.create","run.jobs.create","artifactregistry.repositories.create","secretmanager.secrets.create","storage.buckets.create","serviceusage.services.enable","resourcemanager.projects.setIamPolicy","iam.serviceAccounts.create"]}'
"""

# Repository-controlled envsubst so these tests do NOT depend on host-installed gettext (they must
# behave identically in make check, CI, and the API/pipeline images). It faithfully substitutes
# ${VAR} and $VAR from the environment - Python 3 is always present (this is a Python test).
_FAKE_ENVSUBST = r"""#!/usr/bin/env python3
import os, re, sys
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\$(\w+)",
                        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                        sys.stdin.read()))
"""

# Any mutating gcloud verb - none of these may appear in the call log if the deploy aborted.
_MUTATIONS = ("services enable", "repositories create", "service-accounts create",
              "buckets create", "secrets create", "add-iam-policy-binding", "builds submit",
              "services replace", "jobs replace")


@pytest.fixture()
def deploy_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(_FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    (bindir / "curl").write_text(_FAKE_CURL)
    (bindir / "curl").chmod(0o755)
    (bindir / "envsubst").write_text(_FAKE_ENVSUBST)
    (bindir / "envsubst").chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    e = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "FAKE_GCP_STATE": str(state),
        "DEPLOY_ENV_FILE": str(tmp_path / "generated.env"),
    }
    return e, state


def run_deploy(env, input_text=""):
    return subprocess.run(["bash", str(SCRIPTS / "deploy.sh")],
                          env=env, input=input_text, capture_output=True, text=True, timeout=90)


def calls(state):
    f = state / "calls.log"
    return f.read_text() if f.exists() else ""


def test_a_wrong_typed_project_aborts_before_any_mutation(deploy_env):
    e, state = deploy_env
    p = run_deploy(e, input_text="not-the-project\n")
    assert p.returncode != 0
    assert "aborted" in (p.stdout + p.stderr).lower()
    log = calls(state)
    hit = [m for m in _MUTATIONS if m in log]
    assert not hit, f"a mutation ran despite an aborted confirmation: {hit}"


def test_empty_confirmation_aborts_before_any_mutation(deploy_env):
    e, state = deploy_env
    p = run_deploy(e, input_text="\n")
    assert p.returncode != 0
    assert "aborted" in (p.stdout + p.stderr).lower()
    assert not [m for m in _MUTATIONS if m in calls(state)]


def test_correct_typed_project_passes_the_gate(deploy_env):
    e, state = deploy_env
    p = run_deploy(e, input_text="fake-proj\n")
    out = p.stdout + p.stderr
    assert "confirmed" in out, out[-400:]
    # it reached the first mutation (enabling APIs) - the gate opened
    assert "services enable" in calls(state)


def test_noninteractive_requires_explicit_targets(deploy_env):
    e, state = deploy_env
    p = run_deploy({**e, "DEPLOY_NONINTERACTIVE": "1"})
    assert p.returncode != 0
    out = p.stdout + p.stderr
    assert "every deployment target to be explicit" in out
    assert not [m for m in _MUTATIONS if m in calls(state)]
