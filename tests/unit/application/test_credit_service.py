# Responsibility: Verify credits are issued exactly as configured, and that nothing here spends them.
from __future__ import annotations

import inspect
import uuid

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.persistence.models import CreditEntryType

pytestmark = pytest.mark.asyncio


class _LedgerDouble:

    def __init__(self):
        self.entries: list[dict] = []

    async def append(self, db, *, organization_id, entry_type, amount, reason=""):
        self.entries.append({"organization_id": organization_id, "entry_type": entry_type,
                             "amount": amount, "reason": reason})
        return None

    async def balance(self, db, *, organization_id):
        return sum(e["amount"] for e in self.entries
                   if e["organization_id"] == organization_id)


@pytest.fixture
def ledger(monkeypatch):
    double = _LedgerDouble()
    monkeypatch.setattr(credit_service, "credit_ledger_repo", double)
    return double


async def test_a_grant_is_a_positive_entry(ledger):
    org = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="signup")
    assert ledger.entries == [{"organization_id": org, "entry_type": CreditEntryType.grant,
                               "amount": 100, "reason": "signup"}]


async def test_a_grant_of_zero_writes_nothing(ledger):
    # The ledger's CHECK refuses a zero amount, so the service must not offer it one.
    await credit_service.grant(None, organization_id=uuid.uuid4(), amount=0, reason="signup")
    assert ledger.entries == []


async def test_a_negative_grant_is_refused(ledger):
    # Spending is out of scope for this cycle. A grant that could be negative is a debit with
    # the wrong name on it, and would be the one way this cycle could remove credits.
    with pytest.raises(ValueError):
        await credit_service.grant(None, organization_id=uuid.uuid4(), amount=-1, reason="x")
    assert ledger.entries == []


async def test_the_balance_is_read_through_the_ledger(ledger):
    org = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="signup")
    assert await credit_service.balance(None, organization_id=org) == 100


async def test_the_signup_grant_honours_the_configured_amount(ledger, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 250)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(None, organization_id=org) == 250
    assert ledger.entries[0]["amount"] == 250


async def test_a_configured_zero_disables_the_signup_grant(ledger, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 0)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(None, organization_id=org) == 0
    assert ledger.entries == []


def test_the_service_offers_no_way_to_spend():
    # The guard on the design's central promise: this cycle issues credits and nothing else.
    names = {n for n, _ in inspect.getmembers(credit_service, inspect.isfunction)
             if not n.startswith("_")}
    assert not (names & {"debit", "spend", "charge", "hold", "settle", "refund"}), names


def test_the_settings_are_declared_in_the_inventory():
    from meshpipeline.settings import inventory
    declared = {v.name for g in inventory.INVENTORY for v in g.vars}
    assert "SIGNUP_GRANT_CREDITS" in declared
    assert "CONSOLE_SIGNUP_ENABLED" in declared
