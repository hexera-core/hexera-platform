# Responsibility: Verify a prod deployment cannot be configured to accept self-asserted identities.
# Boundaries: read-only validation; it calls no cloud.
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).parents[3]
VALIDATE = REPO / "deploy" / "gcp" / "scripts" / "validate-config.sh"
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"

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


def test_the_workflow_pins_prods_hardening():
    """The WORKFLOW is the only path that delivers APP_ENV to the API, so this asserts against it.

    This test used to read deploy/gcp/generated.prod.env, and passed while prod was in fact both
    unhardened and undeployable. That file is a committed SNAPSHOT of generated state: nothing in
    deploy.sh, bootstrap-env.sh or this workflow reads it. bootstrap-env.sh REGENERATES the
    deployment env from a fixed heredoc on stage 1, before validate-config.sh reads it on stage 2 -
    so a value written into that snapshot was erased on every run, and the validator below then
    refused the deploy for its absence. Asserting on the snapshot tested a file with no readers.

    The live path is: this workflow's picker -> the deploy job's env: block -> bootstrap-env.sh's
    `${APP_ENV:-dev}` -> create-api-service.sh's ENV/CORS_ORIGINS on the service.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "app_env=prod" in text, (
        "deploy.yml's prod picker does not emit app_env=prod, so bootstrap-env.sh falls back to "
        "create-api-service.sh's default of 'dev' and every hardening guard is skipped in prod")
    assert "api_cors_origins=https://console.hexera.ai" in text, (
        "deploy.yml's prod picker does not narrow the CORS origins, so the API deploys with the "
        "wildcard default a hardened runtime refuses to start under")
    # An output emitted by the picker but never mapped into the deploy job's environment would
    # never reach bootstrap-env.sh - which is precisely the class of gap this whole test exists for.
    for name in ("APP_ENV", "API_CORS_ORIGINS"):
        assert f"{name}: ${{{{ needs.target.outputs.{name.lower()} }}}}" in text, (
            f"deploy.yml never maps {name} into the deploy job's env:, so the picker's value "
            f"never reaches bootstrap-env.sh and is erased by the regenerated deployment env")


def test_the_generated_env_preserves_a_stated_app_env():
    """Regeneration must carry a stated value through, not overwrite it with the dev default.

    `APP_ENV=${APP_ENV:-dev}` is what makes the workflow's export survive stage 1. A bare
    `APP_ENV=dev`, or no line at all, breaks the chain above at its weakest link.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "APP_ENV=${APP_ENV:-dev}" in text, (
        "bootstrap-env.sh's generated-env heredoc does not emit APP_ENV with the ${VAR:-default} "
        "idiom, so a value the workflow exported is erased when the env is regenerated")
    assert 'API_CORS_ORIGINS="${API_CORS_ORIGINS:-*}"' in text, (
        "bootstrap-env.sh's generated-env heredoc does not emit API_CORS_ORIGINS with the "
        "${VAR:-default} idiom, so prod's narrowed origin list is erased on every run")


def test_the_hardened_secret_containers_are_named():
    """settings/policy.py refuses to import for a hardened environment missing either of these, so
    prod would not start - or worse, would start unhardened. Both are emitted with a non-empty
    default by the generator, which is the thing that has to keep being true."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for name, default in (("MESH_API_KEY_SECRET", "mesh-api-key"),
                          ("USER_TOKEN_SECRET_SECRET", "user-token-secret")):
        assert f"{name}=${{{name}:-{default}}}" in text, (
            f"{name} is not emitted with a non-empty default, so a hardened deployment can reach "
            f"create-api-service.sh naming no container for it")
