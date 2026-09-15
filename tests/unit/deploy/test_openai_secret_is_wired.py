# Responsibility: Verify the openai credential reaches every runtime that resolves a route.
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).parents[3]

CASES = [
    ("deploy/gcp/scripts/create-secrets.sh",      "OPENAI_API_KEY|"),
    ("deploy/gcp/scripts/bootstrap-env.sh",       "OPENAI_API_KEY_SECRET"),
    ("deploy/gcp/generated.prod.env",             "OPENAI_API_KEY_SECRET=openai-api-key"),
    ("deploy/gcp/scripts/create-api-service.sh",  "OPENAI_API_KEY:OPENAI_API_KEY_SECRET"),
    ("deploy/gcp/scripts/create-worker-fleet.sh", "openai-api-key-secret:OPENAI_API_KEY_SECRET"),
    ("deploy/gcp/worker/startup.sh",              "OPENAI_API_KEY:openai-api-key-secret"),
]


@pytest.mark.parametrize("path,needle", CASES)
def test_the_openai_credential_is_wired(path, needle):
    text = (REPO / path).read_text(encoding="utf-8")
    assert needle in text, f"{path} does not carry {needle!r}"


def _secret_bearing_roster() -> set[str]:
    """The SECRET_BEARING=( ... ) array in create-api-service.sh, parsed."""
    import re
    text = (REPO / "deploy/gcp/scripts/create-api-service.sh").read_text(encoding="utf-8")
    match = re.search(r"SECRET_BEARING=\((.*?)\)", text, re.DOTALL)
    assert match, "SECRET_BEARING array not found in create-api-service.sh"
    return set(match.group(1).split())


def test_the_plaintext_refusal_roster_names_the_openai_key():
    assert "OPENAI_API_KEY" in _secret_bearing_roster(), (
        "API_EXTRA_ENV would accept OPENAI_API_KEY=<key> as a visible environment value; "
        "check_deploy_secrets.py only matches names it is told are secret-bearing")


def test_the_roster_is_exactly_the_inventorys_secret_set():
    """create-api-service.sh:243 says the roster IS the secret=True set from inventory.py,
    'restated here' for a blind spot in the deploy gate. Nothing enforced that until now, so
    the two could drift silently - which is how a new credential gets a plaintext path."""
    from meshpipeline.settings import inventory
    declared = {v.name for v in inventory.all_vars() if v.secret}
    roster = _secret_bearing_roster()
    assert declared == roster, (
        "SECRET_BEARING and the inventory's secret=True set have drifted: "
        f"only in inventory={sorted(declared - roster)}, "
        f"only in the script={sorted(roster - declared)}")
