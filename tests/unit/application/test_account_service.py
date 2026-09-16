# Responsibility: Verify a token becomes exactly one account, once, with exactly one grant.
from __future__ import annotations

import uuid

import pytest

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import account_service
from meshpipeline.contracts.firebase_token import VerifiedToken
from meshpipeline.persistence.models import MembershipRole

pytestmark = pytest.mark.asyncio


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Fakes:
    """Stands in for the four repositories and the credit service, sharing one store."""

    def __init__(self):
        self.users: list = []
        self.orgs: list = []
        self.memberships: list = []
        self.grants: list = []

    # users
    async def get_by_firebase_uid(self, db, firebase_uid):
        return next((u for u in self.users if u.firebase_uid == firebase_uid), None)

    async def get_by_email(self, db, email):
        return next((u for u in self.users if u.email == email.strip().lower()), None)

    async def create_user(self, db, *, email, name, firebase_uid):
        row = _Row(id=uuid.uuid4(), email=email.strip().lower(), name=name,
                   firebase_uid=firebase_uid, email_verified_at=None, last_login_at=None)
        self.users.append(row)
        return row

    async def attach_firebase_uid(self, db, *, user_id, firebase_uid):
        user = next((u for u in self.users if u.id == user_id), None)
        if user is None or user.firebase_uid is not None:
            return False
        user.firebase_uid = firebase_uid
        return True

    async def record_login(self, db, *, user_id, at, email_verified, name=""):
        user = next(u for u in self.users if u.id == user_id)
        user.last_login_at = at
        if name.strip():
            user.name = name.strip()
        if email_verified and user.email_verified_at is None:
            user.email_verified_at = at

    # organisations
    async def create_org(self, db, *, name, slug):
        row = _Row(id=uuid.uuid4(), name=name, slug=slug)
        self.orgs.append(row)
        return row

    # memberships
    async def create_membership(self, db, *, user_id, organization_id, role):
        row = _Row(id=uuid.uuid4(), user_id=user_id, organization_id=organization_id, role=role)
        self.memberships.append(row)
        return row

    async def organization_id_for_email(self, db, email):
        user = await self.get_by_email(db, email)
        if user is None:
            return None
        m = next((m for m in self.memberships if m.user_id == user.id), None)
        return m.organization_id if m else None


@pytest.fixture
def fakes(monkeypatch):
    f = _Fakes()

    class _UserRepo:
        get_by_firebase_uid = staticmethod(f.get_by_firebase_uid)
        get_by_email = staticmethod(f.get_by_email)
        create = staticmethod(f.create_user)
        attach_firebase_uid = staticmethod(f.attach_firebase_uid)
        record_login = staticmethod(f.record_login)

    class _OrgRepo:
        create = staticmethod(f.create_org)

    class _MembershipRepo:
        create = staticmethod(f.create_membership)
        organization_id_for_email = staticmethod(f.organization_id_for_email)

    async def fake_grant(db, *, organization_id):
        f.grants.append(organization_id)
        return polcfg.SIGNUP_GRANT_CREDITS

    monkeypatch.setattr(account_service, "user_repo", _UserRepo())
    monkeypatch.setattr(account_service, "organization_repo", _OrgRepo())
    monkeypatch.setattr(account_service, "membership_repo", _MembershipRepo())
    monkeypatch.setattr(account_service.credit_service, "grant_signup_credits", fake_grant)
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", True)
    return f


def _token(uid="uid-1", email="person@example.com", verified=True, name="A Person"):
    return VerifiedToken(uid=uid, email=email, email_verified=verified, name=name)


async def test_a_new_account_gets_a_user_an_organisation_a_membership_and_one_grant(fakes):
    account = await account_service.resolve_or_provision(None, _token())
    assert len(fakes.users) == 1
    assert len(fakes.orgs) == 1
    assert len(fakes.memberships) == 1
    assert fakes.grants == [fakes.orgs[0].id]
    assert account.provisioned is True
    assert account.owner_id == "person@example.com"
    assert account.organization_id == str(fakes.orgs[0].id)


async def test_the_new_member_owns_their_own_organisation(fakes):
    await account_service.resolve_or_provision(None, _token())
    assert fakes.memberships[0].role is MembershipRole.owner


async def test_a_second_sign_in_provisions_nothing_further(fakes):
    first = await account_service.resolve_or_provision(None, _token())
    second = await account_service.resolve_or_provision(None, _token())
    assert (len(fakes.users), len(fakes.orgs), len(fakes.memberships)) == (1, 1, 1)
    assert fakes.grants == [fakes.orgs[0].id], "the signup grant was issued twice"
    assert second.organization_id == first.organization_id
    assert second.provisioned is False


