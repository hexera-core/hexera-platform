# Responsibility: Verify discovery reuses a matching environment, rejects a stale one, and never guesses a name.
from __future__ import annotations

import os
import pathlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"

# A fake gcloud whose answers come entirely from FAKE_* env vars, so each test shapes the live
# project it discovers. It answers only the calls discovery makes (no auth/billing - that is
# preflight). Multi-line lists are passed as literal newlines in the env var.
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
ARGS="$*"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "config get-value project"; then printf '%s\n' "${FAKE_PROJECT:-fake-proj}"; exit 0; fi
if has "config get-value run/region"; then printf '%s\n' "${FAKE_RUN_REGION:-us-fake1}"; exit 0; fi
if has "projects describe"; then [ -n "${FAKE_DENY_PROJECT:-}" ] && exit 1; printf '%s\n' "${FAKE_PROJECT_NUMBER:-123456}"; exit 0; fi
if has "run jobs list"; then
  if has "cloud.googleapis.com/location"; then printf '%s\n' "${FAKE_JOB_REGION:-us-fake1}"; exit 0; fi
  [ -n "${FAKE_DENY_JOBS_LIST:-}" ] && exit 1
  printf '%b' "${FAKE_MESH_JOBS:-}"; exit 0
fi
if has "run jobs describe"; then
  [ -n "${FAKE_DENY_DESCRIBE:-}" ] && exit 1
  printf '%s\n' "${FAKE_MESH_SA:-mesh-sa@fake-proj.iam.gserviceaccount.com}"; exit 0
fi
if has "storage buckets list"; then printf '%b' "${FAKE_BUCKETS:-}"; exit 0; fi
if has "storage buckets describe"; then [ -n "${FAKE_BUCKET_ABSENT:-}" ] && exit 1; exit 0; fi
exit 0
"""


@pytest.fixture()
def gcloud_env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gcloud").write_text(_FAKE_GCLOUD)
    (bindir / "gcloud").chmod(0o755)
    base = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "DEPLOY_ENV_FILE": str(tmp_path / "generated.env"),
        "FAKE_PROJECT": "fake-proj",
        "FAKE_RUN_REGION": "us-fake1",
    }
    return base, tmp_path


def run(base, extra, *args):
    return subprocess.run(["bash", str(BOOTSTRAP), *args],
                          env={**base, **extra}, capture_output=True, text=True, timeout=60)


def gen(tmp_path) -> dict[str, str]:
    f = tmp_path / "generated.env"
    out = {}
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


# candidate counting







def test_an_explicit_mesh_job_overrides_discovery(gcloud_env):
    base, tmp = gcloud_env
    # even with several candidates, an explicit value wins (and is validated by describe)
    p = run(base, {"FAKE_MESH_JOBS": "a-mesh\nb-mesh\n", "FAKE_BUCKETS": "fake-xfer\n",
                   "CLOUDRUN_MESH_JOB": "the-real-mesh-job"})
    assert p.returncode == 0, p.stderr
    assert gen(tmp)["CLOUDRUN_MESH_JOB"] == "the-real-mesh-job"


# the project-name-contains-"mesh" trap the bucket fix closes





# malformed output & missing permission

def test_unreadable_project_fails_clearly(gcloud_env):
    base, tmp = gcloud_env
    p = run(base, {"FAKE_DENY_PROJECT": "1", "FAKE_MESH_JOBS": "the-mesh-job\n"})
    assert p.returncode != 0
    assert "cannot read project" in (p.stdout + p.stderr)




# stale generated.env (bound to a project/region that is no longer active)

def _seed_generated(tmp_path, project, region):
    (tmp_path / "generated.env").write_text(
        f"GCP_PROJECT_ID={project}\nGCP_PROJECT_NUMBER=1\nGCP_REGION={region}\n"
        "CLOUDRUN_MESH_JOB=the-mesh-job\nGCP_MESH_BUCKET=fake-xfer\n")


def test_stale_generated_env_wrong_project_is_rejected(gcloud_env):
    base, tmp = gcloud_env
    _seed_generated(tmp, project="old-project", region="us-fake1")
    p = run(base, {"FAKE_PROJECT": "new-project", "FAKE_MESH_JOBS": "the-mesh-job\n"})
    assert p.returncode != 0
    out = p.stdout + p.stderr
    assert "bound to project 'old-project'" in out and "new-project" in out
    assert "--force" in out


def test_stale_generated_env_wrong_region_is_rejected(gcloud_env):
    base, tmp = gcloud_env
    _seed_generated(tmp, project="fake-proj", region="us-fake1")
    p = run(base, {"FAKE_PROJECT": "fake-proj", "GCP_REGION": "eu-other1",
                   "FAKE_MESH_JOBS": "the-mesh-job\n"})
    assert p.returncode != 0
    assert "bound to region 'us-fake1'" in (p.stdout + p.stderr)


def test_reuse_is_accepted_when_project_and_region_still_match(gcloud_env):
    base, tmp = gcloud_env
    _seed_generated(tmp, project="fake-proj", region="us-fake1")
    p = run(base, {"FAKE_PROJECT": "fake-proj", "FAKE_MESH_JOBS": "the-mesh-job\n",
                   "FAKE_BUCKETS": "fake-xfer\n"})
    assert p.returncode == 0, p.stdout + p.stderr
    assert gen(tmp)["GCP_PROJECT_ID"] == "fake-proj"


def test_an_absent_mesh_job_is_marked_for_creation(gcloud_env):
    base, _ = gcloud_env
    r = run(base, {"FAKE_DENY_DESCRIBE": "1", "FAKE_BUCKET_ABSENT": "1"})
    assert r.returncode == 0, r.stderr[-400:]
    env = (pathlib.Path(base["DEPLOY_ENV_FILE"])).read_text()
    assert "MESH_JOB_DISPOSITION=created" in env
    assert "MESH_BUCKET_DISPOSITION=created" in env


def test_an_existing_mesh_job_is_reused_rather_than_recreated(gcloud_env):
    base, _ = gcloud_env
    r = run(base, {"CLOUDRUN_MESH_JOB": "acme-mesh"})
    assert r.returncode == 0, r.stderr[-400:]
    env = (pathlib.Path(base["DEPLOY_ENV_FILE"])).read_text()
    assert "MESH_JOB_DISPOSITION=reused" in env
