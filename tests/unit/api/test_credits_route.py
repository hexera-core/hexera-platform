# Responsibility: Verify the balance is read for the caller's own organisation and for nobody else's.
from __future__ import annotations

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.api.v1 import client_config, credits

pytestmark = pytest.mark.asyncio


@pytest.fixture
def balances(monkeypatch):
    store = {"org-1": 100}

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
    assert await credits.read_balance(organization_id="org-1") == {"balance": 100,
                                                                   "unit": "credits"}


async def test_another_organisations_balance_is_not_reachable(balances):
    assert await credits.read_balance(organization_id="org-2") == {"balance": 0,
                                                                   "unit": "credits"}


async def test_a_caller_with_no_organisation_reads_zero_rather_than_failing(balances):
    # Mid-migration, or a lookup that could not run. A missing balance is 0, not a 500.
    assert await credits.read_balance(organization_id="") == {"balance": 0, "unit": "credits"}


async def test_the_client_config_tells_the_console_whether_signup_is_open(monkeypatch):
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)
    payload = await client_config.client_config()
    assert payload["auth"] == {"signup_enabled": False}
