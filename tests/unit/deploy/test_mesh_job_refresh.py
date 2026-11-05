# Responsibility: Verify the mesh tier keeps the mesh job it created current on every deploy, and
# leaves a job somebody supplied exactly as it found it.
# Boundaries: drives create-mesh-tier.sh against a fake gcloud and reads what it would have mutated.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
SCRIPTS = REPO / "deploy" / "gcp" / "scripts"

# THE DECISION UNDER TEST. "Reused" used to mean untouched for every existing mesh job, which
# left the job this tooling had created on the image of the deploy that created it while every
# service around it moved on with each merge - the deploy built a new mesh image every time and
# pointed nothing at it. Ownership settles it: the job carries the managed-by=deploy label its
# manifest stamps, so the deploy knows which jobs are its own to refresh and which are somebody
# else's to leave alone.

DIGEST = "europe-west1-docker.pkg.dev/independent-org-mesh/isolated-deploy/mesh@sha256:" + "ab" * 32

# A fake gcloud that logs every call. The exchange bucket and the mesh job both exist; the job's
# managed-by label is whatever FAKE_MESH_JOB_OWNER says (empty = a job with no label at all).
_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
has(){ case "$ARGS" in *"$1"*) return 0;; *) return 1;; esac; }
if has "storage buckets describe"; then exit 0; fi
if has "run jobs describe"; then
  if has "metadata.labels.managed-by"; then printf '%s\n' "${FAKE_MESH_JOB_OWNER:-}"; exit 0; fi
  echo "sa@x"; exit 0
fi
exit 0
"""

_FAKE_ENVSUBST = r"""#!/usr/bin/env python3
import os, re, sys
sys.stdout.write(re.sub(r"\$\{(\w+)\}|\$(\w+)",
                        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
                        sys.stdin.read()))
"""

ENV = {
    "DEPLOYMENT_ID": "isolated-deploy",
    "GCP_PROJECT_ID": "independent-org-mesh", "GCP_PROJECT_NUMBER": "778899",
    "GCP_REGION": "europe-west1", "ARTIFACT_REGISTRY_REPOSITORY": "isolated-deploy",
    "CLOUDRUN_MESH_JOB": "isolated-deploy-mesh",
    "MESH_SERVICE_ACCOUNT": "isolated-deploy-mesh",
    "GCP_MESH_BUCKET": "isolated-deploy-exchange-778899",
    "MESH_JOB_DISPOSITION": "reused", "MESH_SA_DISPOSITION": "reused", "MESH_BUCKET_DISPOSITION": "reused",
    "MESH_IMAGE": DIGEST,
    "MESH_CPU": "8", "MESH_MEMORY": "16Gi", "MESH_TIMEOUT_SECONDS": "14400",
}


def _run(tmp_path, *, owner: str, overrides: dict | None = None):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (b / "gcloud").write_text(_FAKE_GCLOUD); (b / "gcloud").chmod(0o755)
    (b / "envsubst").write_text(_FAKE_ENVSUBST); (b / "envsubst").chmod(0o755)
    env_file = tmp_path / "generated.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in {**ENV, **(overrides or {})}.items()))
    state = tmp_path / "state"; state.mkdir(exist_ok=True)
    env = {"PATH": f"{b}:{os.environ.get('PATH', '')}", "HOME": str(tmp_path),
           "FAKE_GCP_STATE": str(state), "FAKE_MESH_JOB_OWNER": owner,
           "DEPLOY_ENV_FILE": str(env_file), "ASSUME_YES": "1",
           "RENDER_DIR": str(tmp_path / "rendered"),
           "DEPLOY_OUTPUT_DIR": str(tmp_path / "deploy-output")}
    p = subprocess.run(["bash", str(SCRIPTS / "create-mesh-tier.sh")], env=env,
                       capture_output=True, text=True, timeout=120)
    log = (state / "calls.log").read_text() if (state / "calls.log").exists() else ""
    return p, log


def test_a_job_the_deploy_created_is_refreshed_to_this_release(tmp_path):
    p, log = _run(tmp_path, owner="deploy")
    assert p.returncode == 0, p.stderr
    assert "run jobs replace" in log, "a deploy-managed mesh job was left on its old image"
    rendered = (tmp_path / "rendered" / "mesh-job.yaml").read_text()
    assert DIGEST in rendered and 'cpu: "8"' in rendered and "memory: 16Gi" in rendered
    assert "refreshed" in p.stdout


def test_a_job_somebody_supplied_is_left_exactly_as_it_was(tmp_path):
    p, log = _run(tmp_path, owner="")
    assert p.returncode == 0, p.stderr
    assert "run jobs replace" not in log, "the deploy reconfigured a job it does not own"
    assert "untouched" in p.stdout


def test_a_refresh_needs_the_validated_digest_like_a_creation_does(tmp_path):
    p, log = _run(tmp_path, owner="deploy", overrides={"MESH_IMAGE": ""})
    assert p.returncode != 0
    assert "promote-release.sh" in (p.stdout + p.stderr)
    assert "run jobs replace" not in log

    tag = "europe-west1-docker.pkg.dev/independent-org-mesh/isolated-deploy/mesh:latest"
    p, log = _run(tmp_path, owner="deploy", overrides={"MESH_IMAGE": tag})
    assert p.returncode != 0
    assert "is a TAG, not a digest" in (p.stdout + p.stderr)
    assert "run jobs replace" not in log


def test_a_supplied_job_is_not_asked_for_an_image_at_all(tmp_path):
    p, log = _run(tmp_path, owner="", overrides={"MESH_IMAGE": ""})
    assert p.returncode == 0, p.stderr
    assert "run jobs replace" not in log


def test_preflight_asks_for_update_only_when_it_will_refresh():
    # The read-only preflight derives which permissions this run needs. run.jobs.update belongs to
    # the refresh, so it is asked of a caller whose job carries the label and of nobody else.
    s = (SCRIPTS / "preflight.sh").read_text(encoding="utf-8")
    assert '"run.jobs.update"' in s.split("PERM_LIST=")[1].split("\n")[0]
    assert 'run.jobs.update)' in s and '_MESH_JOB_OWNER' in s
    assert 'metadata.labels.managed-by' in s
