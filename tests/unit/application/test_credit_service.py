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

    async def get_by_id(self, db, entry_id):
        for entry in self.entries:
            if entry.get("id") == entry_id:
                return type("LedgerEntry", (), entry)()
        return None

    async def append(self, db, *, organization_id, entry_type, amount, reason="", overage=0,
                     entry_id=None):
        entry = {"organization_id": organization_id, "entry_type": entry_type,
                 "amount": amount, "reason": reason}
        if entry_id is not None:
            entry["id"] = entry_id
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
async def test_a_grant_retry_with_the_same_entry_id_writes_once(ledger):
    org = uuid.uuid4()
    entry_id = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="support",
                               entry_id=entry_id)
    await credit_service.grant(None, organization_id=org, amount=100, reason="support",
                               entry_id=entry_id)
    assert ledger.entries == [{"id": entry_id, "organization_id": org,
                               "entry_type": CreditEntryType.grant, "amount": 100,
                               "reason": "support"}]


@pytest.mark.asyncio
async def test_a_grant_entry_id_cannot_be_reused_for_a_different_grant(ledger):
    org = uuid.uuid4()
    entry_id = uuid.uuid4()
    await credit_service.grant(None, organization_id=org, amount=100, reason="support",
                               entry_id=entry_id)
    with pytest.raises(ValueError):
        await credit_service.grant(None, organization_id=org, amount=200, reason="support",
                                   entry_id=entry_id)


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


@pytest.fixture
def claims(monkeypatch):
    """The once-per-mailbox claim table, as a set of normalised mailboxes."""
    claimed: set[str] = set()

    async def claim(db, email):
        digest = credit_service.signup_claims.claim_digest(email)
        if digest in claimed:
            return False
        claimed.add(digest)
        return True

    monkeypatch.setattr(credit_service.signup_claims, "claim", claim)
    return claimed


@pytest.mark.asyncio
async def test_the_signup_grant_honours_the_configured_amount(ledger, claims, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 250)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(
        None, organization_id=org, email="a@example.com") == 250
    assert ledger.entries[0]["amount"] == 250


@pytest.mark.asyncio
async def test_a_configured_zero_disables_the_signup_grant(ledger, claims, monkeypatch):
    monkeypatch.setattr(polcfg, "SIGNUP_GRANT_CREDITS", 0)
    org = uuid.uuid4()
    assert await credit_service.grant_signup_credits(
        None, organization_id=org, email="a@example.com") == 0
    assert ledger.entries == []
    assert claims == set(), "a disabled grant still spent the mailbox's one claim"


@pytest.mark.asyncio
async def test_the_grant_reports_its_outcome_to_the_caller_not_the_trail(ledger, claims):
    # The audit line is the CALLER's to write after its transaction commits; this function only
    # describes what happened. A rolled-back grant must not stand in the trail.
    org = uuid.uuid4()
    events: list = []
    await credit_service.grant_signup_credits(None, organization_id=org,
                                              email="a@example.com", events=events)
    await credit_service.grant_signup_credits(None, organization_id=uuid.uuid4(),
                                              email="a+again@example.com", events=events)
    assert [(a, k["outcome"], k["detail"]) for a, k in events] == [
        ("credits.signup_grant", "success", {"amount": polcfg.SIGNUP_GRANT_CREDITS}),
        ("credits.signup_grant", "withheld", {"cause": "already_claimed"}),
    ]


@pytest.mark.asyncio
async def test_one_mailbox_gets_one_grant_across_organisations_and_aliases(ledger, claims):
    # The farming shapes: a second account for the same inbox, a +tag alias, a dotted Gmail
    # spelling, and re-signing up after erasing the first account. Each still gets an account;
    # none gets the credits a second time.
    first = await credit_service.grant_signup_credits(
        None, organization_id=uuid.uuid4(), email="jane.doe@gmail.com")
    again = [await credit_service.grant_signup_credits(None, organization_id=uuid.uuid4(),
                                                       email=address)
             for address in ("janedoe+1@gmail.com", "Jane.Doe@googlemail.com",
                             "jane.doe@gmail.com")]
    assert first == polcfg.SIGNUP_GRANT_CREDITS
    assert again == [0, 0, 0]
    assert len(ledger.entries) == 1


@pytest.mark.asyncio
async def test_a_disposable_inbox_gets_an_account_but_no_grant(ledger, claims):
    assert await credit_service.grant_signup_credits(
        None, organization_id=uuid.uuid4(), email="bot@mailinator.com") == 0
    assert await credit_service.grant_signup_credits(
        None, organization_id=uuid.uuid4(), email="bot@eu.mailinator.com") == 0
    assert ledger.entries == []


@pytest.mark.asyncio
async def test_the_signup_grant_has_a_deterministic_ledger_id(ledger, claims):
    # A replay of the SAME organisation's grant is a no-op in the ledger itself, independent of
    # the claim row: the entry id is derived from the organisation.
    org = uuid.uuid4()
    await credit_service.grant_signup_credits(None, organization_id=org, email="a@example.com")
    assert ledger.entries[0]["id"] == credit_service._signup_grant_entry_id(org)
    assert credit_service._signup_grant_entry_id(org) == credit_service._signup_grant_entry_id(org)


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