async def test_a_backfilled_user_is_linked_rather_than_duplicated(fakes):
    # The state 0004's backfill leaves behind: a user and an organisation exist for this email,
    # and no uid has ever been attached.
    existing = await fakes.create_user(None, email="veteran@example.com", name="",
                                       firebase_uid=None)
    org = await fakes.create_org(None, name="veteran@example.com", slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)

    account = await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com"))

    assert len(fakes.users) == 1, "signing up created a second user for an existing owner"
    assert len(fakes.orgs) == 1, "signing up stranded their existing rows in a new organisation"
    assert fakes.users[0].firebase_uid == "uid-veteran"
    assert account.organization_id == str(org.id)
    assert account.provisioned is False


async def test_a_linked_backfilled_user_is_not_granted_signup_credits(fakes):
    # A grant is for a NEW account. Issuing one here hands a balance to somebody who predates
    # the feature, every time the backfill runs against a fresh environment.
    existing = await fakes.create_user(None, email="veteran@example.com", name="",
                                       firebase_uid=None)
    org = await fakes.create_org(None, name="veteran@example.com", slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)
    await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com"))
    assert fakes.grants == []


async def test_signing_in_records_the_login(fakes):
    await account_service.resolve_or_provision(None, _token())
    assert fakes.users[0].last_login_at is not None


async def test_an_unverified_email_still_signs_in_and_is_reported_as_unverified(fakes):
    account = await account_service.resolve_or_provision(None, _token(verified=False))
    assert account.email_verified is False
    assert fakes.users[0].email_verified_at is None


async def test_signup_can_be_closed_without_locking_out_existing_accounts(fakes, monkeypatch):
    await account_service.resolve_or_provision(None, _token())
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)

    # The known account still signs in.
    account = await account_service.resolve_or_provision(None, _token())
    assert account.owner_id == "person@example.com"

    # A stranger does not get provisioned.
    with pytest.raises(account_service.SignupDisabled):
        await account_service.resolve_or_provision(None, _token(uid="uid-2",
                                                                email="new@example.com"))
    assert len(fakes.users) == 1


async def test_a_closed_signup_still_links_a_backfilled_user(fakes, monkeypatch):
    # Linking is not signing up: the account already exists, it simply has no uid yet. Refusing
    # here would lock every pre-existing owner out of their own data.
    existing = await fakes.create_user(None, email="veteran@example.com", name="",
                                       firebase_uid=None)
    org = await fakes.create_org(None, name="veteran@example.com", slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)
    monkeypatch.setattr(polcfg, "CONSOLE_SIGNUP_ENABLED", False)

    account = await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com"))
    assert account.organization_id == str(org.id)


async def test_an_owner_resolves_to_their_organisation(fakes):
    await account_service.resolve_or_provision(None, _token())
    found = await account_service.organization_id_for_owner(None, "person@example.com")
    assert found == str(fakes.orgs[0].id)


async def test_an_owner_with_no_membership_resolves_to_no_organisation(fakes):
    assert await account_service.organization_id_for_owner(None, "nobody@example.com") == ""


