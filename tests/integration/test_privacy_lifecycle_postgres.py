# Responsibility: Verify against real PostgreSQL that erasure removes what names a person and keeps what the law
#                 requires, and that the signup grant is issued once per mailbox.
# Boundaries: the persistence half of privacy_service and signup_claims; the routes are covered in the unit tier.
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("real PostgreSQL is required", allow_module_level=True)


async def _owner(db):
    from meshpipeline.application import api_key_service, credit_service
    from meshpipeline.persistence.models import ChatSession, MembershipRole
    from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
    from meshpipeline.persistence.repositories.organization_repository import (
        OrganizationRepository,
    )
    from meshpipeline.persistence.repositories.user_repository import UserRepository

    suffix = uuid.uuid4().hex[:12]
    email = f"privacy-{suffix}@example.com"
    org = await OrganizationRepository().create(db, name="Privacy Co", slug=f"org-{suffix}")
    user = await UserRepository().create(db, email=email, name="P", firebase_uid=f"uid-{suffix}")
    await MembershipRepository().create(db, user_id=user.id, organization_id=org.id,
                                        role=MembershipRole.owner)
    granted = await credit_service.grant_signup_credits(db, organization_id=org.id, email=email)
    await api_key_service.issue(db, owner_id=email, name="ci", organization_id=org.id)
    db.add(ChatSession(owner_id=email, organization_id=org.id))
    await db.commit()
    return email, org.id, user.id, granted


async def test_a_mailbox_is_granted_once_even_after_erasure(db):
    from meshpipeline.application import credit_service, privacy_service
    from meshpipeline.persistence.repositories.organization_repository import (
        OrganizationRepository,
    )

    email, org_id, _, granted = await _owner(db)
    assert granted > 0
    await privacy_service.request_erasure(db, owner_id=email, organization_id=str(org_id))
    await db.commit()

    second = await OrganizationRepository().create(db, name="Again",
                                                   slug=f"org-{uuid.uuid4().hex[:12]}")
    alias = email.replace("@", "+again@")
    assert await credit_service.grant_signup_credits(
        db, organization_id=second.id, email=alias) == 0
    assert await credit_service.balance(db, organization_id=second.id) == 0


async def test_erasure_removes_the_person_and_keeps_the_ledger(db):
    from meshpipeline.application import credit_service, privacy_service
    from meshpipeline.persistence.models import (
        ApiKey,
        ChatSession,
        Membership,
        Organization,
        User,
    )

    email, org_id, user_id, granted = await _owner(db)
    exported = await privacy_service.export_account(db, owner_id=email,
                                                    organization_id=str(org_id))
    assert exported["user"]["email"] == email
    assert len(exported["tables"]["api_keys"]) == 1
    assert "key_hash" not in exported["tables"]["api_keys"][0]
    assert len(exported["tables"]["chat_sessions"]) == 1

    result = await privacy_service.request_erasure(db, owner_id=email,
                                                   organization_id=str(org_id))
    await db.commit()
    assert result["status"] == "requested"

    assert await db.get(User, user_id) is None
    assert (await db.execute(select(func.count()).select_from(Membership)
                             .where(Membership.organization_id == org_id))).scalar_one() == 0
    assert (await db.execute(select(func.count()).select_from(ChatSession)
                             .where(ChatSession.organization_id == org_id))).scalar_one() == 0
    key = (await db.execute(select(ApiKey).where(ApiKey.organization_id == org_id))).scalar_one()
    assert key.revoked_at is not None
    assert key.owner_id == privacy_service.erased_owner(org_id)

    org = await db.get(Organization, org_id)
    assert org.name == privacy_service.ERASED_ORGANIZATION_NAME
    assert org.erasure_requested_at is not None
    # The ledger is an accounting record and survives erasure.
    assert await credit_service.balance(db, organization_id=org_id) == granted

    # Nobody is a member any more, so a lingering session cannot repeat or reverse the request.
    with pytest.raises(privacy_service.ErasureRefused):
        await privacy_service.request_erasure(db, owner_id=email, organization_id=str(org_id))


async def test_the_sweep_marks_an_erasure_complete(db):
    from meshpipeline.application import privacy_service
    from meshpipeline.persistence.models import Organization

    email, org_id, _, _ = await _owner(db)
    await privacy_service.request_erasure(db, owner_id=email, organization_id=str(org_id))
    await db.commit()

    class _NoStore:
        def exists(self, *, object_key):
            return False

        def delete_object(self, *, object_key):
            raise AssertionError("nothing was stored for this organisation")

    result = await privacy_service.sweep_erasures(db, _NoStore(), limit=1000)
    assert result["failed"] == 0
    org = await db.get(Organization, org_id)
    await db.refresh(org)
    assert org.erased_at is not None
