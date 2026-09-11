# tests/unit/api/test_organization_route.py
# Responsibility: Verify the organisation view is the caller's own and degrades to an owner view.
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from meshpipeline.api.v1 import organization
from meshpipeline.application import account_service

pytestmark = pytest.mark.asyncio

OWNER = "engineer@example.com"
OWN_ORG = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = uuid.UUID("22222222-2222-2222-2222-222222222222")


@dataclass
class _Org:
    id: uuid.UUID
    name: str
    slug: str


@dataclass
class _User:
    id: uuid.UUID
    email: str
    name: str


@pytest.fixture
def org(monkeypatch):
    row = _Org(id=OWN_ORG, name="Acme Aerospace", slug="org-abc-123")
    members = [(_User(id=uuid.uuid4(), email=OWNER, name="An Engineer"), "owner")]

    async def fake_get(db, organization_id):
        return row if organization_id == OWN_ORG else None

    async def fake_members(db, *, organization_id):
        return members if organization_id == OWN_ORG else []

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    # THE ROUTE HOLDS NO REPOSITORY. It is transport over `account_service.organization_view`,
    # which owns the degrade below, so the doubles go at the service's own seam - one level
    # further in than they used to sit, and the only place the route can still be reached
    # through. Patching `organization_view` itself instead would move DECISION 12 into this
    # fixture and leave the two degradation tests asserting against their own stub.
    monkeypatch.setattr(account_service.organization_repo, "get_by_id", fake_get)
    monkeypatch.setattr(account_service.membership_repo, "list_members", fake_members)
    monkeypatch.setattr(organization, "get_db", lambda: _NullSession())
    return row


async def test_the_organisation_is_the_callers_own(org):
    payload = await organization.read_organization(owner_id=OWNER, organization_id=str(OWN_ORG))
    assert payload["organization"]["name"] == "Acme Aerospace"
    assert payload["organization"]["slug"] == "org-abc-123"
    assert payload["members"] == [{"email": OWNER, "name": "An Engineer", "role": "owner"}]


async def test_another_organisation_is_not_reachable(org):
    payload = await organization.read_organization(owner_id=OWNER, organization_id=str(OTHER_ORG))
    assert payload["organization"] is None
    assert payload["members"] == []


async def test_a_caller_with_no_organisation_sees_themselves_rather_than_an_error(org):
    # Decision 12: credits.py set the precedent that a mid-migration caller is degraded, not
    # refused. An empty members list would read as "your account vanished".
    payload = await organization.read_organization(owner_id=OWNER, organization_id="")
    assert payload["organization"] is None
    assert payload["members"] == [{"email": OWNER, "name": OWNER, "role": "owner"}]


async def test_a_malformed_organisation_degrades_the_same_way(org):
    payload = await organization.read_organization(owner_id=OWNER, organization_id="not-a-uuid")
    assert payload["members"] == [{"email": OWNER, "name": OWNER, "role": "owner"}]
