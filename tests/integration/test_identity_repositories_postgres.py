# Responsibility: Verify the identity and credit repositories read and write the rows the services depend on.
# Boundaries: an ordinary member of the integration tier - the tier's own conftest builds the whole
# schema and this file uses the `db` session it already provides, isolating only its own four tables.
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from tests import harness_provisioning as hp

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

import meshpipeline.settings.providers as provcfg
from meshpipeline.persistence.models import CreditEntryType, MembershipRole
from meshpipeline.persistence.repositories.credit_ledger_repository import (
    CreditLedgerRepository,
)
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)
from meshpipeline.persistence.repositories.user_repository import UserRepository

# organizations/users/memberships/credit_ledger are exclusively this suite's within the tier - no
# other integration test writes a row to any of them - so a per-test TRUNCATE is enough isolation.
# The schema itself is built once per session by conftest's `_provisioned_stack`/`_schema_present`;
# this reuses `hp.truncate_tables`, the same helper test_job_transitions_postgres.py uses for its
# own table, rather than a second schema-management scheme. CASCADE lets one call clear all four
# regardless of their foreign-key order.
_TABLES = ("credit_ledger", "memberships", "users", "organizations")


@pytest.fixture(autouse=True)
async def _clean_identity_tables():
    await hp.truncate_tables(provcfg.POSTGRES_DSN, *_TABLES)


async def _org_and_user(db, email="owner@example.com", uid="uid-1"):
    org = await OrganizationRepository().create(db, name=email, slug=f"org-{uuid.uuid4().hex[:8]}")
    user = await UserRepository().create(db, email=email, name="", firebase_uid=uid)
    await MembershipRepository().create(db, user_id=user.id, organization_id=org.id,
                                        role=MembershipRole.owner)
    await db.flush()
    return org, user


async def test_a_user_is_found_by_their_firebase_uid(db):
    _, user = await _org_and_user(db)
    found = await UserRepository().get_by_firebase_uid(db, "uid-1")
    assert found is not None and found.id == user.id


async def test_an_unknown_firebase_uid_is_absent(db):
    await _org_and_user(db)
    assert await UserRepository().get_by_firebase_uid(db, "uid-absent") is None


async def test_a_user_is_found_by_email_case_insensitively(db):
    _, user = await _org_and_user(db, email="mixed@example.com")
    found = await UserRepository().get_by_email(db, "MIXED@Example.COM")
    assert found is not None and found.id == user.id


async def test_a_backfilled_user_can_have_a_uid_attached_once(db):
    user = await UserRepository().create(db, email="later@example.com", name="",
                                         firebase_uid=None)
    await db.flush()
    assert await UserRepository().attach_firebase_uid(db, user_id=user.id,
                                                      firebase_uid="uid-later") is True
    # A second attach finds no row with a null uid and reports so, rather than overwriting one.
    assert await UserRepository().attach_firebase_uid(db, user_id=user.id,
                                                      firebase_uid="uid-other") is False
    await db.refresh(user)
    assert user.firebase_uid == "uid-later"


async def test_recording_a_login_stamps_the_moment_and_the_verification(db):
    _, user = await _org_and_user(db)
    at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    await UserRepository().record_login(db, user_id=user.id, at=at, email_verified=True)
    await db.refresh(user)
    assert user.last_login_at is not None
    assert user.email_verified_at is not None


async def test_verification_is_recorded_once_and_not_moved_by_later_logins(db):
    _, user = await _org_and_user(db)
    first = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    later = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    await UserRepository().record_login(db, user_id=user.id, at=first, email_verified=True)
    await db.refresh(user)
    stamped = user.email_verified_at
    await UserRepository().record_login(db, user_id=user.id, at=later, email_verified=True)
    await db.refresh(user)
    assert user.email_verified_at == stamped
    assert user.last_login_at != stamped


async def test_an_unverified_login_leaves_the_verification_unstamped(db):
    _, user = await _org_and_user(db)
    at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    await UserRepository().record_login(db, user_id=user.id, at=at, email_verified=False)
    await db.refresh(user)
    assert user.email_verified_at is None
    assert user.last_login_at is not None


async def test_an_email_resolves_to_the_organisation_its_user_belongs_to(db):
    org, _ = await _org_and_user(db, email="member@example.com", uid="uid-m")
    found = await MembershipRepository().organization_id_for_email(db, "MEMBER@example.com")
    assert found == org.id


async def test_an_email_with_no_user_resolves_to_no_organisation(db):
    await _org_and_user(db)
    assert await MembershipRepository().organization_id_for_email(db, "nobody@example.com") is None


async def test_the_balance_of_an_organisation_with_no_entries_is_zero(db):
    org, _ = await _org_and_user(db)
    assert await CreditLedgerRepository().balance(db, organization_id=org.id) == 0


async def test_the_balance_is_the_sum_of_the_entries(db):
    org, _ = await _org_and_user(db)
    repo = CreditLedgerRepository()
    await repo.append(db, organization_id=org.id, entry_type=CreditEntryType.grant,
                      amount=100, reason="signup")
    await repo.append(db, organization_id=org.id, entry_type=CreditEntryType.debit,
                      amount=-30, reason="job")
    await db.flush()
    assert await repo.balance(db, organization_id=org.id) == 70


async def test_one_organisations_entries_do_not_reach_another(db):
    org_a, _ = await _org_and_user(db, email="a@example.com", uid="uid-a")
    org_b = await OrganizationRepository().create(db, name="b", slug="org-b")
    repo = CreditLedgerRepository()
    await repo.append(db, organization_id=org_a.id, entry_type=CreditEntryType.grant,
                      amount=100, reason="signup")
    await db.flush()
    assert await repo.balance(db, organization_id=org_b.id) == 0
