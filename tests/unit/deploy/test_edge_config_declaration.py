# Responsibility: Verify the deployment env declares the edge tier and that an absent one is legal.
# Boundaries: it reads the generator and the validator; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"

DECLARED = ("CONSOLE_DOMAIN", "ADMIN_DOMAIN", "EDGE_IP_NAME", "EDGE_URL_MAP", "EDGE_CERT")

_BASE = {
    # validate-config.sh's own required-config loop, plus MESH_SERVICE_ACCOUNT which lib.sh's
    # load_env derefs unconditionally - the same complete set test_admin_config_declaration.py
    # supplies for the same reason, so failures here are about the edge tier, not this baseline.
    "DEPLOYMENT_ID": "dev", "GCP_PROJECT_ID": "hexera-dev",
    "GCP_PROJECT_NUMBER": "224734058693", "GCP_REGION": "us-central1",
    "ARTIFACT_REGISTRY_REPOSITORY": "mesh", "CLOUDRUN_MESH_JOB": "dev-mesh",
    "MESH_SERVICE_ACCOUNT": "dev-mesh", "GCP_MESH_BUCKET": "dev-exchange-224734058693",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created",
    "MESH_BUCKET_DISPOSITION": "created",
}


def test_every_edge_setting_is_declared():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    missing = [n for n in DECLARED if f"{n}=" not in text]
    assert not missing, f"the deployment env declares no {missing}"


def _validate(over: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f'{k}="{v}"' for k, v in {**_BASE, **over}.items()) + "\n", encoding="utf-8")
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("MINIO_")}
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**scrubbed, "DEPLOY_ENV_FILE": str(env_file)})


def test_a_domain_without_its_service_is_refused(tmp_path):
    """A hostname with nothing behind it produces a load balancer that 404s on a real name."""
    done = _validate({"CONSOLE_DOMAIN": "dev.console.hexera.ai"}, tmp_path)
    assert done.returncode != 0
    assert "CLOUDRUN_CONSOLE_SERVICE" in done.stdout + done.stderr


def test_a_domain_with_its_service_passes(tmp_path):
    # Declaring CLOUDRUN_CONSOLE_SERVICE also engages the console tier's own checks
    # (test_console_config_validation.py), so those are satisfied here too - this test is only
    # about the edge refusal, not about re-proving the console tier is independently well-formed.
    done = _validate({"CONSOLE_DOMAIN": "dev.console.hexera.ai",
                      "CLOUDRUN_CONSOLE_SERVICE": "dev-console",
                      "HEXERA_API_BASE_URL": "https://api.example",
                      "AUTH_SECRET_SECRET": "console-auth-secret",
                      "CONSOLE_AUTH_USERS_SECRET": "console-auth-users"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_an_admin_domain_without_its_service_is_refused(tmp_path):
    """validate-config.sh checks ADMIN_DOMAIN identically to CONSOLE_DOMAIN above, and until this
    test existed only the console half of that pair was covered - so the admin half could have been
    dropped or misspelled without a single test noticing."""
    done = _validate({"ADMIN_DOMAIN": "dev.admin.hexera.ai"}, tmp_path)
    assert done.returncode != 0
    assert "CLOUDRUN_ADMIN_SERVICE" in done.stdout + done.stderr


def test_an_admin_domain_with_its_service_passes(tmp_path):
    # The admin tier carries no console-style prerequisites (its gate is IAP, and it reads no
    # secrets), so naming the service is all this refusal needs satisfied - the mirror of
    # test_a_domain_with_its_service_passes above.
    done = _validate({"ADMIN_DOMAIN": "dev.admin.hexera.ai",
                      "CLOUDRUN_ADMIN_SERVICE": "dev-admin"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_no_domains_is_not_an_error(tmp_path):
    done = _validate({}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
