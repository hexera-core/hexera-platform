# Responsibility: Verify every Cloud Run manifest renders from the exported variables and pins its image by digest.
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
MANIFESTS = REPO / "deploy" / "gcp" / "cloud-run"

# The variables deploy.sh exports before envsubst renders a manifest.
_VARS = {
    "DEPLOYMENT_ID": "amp-dev",
    "GCP_PROJECT_ID": "fake-proj", "GCP_REGION": "us-central1",
    "CLOUDRUN_MESH_JOB": "amp-dev-mesh",
    "MESH_IMAGE": "us-central1-docker.pkg.dev/fake-proj/mesh/mesh@sha256:dead",
    "MESH_SA_EMAIL": "amp-dev-mesh@fake-proj.iam.gserviceaccount.com",
    "MESH_CPU": "4", "MESH_MEMORY": "8Gi", "MESH_TIMEOUT_SECONDS": "14400",
}
_TOKEN = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _render(text: str) -> str:
    return _TOKEN.sub(lambda m: _VARS.get(m.group(1) or m.group(2), ""), text)


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
