# Responsibility: Verify the deployment env declares the console tier, and declares no secret value.
# Boundaries: it reads the generator's template; it discovers nothing and calls no cloud.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"

DECLARED = (
    "CLOUDRUN_CONSOLE_SERVICE", "CONSOLE_SERVICE_ACCOUNT",
    "CONSOLE_CPU", "CONSOLE_MEMORY", "CONSOLE_CONCURRENCY",
    "CONSOLE_MIN_INSTANCES", "CONSOLE_MAX_INSTANCES",
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
    assert "CLOUDRUN_CONSOLE_SERVICE=${CLOUDRUN_CONSOLE_SERVICE:-" in text, (
        "the console service name must be overridable and may be empty - a deployment that "
        "serves no console skips the stage, exactly as an API-less one does")
