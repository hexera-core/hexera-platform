# Responsibility: Verify an issued key authenticates its owner, and a revoked, expired, unknown or forged one does not.
from __future__ import annotations

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from meshpipeline.application import api_key_service as svc
from meshpipeline.contracts import api_key
from meshpipeline.contracts.identity import Credential

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def _db():
    return AsyncMock()


def _row(minted: api_key.MintedKey, **overrides) -> SimpleNamespace:
    row = SimpleNamespace(
        id=uuid.UUID("11111111-1111-4111-9111-111111111111"),
        owner_id="tenant-a", organization_id=None, name="ci",
        key_prefix=minted.key_prefix, key_hash=minted.key_hash, plan="",
        created_at=NOW - timedelta(days=1), last_used_at=None,
        revoked_at=None, expires_at=None)
    for k, v in overrides.items():
        setattr(row, k, v)
    return row


def _found(row):
    return patch.object(svc.api_key_repo, "get_by_prefix", new=AsyncMock(return_value=row))


def _absent():
    return patch.object(svc.api_key_repo, "get_by_prefix", new=AsyncMock(return_value=None))


# issuance

async def test_issue_stores_only_the_hash_and_returns_the_secret_once():
    created = {}

    async def _create(_db, **kwargs):
        created.update(kwargs)
        return _row(api_key.mint(), **{k: v for k, v in kwargs.items() if k != "key_hash"})

    with patch.object(svc.api_key_repo, "create", new=_create):
        issued = await svc.issue(_db(), owner_id="tenant-a", name="ci", plan="pro")

    assert created["owner_id"] == "tenant-a"
    assert created["key_prefix"] == issued.key_prefix
    assert created["key_hash"] == api_key.hash_secret(issued.secret)
    assert issued.secret not in str(created), "the secret reached the row that is stored"
    assert issued.presented.startswith(issued.key_prefix + "_")


async def test_an_issued_key_authenticates_as_its_owner():
    minted = api_key.mint()
    row = _row(minted, plan="pro")
    with _found(row), patch.object(svc.api_key_repo, "mark_used", new=AsyncMock()):
        principal = await svc.authenticate(_db(), minted.presented, now=NOW)

    assert principal is not None
    assert principal.owner_id == "tenant-a"
    assert principal.plan == "pro"
    assert principal.credential == Credential.api_key
    assert principal.key_id == str(row.id)


async def test_the_row_is_located_by_the_public_prefix_alone():
    minted = api_key.mint()
    lookup = AsyncMock(return_value=_row(minted))
    with patch.object(svc.api_key_repo, "get_by_prefix", new=lookup), \
         patch.object(svc.api_key_repo, "mark_used", new=AsyncMock()):
        await svc.authenticate(_db(), minted.presented, now=NOW)

    (_db_arg, prefix), _kwargs = lookup.call_args
    assert prefix == minted.key_prefix
    assert minted.secret not in prefix, "the secret is used as a lookup key"


# refusals

async def test_a_revoked_key_is_refused():
    minted = api_key.mint()
    row = _row(minted, revoked_at=NOW - timedelta(seconds=1))
    with _found(row), patch.object(svc.api_key_repo, "mark_used", new=AsyncMock()) as mark:
        assert await svc.authenticate(_db(), minted.presented, now=NOW) is None
    assert not mark.called, "a refused key still recorded a use"


async def test_an_expired_key_is_refused_and_the_boundary_is_exclusive():
    minted = api_key.mint()
    expired = _row(minted, expires_at=NOW - timedelta(seconds=1))
    at_the_instant = _row(minted, expires_at=NOW)
    still_live = _row(minted, expires_at=NOW + timedelta(seconds=1))

    with _found(expired):
        assert await svc.authenticate(_db(), minted.presented, now=NOW) is None
    with _found(at_the_instant):
        assert await svc.authenticate(_db(), minted.presented, now=NOW) is None
    with _found(still_live), patch.object(svc.api_key_repo, "mark_used", new=AsyncMock()):
        assert await svc.authenticate(_db(), minted.presented, now=NOW) is not None


async def test_an_expiry_stored_without_a_timezone_is_read_as_utc():
    # Postgres returns an aware value; a fake, a fixture or a driver that does not must not make
    # an expired key live by raising a comparison error.
    minted = api_key.mint()
    row = _row(minted, expires_at=datetime(2026, 8, 30, 11, 0))
    with _found(row):
        assert await svc.authenticate(_db(), minted.presented, now=NOW) is None


