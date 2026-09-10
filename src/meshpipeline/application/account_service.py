# Responsibility: Turn a verified Identity Platform token into the account it names, provisioning one the first time.
# Owns: the rule that a new account is a user, an organisation, a membership and one grant - all or none of them.
# Boundaries: it decides identity and tenancy, never authorisation.
from __future__ import annotations

import hashlib
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


class LinkRefused(Exception):
    """The token names an EXISTING account it has not proven it owns.

    Raised, never returned, and never described to the caller: `api/auth.py` answers it with the
    same undifferentiated refusal every other token failure gets, because the difference between
    "that address is taken" and "that token did not verify" is exactly the disclosure an attacker
    probing addresses is looking for.
    """


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
                               now: datetime | None = None,
                               organization_name: str = "") -> Account:
    at = now or datetime.now(UTC)

    # 1. THE UID PATH: the ordinary case, every sign-in after the first.
    user = await user_repo.get_by_firebase_uid(db, token.uid)
    provisioned = False

    if user is None:
        # 2. THE LINKING PATH. A row already carries this address but no uid - which is exactly
        # what 0004's backfill leaves for every owner who predates Identity Platform. Attaching
        # the uid is what makes their existing jobs and geometry follow them in. It is NOT a
        # signup, so it is neither gated by CONSOLE_SIGNUP_ENABLED nor granted credits.
        #
        # LINKING REQUIRES A VERIFIED ADDRESS, and it is the ONLY path here that does. It hands a
        # presented uid somebody ELSE's tenant - their jobs, geometry, chat sessions, artifacts and
        # credit balance - on the strength of a string the token merely asserts. Anyone may
        # register any address in Identity Platform without verifying it, so without this gate an
        # attacker who knows a victim's address takes over every account 0004 backfilled. Proving
        # control of the mailbox is the only evidence that distinguishes the veteran returning
        # from the stranger claiming to be them. Provisioning a genuinely NEW account stays
        # ungated (spec section 4): there is no victim to take over, and nothing costly is behind
        # it yet.
        existing = await user_repo.get_by_email(db, token.email)
        if existing is not None:
            user = await _link(db, existing, token)
        else:
            # 3. THE SIGNUP PATH.
            if not polcfg.CONSOLE_SIGNUP_ENABLED:
                raise SignupDisabled("this deployment does not provision new accounts")
            try:
                user = await _provision(db, token, organization_name=organization_name)
                provisioned = True
            except IntegrityError:
                # A concurrent first sign-in won the unique index on firebase_uid. Roll back to
                # the savepoint the failed insert poisoned and read the row the winner wrote -
                # the loser must not answer with a refusal for an account that now exists.
                #
                # THE UID CONFLICT IS THE ONLY ONE THIS CAN BE. The other unique index this
                # transaction touches is `organizations.slug`, and `_slug_for` is injective in
                # the uid, so a slug conflict implies a uid conflict that the users insert has
                # already refused. If the re-read still finds nothing the assumption was wrong
                # and the original error is re-raised rather than mistranslated.
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


