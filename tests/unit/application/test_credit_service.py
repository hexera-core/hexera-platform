# Responsibility: Verify credits are issued exactly as configured, and that spending is exactly
#                 debit and refund - both signed the one way their callers cannot get wrong.
from __future__ import annotations

import inspect
import uuid

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.persistence.models import CreditEntryType


class _LedgerDouble:

    def __init__(self):
        self.entries: list[dict] = []

    async def append(self, db, *, organization_id, entry_type, amount, reason="", overage=0):
        entry = {"organization_id": organization_id, "entry_type": entry_type,
                 "amount": amount, "reason": reason}
        if overage:
            entry["overage"] = overage
        self.entries.append(entry)
        return None

    async def balance(self, db, *, organization_id):
        return sum(e["amount"] for e in self.entries
                   if e["organization_id"] == organization_id)


@pytest.fixture
def ledger(monkeypatch):
    double = _LedgerDouble()
    monkeypatch.setattr(credit_service, "credit_ledger_repo", double)
    return double


@pytest.mark.asyncio
async def test_a_grant_is_a_positive_entry(ledger):
    org = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="signup")
    assert ledger.entries == [{"organization_id": org, "entry_type": CreditEntryType.grant,
                               "amount": 100, "reason": "signup"}]


@pytest.mark.asyncio
async def test_a_grant_of_zero_writes_nothing(ledger):
    # The ledger's CHECK refuses a zero amount, so the service must not offer it one.
    await credit_service.grant(None, organization_id=uuid.uuid4(), amount=0, reason="signup")
    assert ledger.entries == []


@pytest.mark.asyncio
async def test_a_negative_grant_is_refused(ledger):
    # Spending is out of scope for this cycle. A grant that could be negative is a debit with
    # the wrong name on it, and would be the one way this cycle could remove credits.
    with pytest.raises(ValueError):
        await credit_service.grant(None, organization_id=uuid.uuid4(), amount=-1, reason="x")
    assert ledger.entries == []


@pytest.mark.asyncio
async def test_the_balance_is_read_through_the_ledger(ledger):
    org = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="signup")
    assert await credit_service.balance(None, organization_id=org) == 100


@pytest.mark.asyncio
async def test_the_signup_grant_honours_the_configured_amount(ledger, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 250)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(None, organization_id=org) == 250
    assert ledger.entries[0]["amount"] == 250


@pytest.mark.asyncio
async def test_a_configured_zero_disables_the_signup_grant(ledger, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 0)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(None, organization_id=org) == 0
    assert ledger.entries == []


def test_the_spending_surface_is_exactly_debit_and_refund():
    # An ALLOWLIST, not a denylist of suspicious names, and it still pins the WHOLE surface: the
    # ledger's CHECK is `amount <> 0` rather than `amount > 0`, so the database accepts a negative
    # amount and each function's own refusal is the only guard. A denylist would let `deduct` or
    # `withdraw` past; pinning the set means any new public function is justified here first.
    #
    # `debit` and `refund` were admitted when Stripe billing landed. The ledger was shaped for them
    # from the start - CreditEntryType declared both while nothing wrote either - so spending
    # arrived as a new caller and not as a migration, which is what this test was guarding.
    names = {n for n, _ in inspect.getmembers(credit_service, inspect.isfunction)
             if not n.startswith("_") and getattr(credit_service, n).__module__ ==
             credit_service.__name__}
    assert names == {"grant", "debit", "refund", "balance",
                     "grant_signup_credits", "history"}, names


@pytest.mark.asyncio
async def test_a_debit_is_stored_negated(ledger):
    # THE CALLER PASSES A POSITIVE AMOUNT and the service negates it. No call site has to remember
    # the sign convention, because a caller that got it backwards would write a debit that RAISES
    # the balance and the ledger's only CHECK - `amount <> 0` - would not catch it.
    org = uuid.uuid4()
    await credit_service.debit(None, organization_id=org, amount=40, reason="mesh job")
    assert ledger.entries == [{"organization_id": org, "entry_type": CreditEntryType.debit,
                               "amount": -40, "reason": "mesh job"}]


@pytest.mark.asyncio
async def test_a_debits_overage_is_recorded_and_bounded_by_the_debit(ledger):
    # The overage is what the metered price bills. More than the debit, or negative, would bill
    # consumption that never happened.
    org = uuid.uuid4()
    await credit_service.debit(None, organization_id=org, amount=40, overage=15)
    assert ledger.entries[-1]["overage"] == 15
    for bad in (-1, 41):
        with pytest.raises(ValueError):
            await credit_service.debit(None, organization_id=org, amount=40, overage=bad)


@pytest.mark.asyncio
async def test_a_debit_refuses_a_negative_amount(ledger):
    # The mirror of grant()'s refusal: a negative debit is a grant wearing the wrong name.
    with pytest.raises(ValueError):
        await credit_service.debit(None, organization_id=uuid.uuid4(), amount=-1)
    assert ledger.entries == []


@pytest.mark.asyncio
async def test_a_refund_is_positive_and_keeps_its_own_type(ledger):
    # A refund is NOT a grant, even though both raise the balance. The entry type is what makes
    # "why is my balance this?" answerable - a refund names a debit that should not have stood,
    # and recording it as a grant would lose that.
    org = uuid.uuid4()
    await credit_service.refund(None, organization_id=org, amount=40, reason="job failed")
    assert ledger.entries == [{"organization_id": org, "entry_type": CreditEntryType.refund,
                               "amount": 40, "reason": "job failed"}]


def test_the_settings_are_declared_in_the_inventory():
    from meshpipeline.settings import inventory
    declared = {v.name for g in inventory.INVENTORY for v in g.vars}
    assert "SIGNUP_GRANT_CREDITS" in declared
    assert "CONSOLE_SIGNUP_ENABLED" in declared
