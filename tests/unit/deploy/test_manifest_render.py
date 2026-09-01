# Responsibility: Verify every Cloud Run manifest renders from the exported variables and pins its image by digest.
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
MANIFESTS = REPO / "deploy" / "gcp" / "cloud-run"

# The variables the deploy scripts export before envsubst renders a manifest. A manifest that
# names one that is NOT here fails the first test below - which is the point: envsubst substitutes
# an unset variable with the empty string, so the only place a typo'd or unexported variable can be
# caught is here, before a deploy applies a spec with a hole in it.
_VARS = {
    "DEPLOYMENT_ID": "amp-dev",
    "GCP_PROJECT_ID": "fake-proj", "GCP_REGION": "us-central1",
    "CLOUDRUN_MESH_JOB": "amp-dev-mesh",
    "MESH_IMAGE": "us-central1-docker.pkg.dev/fake-proj/mesh/mesh@sha256:dead",
    "MESH_SA_EMAIL": "amp-dev-mesh@fake-proj.iam.gserviceaccount.com",
    "MESH_CPU": "4", "MESH_MEMORY": "8Gi", "MESH_TIMEOUT_SECONDS": "14400",
    # the application image, and the two jobs that run application code from it
    "APP_IMAGE": "us-central1-docker.pkg.dev/fake-proj/mesh/app@sha256:beef",
    "VPC_NETWORK": "default", "VPC_SUBNET": "default",
    "CLOUDRUN_MIGRATE_JOB": "amp-dev-migrate",
    "MIGRATE_SA_EMAIL": "amp-dev-migrate@fake-proj.iam.gserviceaccount.com",
    "MIGRATE_DB_HOST": "10.66.0.3", "MIGRATE_DB_PORT": "5432",
    "MIGRATE_DB_NAME": "meshpipeline", "MIGRATE_DB_USER": "meshpipeline",
    "POSTGRES_PASSWORD_SECRET": "postgres-password",
    "CLOUDRUN_QUEUE_DEPTH_JOB": "amp-dev-queue-depth",
    "QUEUE_DEPTH_SA_EMAIL": "amp-dev-queue-depth@fake-proj.iam.gserviceaccount.com",
    "QUEUE_DEPTH_PROGRAM_B64": "cHJpbnQoMCk=",
    "REDIS_URL": "redis://10.108.144.235:6379/0", "QUEUE_NAME": "simulation_jobs",
    "WORKER_MIG_ZONE": "us-central1-a",
}
_TOKEN = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _render(text: str) -> str:
    # An UNKNOWN variable is left as it was written, so the assertion below can see it. Rendering it
    # to "" instead - which is what envsubst would do - made that assertion unfalsifiable.
    return _TOKEN.sub(lambda m: _VARS.get(m.group(1) or m.group(2), m.group(0)), text)


def _manifests() -> list[Path]:
    return sorted(MANIFESTS.glob("*.yaml"))


def test_there_is_a_manifest_to_render():
    assert _manifests(), f"no Cloud Run manifest found under {MANIFESTS}"


@pytest.mark.parametrize("path", _manifests(), ids=lambda p: p.name)
def test_a_manifest_renders_with_the_exported_variables_and_parses(path):
    rendered = _render(path.read_text())
    assert not _TOKEN.search(rendered), (
        f"{path.name} references a variable the deploy scripts do not export: "
        f"{sorted({m.group(1) or m.group(2) for m in _TOKEN.finditer(rendered)})}")
    doc = yaml.safe_load(rendered)
    assert isinstance(doc, dict) and doc.get("kind"), f"{path.name} did not parse as a Cloud Run resource"


@pytest.mark.parametrize("path", _manifests(), ids=lambda p: p.name)
def test_a_manifest_pins_an_image_by_digest(path):
    rendered = _render(path.read_text())
    for image in re.findall(r"^\s*-?\s*image:\s*(\S+)\s*$", rendered, re.M):
        assert "@sha256:" in image, f"{path.name} renders a mutable tag: {image}"
