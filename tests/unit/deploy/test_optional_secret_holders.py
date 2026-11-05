# Responsibility: Keep a deployment's stated "this feature is not configured" from being defaulted away.
# Owns: the empty-vs-unset assertions over bootstrap-env.sh and the admin stage's absent-container gate.
# Boundaries: read-only inspection of the workflow and the scripts; it calls no cloud.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
ADMIN_SERVICE = REPO / "deploy" / "gcp" / "scripts" / "create-admin-service.sh"

#: Holders naming a secret container that exists only where the OPTIONAL feature does. Unlike every
#: other default in bootstrap-env.sh - sizes, names derived from DEPLOY_ID, containers present in
#: every project - an empty value here is a decision that must survive.
OPTIONAL_HOLDERS = ("GOOGLE_CLIENT_SECRET_SECRET", "OUTREACH_DB_PASSWORD_SECRET")


def _picker_block(text: str, deployment_id: str) -> str:
    marker = f'echo "deployment_id={deployment_id}"'
    start = text.index(marker)
    return text[start:text.index('} >> "$GITHUB_OUTPUT"', start)]


def test_an_optional_holder_default_does_not_fire_on_a_stated_empty():
    """THE BUG THIS FILE EXISTS FOR.

    dev runs no outreach and has no OAuth client, so its picker states every value in that block as
    the empty string. `${VAR:-default}` substitutes on unset OR empty, so it overwrote that decision
    with prod's container names; the admin stage bound them as secret references and Cloud Run
    refused the revision with `Permission denied on secret: .../google-client-secret/versions/latest`
    - which reads as a missing IAM grant and is not one. hexera-dev has no such container at all.

    `${VAR-default}` (no colon) honours a stated empty and still defaults when the variable is
    genuinely unset, which is the local `make bootstrap` case the default was written for.
    """
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for holder in OPTIONAL_HOLDERS:
        assert re.search(rf"^{holder}=\$\{{{holder}-", text, re.M), (
            f"{holder} does not use the ${{VAR-default}} form, so a deployment that states it empty "
            f"has that decision replaced by a container name belonging to another environment")
        assert not re.search(rf"^{holder}=\$\{{{holder}:-", text, re.M), (
            f"{holder} still uses ${{VAR:-default}}, which fires on an empty value as well as an "
            f"unset one")


def test_dev_states_the_optional_holders_empty_rather_than_omitting_them():
    """The other half of the contract. The fix only holds while dev actually STATES these as empty -
    an omitted output is unset, and unset is exactly when the default is meant to apply."""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = _picker_block(text, "dev")
    for output in ("google_client_secret_secret", "outreach_db_password_secret"):
        assert re.search(rf'echo "{output}="', block), (
            f"dev's picker no longer states {output} as an explicit empty, so bootstrap-env.sh's "
            f"default applies again and the admin stage binds a container dev does not have")


def test_the_admin_stage_refuses_an_absent_container_before_it_mutates():
    """Order is the contract. Cloud Run reports an absent container as a permission error, so the
    failure names the wrong cause AND arrives only after the identity, its project bindings and its
    secret grants already exist. The console stage has always had this gate; the admin stage is the
    one that demonstrated why."""
    text = ADMIN_SERVICE.read_text(encoding="utf-8")
    assert "secret_confirmed_absent" in text, (
        "create-admin-service.sh has no confirmed-absence gate, so a container that does not exist "
        "surfaces as a Cloud Run permission error after the stage has already mutated IAM")
    gate = text.index("secret_confirmed_absent")
    identity = text.index('if sa_exists "${ADMIN_SA_EMAIL}"')
    deploy = text.index("gc run deploy") if "gc run deploy" in text else text.index("run deploy")
    assert gate < identity < deploy, (
        "the absent-container gate does not run before the identity is created and the service is "
        "deployed, so it cannot prevent a partial mutation")


def test_the_admin_absence_gate_requires_a_confirmed_absence():
    """`secret_exists` would treat a failed read - this identity lacking secretmanager.viewer - as
    proof the container is missing, and refuse a deploy that should have proceeded. Only Secret
    Manager affirmatively saying NOT_FOUND may stop it."""
    text = ADMIN_SERVICE.read_text(encoding="utf-8")
    window = text[text.index("ABSENT_ADMIN_SECRETS=()"):]
    window = window[: window.index("if sa_exists")]
    assert "secret_confirmed_absent" in window, "the gate does not use the confirmed-absence helper"
    assert not re.search(r"\bsecret_exists\b", window), (
        "the gate uses secret_exists, which cannot tell an absent container from an unreadable one")
