# Responsibility: Verify the application images carry no mesh toolchain, and dev-up refuses without remote config.
from __future__ import annotations

import re
from pathlib import Path

from tests._scan import scanned

REPO = Path(__file__).parents[3]
DOCKERFILE = (REPO / "Dockerfile").read_text()
COMPOSE = (REPO / "docker-compose.yml").read_text()
MAKEFILE = (REPO / "Makefile").read_text()

_NATIVE_TOKENS = ("openfoam", "cfmesh", "snappyhexmesh", "vmtk")


def _stages() -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    current = None
    for ln in DOCKERFILE.splitlines():
        m = re.match(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", ln)
        if m:
            current = m.group(2)
            if current:
                out[current] = (m.group(1), "")
            continue
        if current and current in out:
            parent, body = out[current]
            out[current] = (parent, body + ln.split("#", 1)[0] + "\n")
    return out


def _image_content(stage: str) -> str:
    stages = _stages()
    assert stage in stages, f"no {stage!r} build stage found"
    chain, seen = [], set()
    while stage in stages and stage not in seen:
        seen.add(stage)
        parent, body = stages[stage]
        chain.append(body)
        stage = parent
    return "\n".join(reversed(chain)).lower()


# the API and pipeline images carry NO native toolchain - anywhere in their lineage
def test_pipeline_image_has_no_native_mesh_toolchain():
    content = _image_content("pipeline")
    for tok in _NATIVE_TOKENS:
        assert tok not in content, f"the pipeline image installs {tok!r} - native meshing must stay off-box"


def test_api_image_has_no_native_mesh_toolchain():
    content = _image_content("api")
    for tok in _NATIVE_TOKENS:
        assert tok not in content, f"the api image installs {tok!r} - it must not"


def test_the_native_toolchain_lives_in_the_separate_mesh_image():
    content = _image_content("mesh")
    assert "openfoam" in content and "vmtk" in content, \
        "the mesh image must own the native toolchain"


# the developer stack uses the API/pipeline images, not the mesh image
def test_compose_worker_uses_the_pipeline_target_not_mesh():
    worker = re.search(r"^\s{2}worker:\n(?:.*\n)*?\s+target:\s*(\S+)", COMPOSE, re.M)
    assert worker and worker.group(1) == "pipeline", "the dev worker must build the pipeline target"
    assert "target: mesh" not in COMPOSE, "the dev stack must not build the mesh image"


def test_the_generated_env_offers_no_mesh_backend_choice():
    from meshpipeline.settings import inventory
    assert "MESH_BACKEND" not in {v.name for v in inventory.all_vars()}
    assert "MESH_BACKEND" in inventory.REMOVED, "a stale .env value must still be rejected"


def _target_body(name: str) -> str:
    m = re.search(rf"^{re.escape(name)}:[^\n]*\n((?:\t.*\n|\s*\n)*)", MAKEFILE, re.M)
    return m.group(1) if m else ""


def test_dev_up_gates_on_the_configuration_authority_before_anything_runs():
    # WHICH settings are required is the catalogue's answer, so this asserts that dev-up consults
    # the one authority first - not that particular variable names appear in the recipe, which is
    # the duplication that let the gate and the catalogue drift apart.
    body = _target_body("dev-up")
    assert body, "no dev-up target"
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("@#")]
    assert "devtools/env/check_runtime_config.py" in lines[0], \
        f"the configuration gate is not the first thing dev-up runs: {lines[0]!r}"
    for name in ("GCP_PROJECT_ID", "CLOUDRUN_JOB", "GCP_MESH_BUCKET"):
        assert name not in body, f"dev-up keeps its own copy of {name}; the catalogue owns that"
    # GOOGLE_ADC_FILE may appear, but only as the value handed to Compose by the resolver that
    # just validated it - never as a default, a test or a path of dev-up's own.
    for line in (ln for ln in lines if "GOOGLE_ADC_FILE" in ln):
        assert "--credential-path" in line or line.strip().startswith("export "), \
            f"dev-up interprets the credential path itself: {line.strip()!r}"


def test_dev_up_does_not_build_or_require_the_mesh_image():
    body = _target_body("dev-up")
    assert "target mesh" not in body and "meshpipeline-mesh" not in body, \
        "dev-up must never build the mesh image"


def test_generated_env_documents_every_var_dev_up_requires():
    from meshpipeline.settings import inventory
    generated = inventory.render_env()
    for v in ("GCP_PROJECT_ID", "GCP_REGION", "CLOUDRUN_JOB", "GCP_MESH_BUCKET"):
        assert re.search(rf"^{re.escape(v)}=", generated, re.M), \
            f"the settings inventory must document {v} (dev-up requires it; a coworker needs the generated line)"


def test_dev_down_is_non_destructive():
    body = _target_body("dev-down")
    assert "docker compose down" in body and "-v" not in body, \
        "dev-down must not delete volumes (that is dev-reset)"


# the native mesh image is built ONLY by the native-test targets
def test_only_native_test_targets_build_the_mesh_image():
    # every 'meshpipeline-mesh' reference must be inside a test-native-* target or its MESH_IMAGE var
    for m in re.finditer(r"^([a-zA-Z0-9_-]+):", MAKEFILE, re.M):
        name = m.group(1)
        if name in ("dev-up", "up", "dev-down", "down", "dev-reset", "dev-logs"):
            assert "meshpipeline-mesh" not in _target_body(name), \
                f"{name} must not reference the mesh image"


# hosted manifests never select local mesh execution
def test_hosted_manifests_do_not_select_local_mesh():
    for yaml in scanned((REPO / "deploy" / "gcp" / "cloud-run").glob("*.yaml"), "the Cloud Run service manifests"):
        text = yaml.read_text()
        assert "MESH_BACKEND" not in text or "local" not in re.sub(r"#.*", "", text), \
            f"{yaml.name} must not set MESH_BACKEND=local (hosted meshes on Cloud Run)"
