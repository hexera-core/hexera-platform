# Responsibility: Verify the shared tenant predicate scopes on the organisation, and degrades to the owner when there is none.
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

_ORG = "11111111-1111-1111-1111-111111111111"


async def test_a_read_with_an_organisation_scopes_on_it():
    from meshpipeline.persistence.models import GeometrySource
    from meshpipeline.persistence.repositories import tenant_scope

    predicate = tenant_scope.scope(GeometrySource, owner_id="person@example.com",
                                   organization_id=_ORG)
    assert predicate.left.name == "organization_id"
    assert predicate.right.value == uuid.UUID(_ORG)


async def test_a_read_without_an_organisation_falls_back_to_the_owner():
    # A principal whose organisation could not be resolved - a database hiccup, or a deployment
    # mid-migration - must still see their own rows. Returning nothing would look like data loss.
    from meshpipeline.persistence.models import GeometrySource
    from meshpipeline.persistence.repositories import tenant_scope

    predicate = tenant_scope.scope(GeometrySource, owner_id="person@example.com",
                                   organization_id="")
    assert predicate.left.name == "owner_id"
    assert predicate.right.value == "person@example.com"


async def test_a_write_stamps_both():
    from meshpipeline.persistence.repositories import tenant_scope

    values = tenant_scope.stamp(owner_id="person@example.com", organization_id=_ORG)
    assert set(values) == {"owner_id", "organization_id"}
    assert values["owner_id"] == "person@example.com"
    assert values["organization_id"] == uuid.UUID(_ORG)


async def test_a_write_without_an_organisation_stamps_only_the_owner():
    from meshpipeline.persistence.repositories import tenant_scope

    values = tenant_scope.stamp(owner_id="person@example.com", organization_id="")
    assert set(values) == {"owner_id"}


async def test_a_read_with_a_malformed_organisation_falls_back_to_the_owner():
    # NOT an exception inside a request. tenant_scope is the one place this rule lives, so a
    # future caller passing something unsanitised (a new credential path, an admin tool, a test
    # helper) must degrade to owner-scoping - the safe, narrowing direction - rather than 500 a
    # proven request.
    from meshpipeline.persistence.models import GeometrySource
    from meshpipeline.persistence.repositories import tenant_scope

    predicate = tenant_scope.scope(GeometrySource, owner_id="person@example.com",
                                   organization_id="not-a-uuid")
    assert predicate.left.name == "owner_id"
    assert predicate.right.value == "person@example.com"


async def test_a_write_with_a_malformed_organisation_stamps_only_the_owner():
    from meshpipeline.persistence.repositories import tenant_scope

    values = tenant_scope.stamp(owner_id="person@example.com", organization_id="not-a-uuid")
    assert set(values) == {"owner_id"}
    assert values["owner_id"] == "person@example.com"
