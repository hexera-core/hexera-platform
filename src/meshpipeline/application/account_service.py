# Responsibility: Turn a verified Identity Platform token into the account it names, provisioning one the first time.
# Owns: the rule that a new account is a user, an organisation, a membership and one grant - all or none of them.
# Boundaries: it decides identity and tenancy, never authorisation.
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import meshpipeline.settings.policy as polcfg
from meshpipeline.application import credit_service
from meshpipeline.contracts.firebase_token import VerifiedToken
from meshpipeline.persistence.models import MembershipRole
from meshpipeline.persistence.repositories.membership_repository import MembershipRepository
from meshpipeline.persistence.repositories.organization_repository import (
    OrganizationRepository,
)
from meshpipeline.persistence.repositories.user_repository import UserRepository

logger = logging.getLogger(__name__)

user_repo = UserRepository()
organization_repo = OrganizationRepository()
membership_repo = MembershipRepository()


class SignupDisabled(Exception):
    """This deployment does not provision unrecognised accounts."""


@dataclass(frozen=True)
class Account:
    user_id: str
    # THE TENANT STRING every existing column scopes on: the lowercased email. Deliberately not
    # the Identity Platform uid - re-keying owner_id would rewrite live rows and detach every
    # existing job, session and geometry from the person who made them.
    owner_id: str
    organization_id: str
    email_verified: bool
    name: str
    #: whether THIS call created the account. The caller uses it for nothing but logging; it is
    #: here because a test cannot otherwise tell "provisioned" from "found" without counting rows.
    provisioned: bool


async def resolve_or_provision(db: AsyncSession, token: VerifiedToken, *,
                               now: datetime | None = None) -> Account:
    at = now or datetime.now(UTC)

    # 1. THE UID PATH: the ordinary case, every sign-in after the first.
    user = await user_repo.get_by_firebase_uid(db, token.uid)
    provisioned = False

    if user is None:
        # 2. THE LINKING PATH. A row already carries this address but no uid - which is exactly
        # what 0004's backfill leaves for every owner who predates Identity Platform. Attaching
        # the uid is what makes their existing jobs and geometry follow them in. It is NOT a
        # signup, so it is neither gated by CONSOLE_SIGNUP_ENABLED nor granted credits.
        existing = await user_repo.get_by_email(db, token.email)
        if existing is not None:
            if existing.firebase_uid is None:
                await user_repo.attach_firebase_uid(db, user_id=existing.id,
                                                    firebase_uid=token.uid)
            user = existing
        else:
            # 3. THE SIGNUP PATH.
            if not polcfg.CONSOLE_SIGNUP_ENABLED:
                raise SignupDisabled("this deployment does not provision new accounts")
            try:
                user = await _provision(db, token)
                provisioned = True
            except IntegrityError:
                # A concurrent first sign-in won the unique index on firebase_uid. Roll back to
                # the savepoint the failed insert poisoned and read the row the winner wrote -
                # the loser must not answer with a refusal for an account that now exists.
                await db.rollback()
                user = await user_repo.get_by_firebase_uid(db, token.uid)
                if user is None:
                    raise

    await user_repo.record_login(db, user_id=user.id, at=at,
                                 email_verified=token.email_verified)

    organization_id = await membership_repo.organization_id_for_email(db, token.email)
    return Account(
        user_id=str(user.id),
        owner_id=token.email,
        organization_id=str(organization_id) if organization_id else "",
        email_verified=token.email_verified,
        name=token.name or user.name or token.email,
        provisioned=provisioned,
    )


async def _provision(db: AsyncSession, token: VerifiedToken):
    # ONE TRANSACTION, four writes. The session this runs in is committed by the caller's
    # `get_db()` context, so a failure anywhere here leaves no user without an organisation and
    # no organisation without its grant. That atomicity is the whole reason the grant is issued
    # here rather than by a later, separately-failing step.
    user = await user_repo.create(db, email=token.email, name=token.name,
                                  firebase_uid=token.uid)
    organization = await organization_repo.create(db, name=token.email,
                                                  slug=_slug_for(token.uid))
    await membership_repo.create(db, user_id=user.id, organization_id=organization.id,
                                 role=MembershipRole.owner)
    granted = await credit_service.grant_signup_credits(db, organization_id=organization.id)
    logger.info("provisioned organisation %s for a new account, granted %s credits",
                organization.id, granted)
    return user


def _slug_for(firebase_uid: str) -> str:
    # Derived from the uid rather than the email: an address contains characters a slug should
    # not, and two people at the same domain must not collide. The uid is already unique and
    # already URL-safe.
    return f"org-{firebase_uid.lower()[:48]}"


async def organization_id_for_owner(db: AsyncSession, owner_id: str) -> str:
    # THE SEAM the signed-header credential resolves its tenant through, and the only place that
    # journey is written. A cache belongs here and nowhere else.
    if not owner_id:
        return ""
    organization_id = await membership_repo.organization_id_for_email(db, owner_id)
    return str(organization_id) if organization_id else ""
