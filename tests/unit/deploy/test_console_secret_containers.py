# Responsibility: Verify the console's credential gets a container and a reader, and no value.
# Boundaries: it reads the script's declarations; it creates nothing.
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).parents[3]
SECRETS = REPO / "deploy" / "gcp" / "scripts" / "create-secrets.sh"


def test_the_console_credentials_have_containers():
    text = SECRETS.read_text(encoding="utf-8")
    assert "AUTH_SECRET|" in text, "no container is declared for AUTH_SECRET"


def test_the_console_identity_reads_them():
    text = SECRETS.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith('"AUTH_SECRET|'):
            assert "CONSOLE_SERVICE_ACCOUNT" in line, (
                f"the console identity is not a declared reader of this container: {line.strip()!r}")


def test_no_console_credential_value_appears():
    text = SECRETS.read_text(encoding="utf-8")
    assert "scrypt:" not in text, "a password hash literal leaked into the secrets script"
