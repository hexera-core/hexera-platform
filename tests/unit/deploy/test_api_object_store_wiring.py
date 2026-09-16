# Responsibility: Verify every deploy target hands the API service a complete object store.
# Boundaries: read-only validation of the workflow and the scripts it drives; it calls no cloud.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
API_SERVICE = REPO / "deploy" / "gcp" / "scripts" / "create-api-service.sh"

# What the API needs before it can store a single uploaded byte. The secret half is deliberately
# absent: MINIO_SECRET_KEY reaches the service as a Secret Manager reference, and what the picker
# states is the CONTAINER NAME below, never a value.
REQUIRED_OUTPUTS = (
    "minio_endpoint",
    "minio_region",
    "minio_secure",
    "minio_bucket",
    "minio_access_key",
    "minio_secret_key_secret",
)

# Every deployment this workflow can target. Both are checked, because the failure is per-target:
# prod pinned its store and dev did not, and dev is where it shipped.
DEPLOYMENTS = ("dev", "prod")


def _picker_block(text: str, deployment_id: str) -> str:
    """The one `{ ... } >> "$GITHUB_OUTPUT"` group that describes this deployment."""
    marker = f'echo "deployment_id={deployment_id}"'
    start = text.index(marker)
    return text[start:text.index('} >> "$GITHUB_OUTPUT"', start)]


def _emitted(block: str, name: str) -> str | None:
    found = re.search(rf'echo "{name}=([^"]*)"', block)
    return found.group(1) if found else None


def test_every_target_states_a_complete_object_store():
    """A HALF-STATED STORE IS THE BUG THIS FILE EXISTS FOR.

    The `storage` stage is the only other thing that writes MINIO_* into the deployment env, and a
    merge to main selects `images,migrate,console,admin` - so it never runs on dev. The API stage
    runs under `images` and emits its object-store block only when MINIO_ENDPOINT is set, so dev
    deployed an API with no store at all: the adapter fell back to its local-stack defaults, dialled
    localhost:9000, and every geometry upload came back 503 "Storage is unavailable - the upload was
    not saved." Pinning these in the picker is what makes the API stage independent of whether the
    same run also reconciled the store.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    for deployment_id in DEPLOYMENTS:
        block = _picker_block(text, deployment_id)
        for name in REQUIRED_OUTPUTS:
            value = _emitted(block, name)
            assert value, (
                f"deploy.yml's {deployment_id} picker emits no non-empty {name}. Without the full "
                f"set create-api-service.sh writes no object store onto the service and the first "
                f"upload fails with 'Storage is unavailable'")


def test_the_object_store_endpoint_is_addressed_over_tls():
    """MINIO_SECURE defaults to false for the local compose stack, and Google's S3-interoperability
    endpoint serves TLS only - so a target naming that endpoint without stating TLS produces a
    connection reset that never mentions TLS. validate-config.sh refuses this pairing too; asserting
    it here names the target rather than the run that would have been refused."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for deployment_id in DEPLOYMENTS:
        block = _picker_block(text, deployment_id)
        if _emitted(block, "minio_endpoint") == "storage.googleapis.com":
            assert _emitted(block, "minio_secure") == "true", (
                f"deploy.yml's {deployment_id} picker points at Google Cloud Storage but does not "
                f"state minio_secure=true, and that endpoint refuses a plain-HTTP request")


def test_the_picker_values_reach_the_deploy_job():
    """An output the picker emits but the job never maps into its env: is erased on stage 1.

    bootstrap-env.sh REGENERATES the deployment env from a heredoc that reads the ambient
    environment, so the `env:` block below is the only route by which a picked value survives to the
    stage that consumes it. This is the same gap test_prod_api_hardening.py guards for APP_ENV.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in REQUIRED_OUTPUTS:
        runtime_var = name.upper()
        assert f"{runtime_var}: ${{{{ needs.target.outputs.{name} }}}}" in text, (
            f"deploy.yml never maps {runtime_var} into the deploy job's env:, so the picker's value "
            f"never reaches bootstrap-env.sh and is erased by the regenerated deployment env")
    # The address a BROWSER resolves a signed URL on. A distinct fact from the one the API process
    # dials, which is why it has its own setting - and the same value here, because both reach
    # Google's interoperability endpoint directly.
    assert "MINIO_PUBLIC_ENDPOINT: ${{ needs.target.outputs.minio_endpoint }}" in text, (
        "deploy.yml does not map MINIO_PUBLIC_ENDPOINT, so create-api-service.sh signs download "
        "URLs for whatever address it falls back to rather than the one a browser can resolve")


def test_the_generated_env_preserves_a_stated_object_store():
    """The heredoc must carry an exported value through rather than blank it."""
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for name in (*(n.upper() for n in REQUIRED_OUTPUTS), "MINIO_PUBLIC_ENDPOINT"):
        assert f"{name}=${{{name}:-" in text, (
            f"bootstrap-env.sh's generated-env heredoc does not emit {name} with the ${{VAR:-}} "
            f"idiom, so a value the workflow exported is erased when the env is regenerated")


def test_the_api_stage_still_gates_on_the_endpoint():
    """The anchor for everything above. If this gate is ever widened or removed the pinning stays
    harmless, but while it stands MINIO_ENDPOINT is load-bearing: an access id and a bucket name
    address nothing on their own, and without the endpoint none of them are written at all."""
    text = API_SERVICE.read_text(encoding="utf-8")
    assert 'if [ -n "${MINIO_ENDPOINT:-}" ]; then' in text, (
        "create-api-service.sh no longer gates its object-store block on MINIO_ENDPOINT - if the "
        "condition moved, the picker outputs this file pins have to move with it")
