# Responsibility: Verify the balance is read for the caller's own organisation and for nobody else's.
from __future__ import annotations

import uuid

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import client_config, credits

pytestmark = pytest.mark.asyncio


#: REAL uuid strings, because that is what a Principal carries: organization_id is
#: str(<uuid column>) or "". A fake id like "org-1" would exercise the route's
#: malformed-value path rather than its ordinary one, and would have hidden that the route
#: hands credit_service a parsed uuid rather than the string it received.
OWN_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture
def balances(monkeypatch):
    store = {OWN_ORG: 100}

    async def fake_balance(db, *, organization_id):
        return store.get(organization_id, 0)

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(credits.credit_service, "balance", fake_balance)
    monkeypatch.setattr(credits, "get_db", lambda: _NullSession())
    return store


async def test_the_balance_is_the_callers_own(balances):
    assert await credits.read_balance(organization_id=str(OWN_ORG)) == {"balance": 100,
                                                                   "unit": "credits"}


async def test_another_organisations_balance_is_not_reachable(balances):
    assert await credits.read_balance(organization_id=str(OTHER_ORG)) == {"balance": 0,
                                                                   "unit": "credits"}


async def test_a_caller_with_no_organisation_reads_zero_rather_than_failing(balances):
    # Mid-migration, or a lookup that could not run. A missing balance is 0, not a 500.
    assert await credits.read_balance(organization_id="") == {"balance": 0, "unit": "credits"}


async def test_a_malformed_organisation_reads_zero_rather_than_raising(balances):
    # The same narrowing tenant_scope applies to a value that is not a uuid: a Principal should
    # never carry one, but this route must not answer a proven caller with a 500 if it does.
    assert await credits.read_balance(organization_id="not-a-uuid") == {"balance": 0,
                                                                        "unit": "credits"}


async def test_the_client_config_tells_the_console_whether_signup_is_open(monkeypatch):
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)
    payload = await client_config.client_config()
    assert payload["auth"] == {"signup_enabled": False}