async def test_an_unknown_prefix_is_refused():
    with _absent():
        assert await svc.authenticate(_db(), api_key.mint().presented, now=NOW) is None


async def test_an_unknown_prefix_still_costs_a_comparison():
    # Otherwise the response time says whether a prefix exists, which is a free enumeration oracle.
    with _absent(), patch.object(hmac, "compare_digest", wraps=hmac.compare_digest) as compare:
        assert await svc.authenticate(_db(), api_key.mint().presented, now=NOW) is None
    assert compare.called, "an unknown prefix returns before comparing anything"


async def test_the_wrong_secret_for_a_real_prefix_is_refused():
    minted, other = api_key.mint(), api_key.mint()
    forged = f"{minted.key_prefix}_{other.secret}"
    with _found(_row(minted)):
        assert await svc.authenticate(_db(), forged, now=NOW) is None


async def test_a_credential_that_is_not_a_key_never_reaches_the_database():
    lookup = AsyncMock(return_value=None)
    with patch.object(svc.api_key_repo, "get_by_prefix", new=lookup):
        for presented in (None, "", "sk-something", "hx_test_a_b"):
            assert await svc.authenticate(_db(), presented, now=NOW) is None
    assert not lookup.called


async def test_the_secret_is_verified_before_the_key_state_is_trusted():
    minted, other = api_key.mint(), api_key.mint()
    row = _row(minted, revoked_at=NOW)
    with _found(row):
        assert await svc.authenticate(_db(), f"{minted.key_prefix}_{other.secret}", now=NOW) is None


# staleness

async def test_a_first_use_records_last_used_at():
    minted = api_key.mint()
    mark = AsyncMock()
    with _found(_row(minted, last_used_at=None)), patch.object(svc.api_key_repo, "mark_used", new=mark):
        await svc.authenticate(_db(), minted.presented, now=NOW)
    assert mark.call_args.kwargs["at"] == NOW


async def test_a_use_within_the_recording_resolution_writes_nothing():
    # Every authenticated request would otherwise be an UPDATE on the hottest row in the table.
    minted = api_key.mint()
    mark = AsyncMock()
    fresh = NOW - timedelta(seconds=svc.LAST_USED_RESOLUTION_SECONDS - 1)
    with _found(_row(minted, last_used_at=fresh)), patch.object(svc.api_key_repo, "mark_used", new=mark):
        await svc.authenticate(_db(), minted.presented, now=NOW)
    assert not mark.called


async def test_a_use_after_the_recording_resolution_writes_again():
    minted = api_key.mint()
    mark = AsyncMock()
    stale = NOW - timedelta(seconds=svc.LAST_USED_RESOLUTION_SECONDS + 1)
    with _found(_row(minted, last_used_at=stale)), patch.object(svc.api_key_repo, "mark_used", new=mark):
        await svc.authenticate(_db(), minted.presented, now=NOW)
    assert mark.called


# revocation

async def test_revocation_is_scoped_to_the_owner_that_holds_the_key():
    revoke = AsyncMock(return_value=True)
    key_id = uuid.uuid4()
    with patch.object(svc.api_key_repo, "revoke", new=revoke):
        assert await svc.revoke(_db(), owner_id="tenant-a", key_id=key_id, now=NOW) is True
    assert revoke.call_args.kwargs["owner_id"] == "tenant-a"
    assert revoke.call_args.kwargs["key_id"] == key_id
    assert revoke.call_args.kwargs["at"] == NOW


async def test_revoking_a_key_another_tenant_holds_reports_nothing_revoked():
    with patch.object(svc.api_key_repo, "revoke", new=AsyncMock(return_value=False)):
        assert await svc.revoke(_db(), owner_id="tenant-b", key_id=uuid.uuid4(), now=NOW) is False


# the organisation seam

async def test_the_principal_carries_the_organisation_when_the_key_names_one():
    minted = api_key.mint()
    org = uuid.uuid4()
    with _found(_row(minted, organization_id=org)), patch.object(svc.api_key_repo, "mark_used", new=AsyncMock()):
        principal = await svc.authenticate(_db(), minted.presented, now=NOW)
    assert principal.organization_id == str(org)


async def test_a_key_with_no_organisation_still_authenticates_its_owner():
    minted = api_key.mint()
    with _found(_row(minted, organization_id=None)), patch.object(svc.api_key_repo, "mark_used", new=AsyncMock()):
        principal = await svc.authenticate(_db(), minted.presented, now=NOW)
    assert principal.organization_id == ""
    assert principal.owner_id == "tenant-a"
