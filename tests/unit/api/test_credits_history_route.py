# tests/unit/api/test_credits_history_route.py
# Responsibility: Verify the ledger is the caller's own organisation's, and degrades like the balance.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.api.v1 import credits

pytestmark = pytest.mark.asyncio

OWN_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")
BASE = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@dataclass
class _Entry:
    id: uuid.UUID
    entry_type: str
    amount: int
    reason: str
    created_at: datetime


@pytest.fixture
def ledger(monkeypatch):
    store = {
        OWN_ORG: [_Entry(id=uuid.uuid4(), entry_type="grant", amount=500,
                         reason="signup", created_at=BASE - timedelta(hours=n))
                  for n in range(2)],
    }

    async def fake_history(db, *, organization_id, limit=25, before=None):
        return store.get(organization_id, [])[:limit]

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(credits.credit_service, "history", fake_history)
    monkeypatch.setattr(credits, "get_db", lambda: _NullSession())
    return store


async def test_the_ledger_is_the_callers_own(ledger):
    payload = await credits.read_history(organization_id=str(OWN_ORG))
    assert [entry["amount"] for entry in payload["items"]] == [500, 500]
    assert payload["items"][0]["entry_type"] == "grant"


async def test_another_organisations_ledger_is_not_reachable(ledger):
    payload = await credits.read_history(organization_id=str(OTHER_ORG))
    assert payload["items"] == []


async def test_a_caller_with_no_organisation_reads_an_empty_page_rather_than_failing(ledger):
    # The same degradation read_balance already ships: mid-migration is not a reason to 500.
    assert await credits.read_history(organization_id="") == {"items": [], "next_cursor": None}


async def test_a_malformed_organisation_reads_an_empty_page_rather_than_raising(ledger):
    assert await credits.read_history(organization_id="not-a-uuid") == {"items": [],
                                                                        "next_cursor": None}
