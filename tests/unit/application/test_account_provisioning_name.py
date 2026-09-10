# tests/unit/application/test_account_provisioning_name.py
# Responsibility: Verify a caller-supplied organisation name reaches provisioning and nothing else.
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from meshpipeline.application import account_service
from meshpipeline.contracts.firebase_token import VerifiedToken

pytestmark = pytest.mark.asyncio


@dataclass
class _User:
    id: uuid.UUID
    email: str
    name: str = ""
    firebase_uid: str | None = None


@dataclass
class _Org:
    id: uuid.UUID
    name: str
    slug: str


@dataclass
class _Recorder:
    organizations: list = field(default_factory=list)


def _token(uid: str, email: str, *, verified: bool = True) -> VerifiedToken:
    return VerifiedToken(uid=uid, email=email, email_verified=verified, name="An Engineer")


@pytest.fixture
def provisioning(monkeypatch):
    recorder = _Recorder()
    users: dict = {}

    async def fake_get_by_uid(db, uid):
        return users.get(uid)

    async def fake_get_by_email(db, email):
        return next((user for user in users.values() if user.email == email), None)

    async def fake_create_user(db, *, email, name, firebase_uid):
        user = _User(id=uuid.uuid4(), email=email, name=name, firebase_uid=firebase_uid)
        users[firebase_uid] = user
        return user

    async def fake_create_org(db, *, name, slug):
        org = _Org(id=uuid.uuid4(), name=name, slug=slug)
        recorder.organizations.append(org)
        return org

    async def fake_create_membership(db, *, user_id, organization_id, role):
        return None

    async def fake_grant(db, *, organization_id):
        return 500

    async def fake_record_login(db, *, user_id, at, email_verified):
        return None

    async def fake_org_for_email(db, email):
        return recorder.organizations[0].id if recorder.organizations else None

    async def fake_attach(db, *, user_id, firebase_uid):
        for user in users.values():
            if user.id == user_id:
                user.firebase_uid = firebase_uid
                return True
        return False

    monkeypatch.setattr(account_service.user_repo, "get_by_firebase_uid", fake_get_by_uid)
    monkeypatch.setattr(account_service.user_repo, "get_by_email", fake_get_by_email)
    monkeypatch.setattr(account_service.user_repo, "create", fake_create_user)
    monkeypatch.setattr(account_service.user_repo, "record_login", fake_record_login)
    monkeypatch.setattr(account_service.user_repo, "attach_firebase_uid", fake_attach)
    monkeypatch.setattr(account_service.organization_repo, "create", fake_create_org)
    monkeypatch.setattr(account_service.membership_repo, "create", fake_create_membership)
    monkeypatch.setattr(account_service.membership_repo, "organization_id_for_email",
                        fake_org_for_email)
    monkeypatch.setattr(account_service.credit_service, "grant_signup_credits", fake_grant)
    return recorder, users


async def test_a_new_account_names_its_organisation_after_the_company(provisioning):
    recorder, _ = provisioning
    await account_service.resolve_or_provision(
        None, _token("uid-1", "engineer@acme.test"), organization_name="Acme Aerospace")
    assert recorder.organizations[0].name == "Acme Aerospace"


async def test_without_a_company_the_organisation_falls_back_to_the_address(provisioning):
    recorder, _ = provisioning
    await account_service.resolve_or_provision(None, _token("uid-2", "solo@acme.test"))
    assert recorder.organizations[0].name == "solo@acme.test"


async def test_a_blank_company_falls_back_rather_than_naming_an_organisation_empty(provisioning):
    recorder, _ = provisioning
    await account_service.resolve_or_provision(
        None, _token("uid-3", "blank@acme.test"), organization_name="   ")
    assert recorder.organizations[0].name == "blank@acme.test"


async def test_an_existing_account_signing_in_again_cannot_rename_its_organisation(provisioning):
    # DECISION 6. The uid path resolves an account that already exists, so the field is ignored.
    recorder, _ = provisioning
    await account_service.resolve_or_provision(
        None, _token("uid-4", "repeat@acme.test"), organization_name="Original Name")
    await account_service.resolve_or_provision(
        None, _token("uid-4", "repeat@acme.test"), organization_name="Attacker Renamed This")
    assert len(recorder.organizations) == 1
    assert recorder.organizations[0].name == "Original Name"


async def test_a_linking_token_cannot_name_an_organisation(provisioning):
    # The linking path attaches a uid to a row that already exists -- somebody else's tenant,
    # from 0004's backfill. It provisions nothing, so it must create no organisation and rename
    # none either.
    recorder, users = provisioning
    backfilled = _User(id=uuid.uuid4(), email="veteran@acme.test", name="Veteran",
                       firebase_uid=None)
    users["__backfilled__"] = backfilled
    await account_service.resolve_or_provision(
        None, _token("uid-5", "veteran@acme.test"), organization_name="Attacker Named This")
    assert not any(org.name == "Attacker Named This" for org in recorder.organizations)
