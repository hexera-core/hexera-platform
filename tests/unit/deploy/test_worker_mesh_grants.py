# Responsibility: Pin that the worker fleet's own identity may start its environment's mesh job and use its exchange bucket.
# Boundaries: a static read of create-worker-fleet.sh plus one run of its grant block under bash with gcloud faked; nothing in the cloud is touched.
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
FLEET = (REPO / "deploy" / "gcp" / "scripts" / "create-worker-fleet.sh").read_text(encoding="utf-8")

# THE DEFECT. The worker hands every mesh to the environment's Cloud Run job. apply-iam.sh grants
# that caller only when MESH_INVOKER is set, which no deploy does, so a personal environment's fleet
# (the default compute account) could start nothing: dev-areen job c337ad32 failed in 6 s with no
# execution created (2026-10-11). Shared dev's two bindings had been made by hand.


def _grant_block() -> str:
    start = FLEET.index("# 2b) THE MESH EXECUTOR'S CALLER.")
    return FLEET[start:FLEET.index("# 3) the metadata the instance reads.", start)]


def test_the_fleet_identity_is_granted_the_mesh_job_and_the_exchange_bucket():
    block = _grant_block()
    assert 'run jobs add-iam-policy-binding "${CLOUDRUN_MESH_JOB}"' in block
    assert "roles/run.jobsExecutorWithOverrides" in block
    assert 'storage buckets add-iam-policy-binding "gs://${GCP_MESH_BUCKET}"' in block
    assert "roles/storage.objectUser" in block
    assert block.count('--member "serviceAccount:${WORKER_SA_EMAIL}"') == 2
    # after the identity is settled, before the template is written
    assert FLEET.index('WORKER_SA_EMAIL="${GCP_PROJECT_NUMBER}-compute') < FLEET.index(block)
    assert FLEET.index(block) < FLEET.index("WORKER_METADATA=(")


def _run(env: dict, fail: str = "") -> tuple[list[str], str]:
    calls = Path("/tmp") / f"gc_calls_{abs(hash(str(env)))}.txt"
    calls.unlink(missing_ok=True)
    script = (
        f'gc() {{ echo "$*" >> {calls}; case "$*" in *"{fail or "NEVERMATCH"}"*) return 1;; esac; }}\n'
        'log() { echo "LOG $*"; }\nwarn() { echo "WARN $*"; }\n'
        + _grant_block())
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", **env})
    assert done.returncode == 0, done.stderr
    made = calls.read_text().splitlines() if calls.exists() else []
    calls.unlink(missing_ok=True)
    return made, done.stdout


ENV = {"CLOUDRUN_MESH_JOB": "dev-x-mesh", "GCP_MESH_BUCKET": "dev-x-exchange-1", "GCP_REGION": "us-central1",
       "GCP_PROJECT_ID": "p", "WORKER_SA_EMAIL": "1-compute@developer.gserviceaccount.com"}


def test_both_bindings_are_made_for_this_environment():
    made, out = _run(ENV)
    assert any("run jobs add-iam-policy-binding dev-x-mesh" in c and "jobsExecutorWithOverrides" in c
               and "serviceAccount:1-compute@developer.gserviceaccount.com" in c for c in made), made
    assert any("storage buckets add-iam-policy-binding gs://dev-x-exchange-1" in c and "objectUser" in c
               for c in made), made
    assert "WARN" not in out


def test_a_refused_binding_warns_with_the_command_and_does_not_stop_the_deploy():
    made, out = _run(ENV, fail="run jobs")
    assert "WARN could not let" in out and "gcloud run jobs add-iam-policy-binding dev-x-mesh" in out
    assert any("storage buckets" in c for c in made), "the bucket grant is still attempted"


def test_no_mesh_executor_grants_nothing():
    made, out = _run({**ENV, "CLOUDRUN_MESH_JOB": ""})
    assert made == [] and "no mesh executor" in out