async def test_a_concurrent_first_sign_in_yields_one_organisation(fakes, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    real_create = fakes.create_user
    state = {"raised": False}

    async def create_then_lose_the_race(db, *, email, name, firebase_uid):
        if not state["raised"]:
            state["raised"] = True
            # Stand in for the winner having already inserted this uid.
            await real_create(db, email=email, name=name, firebase_uid=firebase_uid)
            await fakes.create_org(db, name=email, slug="org-winner")
            await fakes.create_membership(db, user_id=fakes.users[-1].id,
                                          organization_id=fakes.orgs[-1].id,
                                          role=MembershipRole.owner)
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return await real_create(db, email=email, name=name, firebase_uid=firebase_uid)

    class _UserRepo:
        get_by_firebase_uid = staticmethod(fakes.get_by_firebase_uid)
        get_by_email = staticmethod(fakes.get_by_email)
        create = staticmethod(create_then_lose_the_race)
        attach_firebase_uid = staticmethod(fakes.attach_firebase_uid)
        record_login = staticmethod(fakes.record_login)

    class _Session:
        async def rollback(self):
            return None

    monkeypatch.setattr(account_service, "user_repo", _UserRepo())

    account = await account_service.resolve_or_provision(_Session(), _token())
    assert len(fakes.users) == 1, "the race produced two users"
    assert len(fakes.orgs) == 1, "the race produced two organisations"
    assert account.organization_id == str(fakes.orgs[0].id)


# linking to an account that already exists

async def _backfilled(fakes, email="veteran@example.com"):
    """The state 0004 leaves for every owner who predates Identity Platform."""
    existing = await fakes.create_user(None, email=email, name="", firebase_uid=None)
    org = await fakes.create_org(None, name=email, slug="veteran")
    await fakes.create_membership(None, user_id=existing.id, organization_id=org.id,
                                  role=MembershipRole.owner)
    return existing, org


async def test_an_unverified_token_cannot_link_to_an_existing_account(fakes):
    # THE ACCOUNT-TAKEOVER PATH. Anyone may register any address in Identity Platform and skip
    # the verification email; presenting the resulting token used to hand over the victim's
    # tenant - their jobs, geometry, chat sessions, artifacts and credit balance - because
    # linking matched on the address alone. It must be refused, and the row must be untouched.
    existing, _ = await _backfilled(fakes)

    with pytest.raises(account_service.LinkRefused):
        await account_service.resolve_or_provision(
            None, _token(uid="uid-attacker", email="veteran@example.com", verified=False))

    assert existing.firebase_uid is None, "an unverified token was attached to somebody's account"
    assert len(fakes.users) == 1, "the refusal created a second user"
    assert fakes.grants == []


async def test_a_verified_token_links_to_the_same_existing_account(fakes):
    # THE OTHER HALF of the pair: verification is the ONLY variable between this and the case
    # above, so together they prove the gate is the verified flag and not something incidental.
    existing, org = await _backfilled(fakes)

    account = await account_service.resolve_or_provision(
        None, _token(uid="uid-veteran", email="veteran@example.com", verified=True))

    assert existing.firebase_uid == "uid-veteran"
    assert account.organization_id == str(org.id)
    assert account.provisioned is False
    assert len(fakes.users) == 1


async def test_a_token_whose_uid_differs_from_an_already_linked_row_is_refused(fakes):
    # The second hole in the same block: the old code skipped the attach for a row that already
    # named a uid but STILL returned it, so a second Identity Platform account for one address -
    # which a federated provider makes possible - was handed the first account's tenant. Even a
    # VERIFIED token must not do this: verification proves the mailbox, not that this uid is the
    # uid the account was linked to.
    await _backfilled(fakes)
    fakes.users[0].firebase_uid = "uid-the-real-owner"

    with pytest.raises(account_service.LinkRefused):
        await account_service.resolve_or_provision(
            None, _token(uid="uid-someone-else", email="veteran@example.com", verified=True))

    assert fakes.users[0].firebase_uid == "uid-the-real-owner"


async def test_the_same_uid_on_an_already_linked_row_still_signs_in(fakes):
    # Not a takeover: a duplicate of this very request whose uid read lost a race with the link
    # that has since committed. Refusing it would fail a legitimate concurrent sign-in.
    _, org = await _backfilled(fakes)
    fakes.users[0].firebase_uid = "uid-veteran"

    async def _no_uid_match(db, firebase_uid):
        # Stand in for the read at step 1 that ran before the concurrent link committed.
        return None

    original = account_service.user_repo.get_by_firebase_uid
    try:
        account_service.user_repo.get_by_firebase_uid = staticmethod(_no_uid_match)
        account = await account_service.resolve_or_provision(
            None, _token(uid="uid-veteran", email="veteran@example.com"))
    finally:
        account_service.user_repo.get_by_firebase_uid = original

    assert account.organization_id == str(org.id)


async def test_provisioning_a_brand_new_account_is_not_gated_on_verification(fakes):
    # Spec section 4 deliberately permits an unverified address to sign in. The distinction is
    # that a NEW account has no victim: nothing pre-existing is handed over, and the grant is
    # for the account this token just created. Only LINKING needs the mailbox proven.
    account = await account_service.resolve_or_provision(None, _token(verified=False))
    assert account.provisioned is True
    assert account.email_verified is False
    assert len(fakes.users) == 1 and fakes.grants == [fakes.orgs[0].id]


async def test_two_uids_differing_only_in_case_get_different_slugs(fakes):
    # `organizations.slug` is unique, and the slug used to be the lowercased, truncated uid - so
    # two distinct uids differing only in case (or only after the 48th character) collided, and
    # the IntegrityError handler could not recover from that conflict because it re-reads by uid
    # and nothing was inserted. The slug is now injective in the uid.
    assert account_service._slug_for("AbC") != account_service._slug_for("abc")
    assert len(account_service._slug_for("x" * 200)) <= 64


async def test_signing_in_again_with_a_changed_display_name_refreshes_the_stored_one(fakes):
    # users.name was written once, at provisioning. Someone who changed their display name in
    # Identity Platform - which is exactly what /settings/account does - kept the old one in
    # every reader of this column, the organisation member list included, indefinitely.
    await account_service.resolve_or_provision(None, _token(name="Original Name"))
    await account_service.resolve_or_provision(None, _token(name="Renamed Engineer"))
    assert fakes.users[0].name == "Renamed Engineer"


async def test_a_sign_in_with_no_name_claim_leaves_the_stored_name_alone(fakes):
    # An absent claim is not an instruction to forget the name on file.
    await account_service.resolve_or_provision(None, _token(name="Original Name"))
    await account_service.resolve_or_provision(None, _token(name=""))
    assert fakes.users[0].name == "Original Name"
