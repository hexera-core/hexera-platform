# Responsibility: Verify 'edge' is selectable, ordered last, and its address is recorded.
# Boundaries: it reads the driver and the state writer; it runs no deploy.
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
DEPLOY = REPO / "deploy" / "gcp" / "scripts" / "deploy.sh"
WRITER = REPO / "deploy" / "gcp" / "scripts" / "write-deployment-state.sh"


def test_edge_is_a_known_component():
    known = [l for l in DEPLOY.read_text(encoding="utf-8").splitlines()
             if l.strip().startswith("_known=")]
    assert known and "edge" in known[0]


def test_the_edge_stage_runs_the_edge_script():
    assert "create-edge.sh" in DEPLOY.read_text(encoding="utf-8")


def test_an_unselected_edge_states_its_skip():
    assert "skipped edge" in DEPLOY.read_text(encoding="utf-8")


def test_the_edge_runs_after_the_services_it_fronts():
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("create-console-service.sh") < text.index("create-edge.sh")
    assert text.index("create-admin-service.sh") < text.index("create-edge.sh")


def test_the_reserved_address_is_recorded():
    text = WRITER.read_text(encoding="utf-8")
    assert '"edge"' in text, (
        "the manifest records no edge, so the address an operator put in DNS survives only in the "
        "log of the run that printed it")
    assert "EDGE_IP_ADDRESS" in text


_FAKE_GCLOUD = r"""#!/usr/bin/env bash
set -euo pipefail
STATE="${FAKE_GCP_STATE:?}"; mkdir -p "${STATE}"
ARGS="$*"; printf '%s\n' "${ARGS}" >> "${STATE}/calls.log"
case "${ARGS}" in
  *"compute addresses describe"*)         printf '%s\n' "203.0.113.44"; exit 0 ;;
  *"compute ssl-certificates describe"*)  printf '%s\n' "ACTIVE"; exit 0 ;;
esac
exit 0
"""

# generated.env is what bootstrap-env.sh writes, so EDGE_IP_NAME reaches the manifest writer the
# same way EDGE_CERT does - which is the whole reason the address can be read rather than inherited.
_ENV = {
    "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj", "GCP_PROJECT_NUMBER": "224734058693",
    "GCP_REGION": "us-central1", "MESH_SERVICE_ACCOUNT": "t-mesh",
    "ARTIFACT_REGISTRY_REPOSITORY": "t-repo", "CLOUDRUN_MESH_JOB": "t-mesh-job",
    "GCP_MESH_BUCKET": "t-bucket", "MESH_BUCKET_DISPOSITION": "created",
    "MESH_SA_DISPOSITION": "created",
    "CONSOLE_DOMAIN": "dev.console.hexera.ai", "ADMIN_DOMAIN": "dev.admin.hexera.ai",
    "EDGE_IP_NAME": "t-edge-ip", "EDGE_URL_MAP": "t-edge", "EDGE_CERT": "t-edge-cert",
}


def _write_manifest(tmp_path, over=None, extra_env=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gcloud").write_text(_FAKE_GCLOUD, encoding="utf-8")
    (bin_dir / "gcloud").chmod(0o755)
    vals = {**_ENV, **(over or {})}
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f'{k}="{v}"' for k, v in vals.items() if v != "") + "\n", encoding="utf-8")
    out_dir = tmp_path / "out"
    done = subprocess.run(
        ["bash", str(WRITER)], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "FAKE_GCP_STATE": str(tmp_path / "state"), "DEPLOY_ENV_FILE": str(env_file),
             "DEPLOY_OUTPUT_DIR": str(out_dir), "DEPLOY_COMPONENTS": "edge",
             **(extra_env or {})})
    assert done.returncode == 0, done.stderr
    manifest = out_dir / "deployment.json"
    calls = tmp_path / "state" / "calls.log"
    return (json.loads(manifest.read_text(encoding="utf-8")),
            calls.read_text(encoding="utf-8") if calls.exists() else "")


def test_the_recorded_address_is_the_one_actually_reserved(tmp_path):
    """create-edge.sh runs as its OWN `bash` process, so its `export EDGE_IP_ADDRESS` never
    reaches this script and the manifest field the edge exists to publish was empty on every real
    deploy. The address is read back from EDGE_IP_NAME instead - so a stale inherited value must
    lose to what is actually reserved."""
    doc, calls = _write_manifest(tmp_path, extra_env={"EDGE_IP_ADDRESS": "198.51.100.9"})
    assert "compute addresses describe t-edge-ip --global" in calls, (
        "the address was never read back; the manifest can only be repeating what it was handed")
    assert doc["resources"]["edge"]["address"] == "203.0.113.44"
    assert doc["resources"]["edge"]["certificate_state"] == "ACTIVE"


def test_an_address_that_cannot_be_read_is_empty_rather_than_fatal(tmp_path):
    """A read-back that fails is tolerated exactly as the certificate's is: a manifest is worth
    writing even when one lookup did not answer."""
    bad = tmp_path / "badbin"
    bad.mkdir()
    (bad / "gcloud").write_text("#!/usr/bin/env bash\nexit 7\n", encoding="utf-8")
    (bad / "gcloud").chmod(0o755)
    doc, _ = _write_manifest(tmp_path, extra_env={"PATH": f"{bad}:{os.environ['PATH']}"})
    assert doc["resources"]["edge"]["address"] == ""


def test_a_deployment_with_no_hostname_asks_for_no_address(tmp_path):
    doc, calls = _write_manifest(tmp_path, over={"CONSOLE_DOMAIN": "", "ADMIN_DOMAIN": ""})
    assert "edge" not in doc["resources"]
    assert "addresses describe" not in calls
