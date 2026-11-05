# Responsibility: Verify a proven caller resolves to the organisation they act within, whichever credential proved them.
from __future__ import annotations

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.api import security
from meshpipeline.contracts.identity import Credential

pytestmark = pytest.mark.asyncio


@pytest.fixture
def memberships(monkeypatch):
    store = {"person@example.com": "org-1"}

    async def fake_lookup(db, owner_id):
        return store.get(owner_id, "")

    class _NullSession:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(security.account_service, "organization_id_for_owner", fake_lookup)
    monkeypatch.setattr(security, "get_db", lambda: _NullSession())
    monkeypatch.setattr(polcfg, "MESH_API_KEY", "")
    monkeypatch.setattr(polcfg, "USER_TOKEN_SECRET", "")
    return store


async def test_a_header_caller_resolves_to_their_organisation(memberships):
    principal = await security.resolve_principal(None, None, "person@example.com", None)
    assert principal.owner_id == "person@example.com"
    assert principal.organization_id == "org-1"
    assert principal.credential is Credential.self_asserted


async def test_an_owner_with_no_membership_resolves_to_no_organisation(memberships):
    principal = await security.resolve_principal(None, None, "stranger@example.com", None)
    assert principal.organization_id == ""


async def test_a_failed_lookup_does_not_refuse_the_caller(memberships, monkeypatch):
    # The organisation is a SCOPE, not a credential. A database hiccup while resolving it must
    # degrade to today's owner-only behaviour, not turn a proven caller into a 500.
    async def explode(db, owner_id):
        raise RuntimeError("the database is having a moment")

    monkeypatch.setattr(security.account_service, "organization_id_for_owner", explode)
    principal = await security.resolve_principal(None, None, "person@example.com", None)
    assert principal.owner_id == "person@example.com"
    assert principal.organization_id == ""


async def test_a_key_caller_keeps_the_organisation_its_row_names(memberships, monkeypatch):
    # A key already carries its organisation. Re-resolving it from the owner would override the
    # row - and a key may later be scoped to one organisation while its owner belongs to several.
    from meshpipeline.contracts.identity import Principal

    async def fake_key_principal(presented):
        return Principal(owner_id="person@example.com", organization_id="org-from-the-key",
                         credential=Credential.api_key)

    monkeypatch.setattr(security, "_principal_from_key", fake_key_principal)
    principal = await security.resolve_principal("Bearer hx_live_abc_def", None, None, None)
    assert principal.organization_id == "org-from-the-key"


async def test_org_dep_reports_what_the_principal_carries():
    from meshpipeline.contracts.identity import Principal
    assert await security.org_dep(Principal(owner_id="x", organization_id="org-9")) == "org-9"


async def test_a_failed_lookup_is_logged_at_error_not_warning(memberships, monkeypatch, caplog):
    # Failing open is right here, but it is not symmetric. On a READ it costs nothing durable.
    # On a WRITE the same empty organisation reaches `tenant_scope.stamp`, which writes owner_id
    # alone - and that row is then invisible to every later org-scoped read, forever, because
    # 0004's backfill is a one-shot that has already run. A transient blip therefore leaves
    # permanent damage, and rows like these would also block 0005's NOT NULL. That has to page
    # somebody rather than sit in a warning stream nobody reads.
    import logging

    async def explode(db, owner_id):
        raise RuntimeError("the database is having a moment")

    monkeypatch.setattr(security.account_service, "organization_id_for_owner", explode)
    caplog.set_level(logging.DEBUG, logger=security.logger.name)
    await security.resolve_principal(None, None, "person@example.com", None)

    records = [r for r in caplog.records if "could not resolve an organisation" in r.getMessage()]
    assert records, "the fail-open was silent"
    assert all(r.levelno >= logging.ERROR for r in records), (
        f"the durable fail-open logged at {records[0].levelname}, not ERROR")
