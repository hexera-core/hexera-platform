# Responsibility: Verify the deployment env declares the console tier, and declares no secret value.
# Boundaries: it reads the generator's template; it discovers nothing and calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"

DECLARED = (
    "CLOUDRUN_CONSOLE_SERVICE", "CONSOLE_SERVICE_ACCOUNT",
    "CONSOLE_CPU", "CONSOLE_MEMORY", "CONSOLE_CONCURRENCY",
    "CONSOLE_MIN_INSTANCES", "CONSOLE_MAX_INSTANCES", "CONSOLE_TIMEOUT_SECONDS",
    "CONSOLE_INGRESS", "CONSOLE_ALLOW_UNAUTHENTICATED",
    "AUTH_SECRET_SECRET", "CONSOLE_AUTH_USERS_SECRET",
    "HEXERA_API_BASE_URL", "NEXT_PUBLIC_HEXERA_API_BASE_URL",
)


def test_every_console_setting_is_declared():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    missing = [name for name in DECLARED if f"{name}=" not in text]
    assert not missing, f"the deployment env declares no {missing}"


def test_the_console_names_secret_containers_never_values():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    # The <SETTING>_SECRET convention: the deployment names a container. A bare AUTH_SECRET=
    # assignment in the generated env would be the literal credential this file exists to avoid.
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("AUTH_SECRET="), (
            f"the generated env assigns AUTH_SECRET directly ({stripped!r}); it must name a "
            f"Secret Manager container via AUTH_SECRET_SECRET instead")
        assert not stripped.startswith("CONSOLE_AUTH_USERS="), (
            f"the generated env assigns CONSOLE_AUTH_USERS directly ({stripped!r}); the "
            f"password hashes are a credential and belong in Secret Manager")


def test_an_absent_console_service_is_a_supported_arrangement():
    text = BOOTSTRAP.read_text(encoding="utf-8")
    # discover() derives the console service name from CLOUDRUN_CONSOLE_SERVICE with an empty
    # default - i.e. it is overridable and may legitimately be empty - the same shape MESH_JOB
    # uses for CLOUDRUN_MESH_JOB.
    assert 'CONSOLE_SERVICE="${CLOUDRUN_CONSOLE_SERVICE:-}"' in text, (
        "discover() must derive CONSOLE_SERVICE from CLOUDRUN_CONSOLE_SERVICE with an empty "
        "default; the console service name must be overridable and may be empty - a deployment "
        "that serves no console skips the stage, exactly as an API-less one does")
    # emit_env's heredoc must read the discovered variable back rather than re-deriving it from
    # the raw environment - again mirroring how CLOUDRUN_MESH_JOB=${MESH_JOB} is written.
    assert "CLOUDRUN_CONSOLE_SERVICE=${CONSOLE_SERVICE}" in text, (
        "the generated env must read the console service name discover() already resolved "
        "(CLOUDRUN_CONSOLE_SERVICE=${CONSOLE_SERVICE}), not re-default from the raw "
        "CLOUDRUN_CONSOLE_SERVICE environment variable a second time")


def test_a_regeneration_preserves_a_previously_promoted_console_digest():
    # MESH_IMAGE=${MESH_IMAGE:-} and APP_IMAGE=${APP_IMAGE:-} let a promoted digest survive a
    # rerun of bootstrap-env.sh; CONSOLE_IMAGE must carry the identical shape, or every
    # regeneration silently drops the console's promoted digest (I5).
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "CONSOLE_IMAGE=${CONSOLE_IMAGE:-}" in text, (
        "bootstrap-env.sh does not emit CONSOLE_IMAGE=${CONSOLE_IMAGE:-} beside APP_IMAGE, so a "
        "regeneration erases a previously promoted console digest")