async def _link(db: AsyncSession, existing, token: VerifiedToken):
    """Attach `token.uid` to an existing row for the same address, or refuse.

    Every refusal here raises the SAME exception with no detail the caller can use: the attacker
    and the confused legitimate user must be given the same answer.
    """
    if existing.firebase_uid is not None:
        # A row that ALREADY names a uid is a live account, not a backfilled one. The old code
        # skipped the attach here but still returned this row, so a second Identity Platform
        # account for the same address - which a federated provider, or a project configured to
        # allow multiple accounts per address, makes possible - was handed the first one's tenant.
        # The equality case is not a takeover: it is our own concurrent request, whose uid read
        # at step 1 lost a race with the link that has since committed.
        if existing.firebase_uid == token.uid:
            return existing
        logger.warning("sign-in refused: a token presented a uid for an address that is already "
                       "linked to a different account")
        raise LinkRefused("this address is already linked to another account")

    if not token.email_verified:
        # Deliberately NOT logged with the address: this is the log line an enumeration attempt
        # would fill, and it would name people who do not have an account here.
        logger.info("sign-in refused: linking an existing account requires a verified address")
        raise LinkRefused("linking an existing account requires a verified address")

    if not await user_repo.attach_firebase_uid(db, user_id=existing.id, firebase_uid=token.uid):
        # `attach_firebase_uid` only updates a row whose uid is still NULL, so a False here means
        # somebody linked this row between the read above and this update. Accept it only when the
        # winner is US - the same uid, a duplicate of this very request - and refuse otherwise.
        winner = await user_repo.get_by_firebase_uid(db, token.uid)
        if winner is None or winner.id != existing.id:
            logger.warning("sign-in refused: an address was linked to another account "
                           "concurrently")
            raise LinkRefused("this address is already linked to another account")
        return winner
    return existing


async def _provision(db: AsyncSession, token: VerifiedToken, *, organization_name: str = ""):
    # ONE TRANSACTION, four writes. The session this runs in is committed by the caller's
    # `get_db()` context, so a failure anywhere here leaves no user without an organisation and
    # no organisation without its grant. That atomicity is the whole reason the grant is issued
    # here rather than by a later, separately-failing step.
    #
    # `organization_name` IS CALLER-SUPPLIED and reaches this function on the provision path
    # ALONE (design decision 6). The uid path resolves an account that already exists and the
    # linking path attaches a uid to somebody else's backfilled tenant; honouring a name on
    # either would let any token rename an organisation it did not create. Blank falls back to
    # the address, which is the behaviour every account provisioned before this cycle got.
    name = (organization_name or "").strip() or token.email
    user = await user_repo.create(db, email=token.email, name=token.name,
                                  firebase_uid=token.uid)
    organization = await organization_repo.create(db, name=name,
                                                  slug=_slug_for(token.uid))
    await membership_repo.create(db, user_id=user.id, organization_id=organization.id,
                                 role=MembershipRole.owner)
    granted = await credit_service.grant_signup_credits(db, organization_id=organization.id)
    logger.info("provisioned organisation %s for a new account, granted %s credits",
                organization.id, granted)
    return user


#: `organizations.slug` is String(64) with a unique index. "org-" + 40 + "-" + 8 = 53.
_SLUG_UID_CHARS = 40
_SLUG_DIGEST_CHARS = 8


def _slug_for(firebase_uid: str) -> str:
    # Derived from the uid rather than the email: an address contains characters a slug should
    # not, and two people at the same domain must not collide. The uid is already unique and
    # already URL-safe.
    #
    # THE DIGEST IS WHAT MAKES THIS INJECTIVE, and that is what keeps the IntegrityError handler
    # in `_provision` honest. Lowercasing and truncating alone are not: two distinct uids that
    # differ only in case, or only after the 48th character, produced the SAME slug and so a
    # second unique-index conflict the handler cannot recover from - it re-reads by uid, finds
    # nothing (nothing was inserted), and re-raises as an opaque 500. Hashing the EXACT uid means
    # a slug collision implies a uid collision, which the users insert refuses first, so the only
    # conflict `_provision` can now see is the concurrent-signup one it is written for.
    digest = hashlib.sha256(firebase_uid.encode("utf-8")).hexdigest()[:_SLUG_DIGEST_CHARS]
    return f"org-{firebase_uid.lower()[:_SLUG_UID_CHARS]}-{digest}"


async def organization_id_for_owner(db: AsyncSession, owner_id: str) -> str:
    # THE SEAM the signed-header credential resolves its tenant through, and the only place that
    # journey is written. A cache belongs here and nowhere else.
    if not owner_id:
        return ""
    organization_id = await membership_repo.organization_id_for_email(db, owner_id)
    return str(organization_id) if organization_id else ""
