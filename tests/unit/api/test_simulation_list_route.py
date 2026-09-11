# tests/unit/api/test_simulation_list_route.py
# Responsibility: Verify the run list is the caller's own, newest first, and paged.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from meshpipeline.api import pagination
from meshpipeline.api.v1 import simulation

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
OTHER = "stranger@example.com"
BASE = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


@dataclass
class _Job:
    id: uuid.UUID
    status: str
    created_at: datetime
    ended_at: datetime | None = None
    current_attempt: int = 1
    failed_reason: str | None = None
    dispute_operation_key: str | None = None


def _rows(owner: str, count: int):
    return [
        (_Job(id=uuid.uuid4(), status="succeeded", created_at=BASE - timedelta(hours=n)),
         f"{owner} run {n}")
        for n in range(count)
    ]


@pytest.fixture
def listing(monkeypatch):
    # SIX rows, so a request for a 3-row page has a real 4th row behind it and the look-ahead
    # has something to find. A fixture the same size as the page cannot tell "this page is full"
    # apart from "there is another page", which is the bug the look-ahead exists to fix.
    store = {OWNER: _rows(OWNER, 6), OTHER: _rows(OTHER, 6)}
    seen: dict = {}

    async def fake_list(db, owner_id, *, organization_id="", limit=25, before=None):
        seen["limit"] = limit
        seen["before"] = before
        seen["organization_id"] = organization_id
        return store.get(owner_id, [])[:limit]

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    # THE SERVICE IS THE SEAM. The route is transport over `JobService`, never over a
    # repository, so the double goes where the route actually reaches - `svc.list_runs`.
    monkeypatch.setattr(simulation.svc, "list_runs", fake_list)
    monkeypatch.setattr(simulation, "get_db", lambda: _NullSession())
    return store, seen


async def test_the_list_is_the_callers_own_runs(listing):
    store, _ = listing
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="")
    assert [item["id"] for item in payload["items"]] == [str(job.id) for job, _ in store[OWNER]]


async def test_a_dispute_child_is_marked_as_a_rerun(listing):
    # A dispute creates a fresh job and links no conversation to it, so it has no label. Saying
    # it is a re-review is what stops every one of them reading as "Untitled study".
    store, _ = listing
    store[OWNER][0][0].dispute_operation_key = "op-abc"
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="")
    assert payload["items"][0]["is_rerun"] is True
    assert payload["items"][1]["is_rerun"] is False


async def test_another_owners_runs_are_not_reachable(listing):
    payload = await simulation.list_jobs(owner_id="nobody@example.com", organization_id="")
    assert payload["items"] == []
    assert payload["next_cursor"] is None


async def test_the_task_label_rides_along_from_the_joined_session(listing):
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="")
    assert payload["items"][0]["task_label"] == f"{OWNER} run 0"


async def test_a_full_page_offers_a_cursor_to_the_next_one(listing):
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="", limit=3)
    assert payload["next_cursor"] is not None
    assert pagination.decode_cursor(payload["next_cursor"]) is not None


async def test_a_short_page_offers_no_cursor(listing):
    # Six rows exist and ten were asked for, so there is provably nothing behind this page.
    payload = await simulation.list_jobs(owner_id=OWNER, organization_id="", limit=10)
    assert payload["next_cursor"] is None


async def test_the_limit_is_clamped_before_it_reaches_the_repository(listing):
    _, seen = listing
    await simulation.list_jobs(owner_id=OWNER, organization_id="", limit=100_000)
    # Clamped, then the look-ahead row on top: the repository is asked for one more than the
    # largest page a caller may have.
    assert seen["limit"] == pagination.MAX_LIMIT + 1


async def test_a_malformed_cursor_reads_as_the_first_page_rather_than_failing(listing):
    _, seen = listing
    await simulation.list_jobs(owner_id=OWNER, organization_id="", cursor="!!!not-a-cursor!!!")
    assert seen["before"] is None


async def test_the_organisation_reaches_the_repository_so_it_can_scope_on_it(listing):
    _, seen = listing
    await simulation.list_jobs(owner_id=OWNER, organization_id="11111111-1111-1111-1111-111111111111")
    assert seen["organization_id"] == "11111111-1111-1111-1111-111111111111"
