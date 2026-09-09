# Responsibility: Verify a prod deployment cannot be configured to accept self-asserted identities.
# Boundaries: read-only validation; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"
PROD_ENV = REPO / "deploy" / "gcp" / "generated.prod.env"

# The unconditionally-required config validate-config.sh checks before anything else. Copied from
# the working sibling fixture in test_console_config_validation.py - including the three
# MESH_*_DISPOSITION entries validate-config.sh's own required-config loop demands unconditionally.
_BASE = {
    "GCP_PROJECT_ID": "hexera-prod", "GCP_PROJECT_NUMBER": "688073002171",
    "GCP_REGION": "us-central1", "ARTIFACT_REGISTRY_REPOSITORY": "mesh",
    "CLOUDRUN_MESH_JOB": "prod-mesh", "MESH_SERVICE_ACCOUNT": "prod-mesh",
    "GCP_MESH_BUCKET": "prod-exchange-688073002171",
    "MESH_JOB_DISPOSITION": "created", "MESH_SA_DISPOSITION": "created",
    "MESH_BUCKET_DISPOSITION": "created",
}


def _validate(over: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    env_file = tmp_path / "generated.env"
    env_file.write_text(
        "\n".join(f'{k}="{v}"' for k, v in {**_BASE, **over}.items()) + "\n", encoding="utf-8")
    scrubbed = {k: v for k, v in os.environ.items() if not k.startswith("MINIO_")}
    return subprocess.run(["bash", str(VALIDATE)], capture_output=True, text=True,
                          env={**scrubbed, "DEPLOY_ENV_FILE": str(env_file)})


def test_prod_with_no_app_env_is_refused(tmp_path):
    """create-api-service.sh defaults APP_ENV to dev, and settings/policy.py exempts dev from every
    hardening guard - so an unset APP_ENV on prod is an API that accepts any X-User-Id sent to it."""
    done = _validate({"DEPLOYMENT_ID": "prod"}, tmp_path)
    assert done.returncode != 0
    assert "APP_ENV" in done.stdout + done.stderr


def test_prod_with_a_dev_app_env_is_refused(tmp_path):
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "dev"}, tmp_path)
    assert done.returncode != 0
    assert "APP_ENV" in done.stdout + done.stderr


def test_a_hardened_prod_passes(tmp_path):
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "prod"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_prod_with_a_mixed_case_dev_app_env_is_refused(tmp_path):
    """settings/policy.py lower-cases ENV before matching its dev allowlist, so APP_ENV=Dev reaches
    the API as `dev` and is unhardened at runtime - a case-sensitive check here would wrongly pass
    it, which is the exact bypass this test guards against."""
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "Dev"}, tmp_path)
    assert done.returncode != 0
    assert "APP_ENV" in done.stdout + done.stderr


def test_prod_with_another_mixed_case_dev_app_env_is_refused(tmp_path):
    """Same bypass as above, exercised against a different member of the dev allowlist (CI) to show
    the fix is a case-insensitive match against the whole set, not a one-off for `dev` alone."""
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "CI"}, tmp_path)
    assert done.returncode != 0
    assert "APP_ENV" in done.stdout + done.stderr


def test_a_hardened_prod_with_unusual_casing_still_passes(tmp_path):
    """The case-insensitive match must only widen the dev allowlist, not swallow values that don't
    belong to it - APP_ENV=PROD is not a member under any casing and must still be accepted."""
    done = _validate({"DEPLOYMENT_ID": "prod", "APP_ENV": "PROD"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_dev_is_unaffected(tmp_path):
    done = _validate({"DEPLOYMENT_ID": "dev"}, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_prod_env_file_is_hardened():
    text = PROD_ENV.read_text(encoding="utf-8")
    assert "\nAPP_ENV=prod" in text or text.startswith("APP_ENV=prod"), (
        "generated.prod.env does not set APP_ENV=prod, so create-api-service.sh's default of "
        "'dev' applies and every hardening guard is skipped in production")
    for name in ("MESH_API_KEY_SECRET", "USER_TOKEN_SECRET_SECRET"):
        line = [l for l in text.splitlines() if l.startswith(f"{name}=")]
        assert line and line[0] != f"{name}=", (
            f"{name} is empty in prod; settings/policy.py refuses to import for a hardened "
            f"environment missing it, so prod would not start - or worse, would start unhardened")
