# Responsibility: Keep billing opt-in end to end - GitHub environment variable, deployment env, API service.
# Boundaries: read-only inspection of the workflow and the scripts; it calls no cloud.
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).parents[3]
WORKFLOW = REPO / ".github" / "workflows" / "deploy.yml"
BOOTSTRAP = REPO / "deploy" / "gcp" / "scripts" / "bootstrap-env.sh"
API = REPO / "deploy" / "gcp" / "scripts" / "create-api-service.sh"

BILLING = ("STRIPE_API_KEY_SECRET", "STRIPE_WEBHOOK_SECRET_SECRET", "ADMIN_API_KEY_SECRET",
           "STRIPE_PRICE_STARTER", "STRIPE_PRICE_STARTER_OVERAGE", "STRIPE_PRICE_TEAM",
           "STRIPE_PRICE_TEAM_OVERAGE", "CONSOLE_BASE_URL")


def test_each_billing_setting_reaches_the_deploy_from_its_environment_variable():
    text = WORKFLOW.read_text(encoding="utf-8")
    for name in BILLING:
        assert f"{name}: ${{{{ vars.{name} }}}}" in text, name


def test_each_billing_setting_survives_regeneration_and_defaults_to_not_charging():
    # Stage 1 regenerates the deployment env from bootstrap-env.sh's heredoc; a setting missing
    # there is erased before any later stage reads it. The default must be EMPTY - a container name
    # defaulted in would bind a reference to a secret that may hold no version, and a revision that
    # cannot resolve one never becomes ready.
    text = BOOTSTRAP.read_text(encoding="utf-8")
    for name in BILLING:
        assert re.search(rf"^{name}=\$\{{{name}:-\}}$", text, re.M), name


def test_the_api_is_never_left_returning_a_paying_customer_to_localhost():
    text = API.read_text(encoding="utf-8")
    block = text[text.index("BILLING'S NON-SECRET HALF"):]
    assert 'https://${CONSOLE_DOMAIN}' in block
    assert "die " in block[:block.index("API_ENV_PAIRS+=(\"CONSOLE_BASE_URL")]


def test_the_admin_credential_is_bound_to_the_api_where_it_is_stated():
    # The admin console's Billing page reads /admin/billing, which answers 404 while the API holds
    # no ADMIN_API_KEY. Before this holder was carried, it held none in any environment.
    assert '"ADMIN_API_KEY:ADMIN_API_KEY_SECRET"' in API.read_text(encoding="utf-8")
