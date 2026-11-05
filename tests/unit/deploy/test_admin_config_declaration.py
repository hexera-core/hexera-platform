# Responsibility: Verify the deployment env declares the admin tier and never a public invoker.
# Boundaries: it reads the generator and the validator; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"

DECLARED = (
    "CLOUDRUN_ADMIN_SERVICE", "ADMIN_SERVICE_ACCOUNT", "ADMIN_CPU", "ADMIN_MEMORY",
    "ADMIN_CONCURRENCY", "ADMIN_MIN_INSTANCES", "ADMIN_MAX_INSTANCES", "ADMIN_INGRESS",
    "ADMIN_IMAGE",
)

_BASE = {
    # validate-config.sh's own required-config loop, plus MESH_SERVICE_ACCOUNT which lib.sh's
    # load_env derefs unconditionally - the same complete set test_console_config_validation.py
    # supplies for the same reason, so failures here are about the admin tier, not this baseline.
    "DEPLOYMENT_ID": "t", "GCP_PROJECT_ID": "fake-proj", "GCP_PROJECT_NUMBER": "123456",
    "GCP_REGION": "europe-west1", "ARTIFACT_REGISTRY_REPOSITORY": "mesh",
    "CLOUDRUN_MESH_JOB": "t-mesh", "MESH_SERVICE_ACCOUNT": "t-mesh",
    "GCP_MESH_BUCKET": "t-exchange-123456",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created",
    "MESH_BUCKET_DISPOSITION": "created",
}


def test_every_admin_setting_is_declared():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    missing = [n for n in DECLARED if f"{n}=" not in text]
    assert not missing, f"the deployment env declares no {missing}"


def test_the_admin_image_slot_survives_regeneration():
    assert "ADMIN_IMAGE=${ADMIN_IMAGE:-}" in BOOTSTRAP.read_text(encoding="utf-8"), (
        "without the ${VAR:-} slot every bootstrap run erases the promoted admin digest, "
        "exactly as it once did for CONSOLE_IMAGE")


def test_no_admin_public_invoker_setting_exists():
    # The admin console must never be allUsers-invokable. Not configurable, not defaulted off -
    # absent, so no deployment can turn it on by setting a variable.
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "ADMIN_ALLOW_UNAUTHENTICATED" not in text, (
        "a public-invoker knob exists for the admin tier; IAP over an allUsers binding protects "
        "nothing, so this must not be settable")


def _validate(env_extra: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text("\n".join(f"{k}={v}" for k, v in {**_BASE, **env_extra}.items()) + "\n",
                        encoding="utf-8")
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("MINIO_")}
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**scrubbed, "DEPLOY_ENV_FILE": str(env_file)})


def test_inverted_admin_scaling_is_refused(tmp_path):
    done = _validate({"CLOUDRUN_ADMIN_SERVICE": "t-admin",
                      "ADMIN_MIN_INSTANCES": "4", "ADMIN_MAX_INSTANCES": "2"}, tmp_path)
    assert done.returncode != 0
    assert "ADMIN_MIN_INSTANCES" in done.stdout + done.stderr


def test_no_admin_is_not_an_error(tmp_path):
    done = _validate({}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
