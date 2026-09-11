# tests/unit/api/test_chat_list_route.py
# Responsibility: Verify the conversation list is the caller's own and paged like every other.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.api import pagination
from meshpipeline.api.v1 import chat

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
BASE = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@dataclass
class _Session:
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    domain: str | None = None
    job_id: uuid.UUID | None = None
    messages: list = None


@pytest.fixture
def listing(monkeypatch):
    rows = [
        _Session(id=uuid.uuid4(), created_at=BASE - timedelta(hours=n),
                 updated_at=BASE - timedelta(hours=n), domain=f"study {n}",
                 messages=[{"role": "user", "content": "hi"}])
        for n in range(3)
    ]
    store = {OWNER: rows}
    seen: dict = {}

    async def fake_list(db, owner_id, *, organization_id="", limit=25, before=None):
        seen["limit"] = limit
        seen["before"] = before
        return store.get(owner_id, [])[:limit]

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    # THE SERVICE IS THE SEAM. `chat.py` holds no repository; the collection route reaches for
    # `JobService.list_conversations`, so that is what the double replaces.
    monkeypatch.setattr(chat.svc, "list_conversations", fake_list)
    monkeypatch.setattr(chat, "get_db", lambda: _NullSession())
    return rows, seen


async def test_the_list_is_the_callers_own_conversations(listing):
    rows, _ = listing
    payload = await chat.list_sessions(owner_id=OWNER, organization_id="")
    assert [item["id"] for item in payload["items"]] == [str(row.id) for row in rows]


async def test_another_owners_conversations_are_not_reachable(listing):
    payload = await chat.list_sessions(owner_id="nobody@example.com", organization_id="")
    assert payload["items"] == []


async def test_the_row_carries_a_message_count_not_the_messages(listing):
    # The list must not ship every transcript to render a table. A count is what the page needs;
    # the transcript is what /chat/history/{id} is for.
    payload = await chat.list_sessions(owner_id=OWNER, organization_id="")
    assert payload["items"][0]["message_count"] == 1
    assert "messages" not in payload["items"][0]


async def test_the_limit_is_clamped_before_it_reaches_the_repository(listing):
    _, seen = listing
    await chat.list_sessions(owner_id=OWNER, organization_id="", limit=100_000)
    # Clamped, then the look-ahead row on top - see listing.look_ahead.
    assert seen["limit"] == pagination.MAX_LIMIT + 1


async def test_a_malformed_cursor_reads_as_the_first_page(listing):
    _, seen = listing
    await chat.list_sessions(owner_id=OWNER, organization_id="", cursor="nonsense")
    assert seen["before"] is None
