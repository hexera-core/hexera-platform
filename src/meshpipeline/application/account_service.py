# Responsibility: Turn a verified Identity Platform token into the account it names, provisioning one the first time.
# Owns: the rule that a new account is a user first, and an organisation, a membership and one grant only
#       once its address is verified - those three all or none of them.
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
from meshpipeline.persistence.models import MembershipRole, Organization
from meshpipeline.persistence.repositories import tenant_scope
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
    #: Audit events this resolution earned, as (action, kwargs) pairs for audit.record. CARRIED,
    #: NOT EMITTED: everything here ran inside the caller's uncommitted transaction, and a line
    #: describing a rolled-back grant or provisioning would record something that never happened.
    #: api/auth.py emits them once get_db has committed.
    audit_events: tuple = ()


async def resolve_or_provision(db: AsyncSession, token: VerifiedToken, *,
                               now: datetime | None = None,
                               organization_name: str = "") -> Account:
    at = now or datetime.now(UTC)
    events: list = []

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
        # from the stranger claiming to be them.
        existing = await user_repo.get_by_email(db, token.email)
        if existing is not None:
            user = await _link(db, existing, token, events)
        else:
            # 3. THE SIGNUP PATH. Only the users row is written here - the person exists, but no
            # organisation, membership or credit does until the address is verified (step 4).
            if not polcfg.CONSOLE_SIGNUP_ENABLED:
                raise SignupDisabled("this deployment does not provision new accounts")
            try:
                user = await user_repo.create(
                    db, email=token.email, name=token.name, firebase_uid=token.uid,
                    pending_organization_name=(organization_name or "").strip() or None)
            except IntegrityError:
                # A concurrent first sign-in won one of the two unique indexes this insert can
                # hit. Roll back to the savepoint the failed insert poisoned and read what the
                # winner wrote - the loser must not answer with a refusal for an account that now
                # exists. The SAME uid (a duplicate of this very request) resolves by uid; a
                # DIFFERENT uid that won `users.email` goes through `_link`, which applies the
                # same verified-mailbox rules as if the row had been found at step 2. If neither
                # re-read finds anything the assumption was wrong and the original error is
                # re-raised rather than mistranslated.
                await db.rollback()
                user = await user_repo.get_by_firebase_uid(db, token.uid)
                if user is None:
                    existing = await user_repo.get_by_email(db, token.email)
                    if existing is None:
                        raise
                    user = await _link(db, existing, token, events)

    # `token.name` travels with the login stamp so a display name changed in Identity Platform
    # reaches `users.name`, which every reader of this column - the organisation page's member
    # list above all - would otherwise show as it was at provisioning, forever.
    await user_repo.record_login(db, user_id=user.id, at=at,
                                 email_verified=token.email_verified, name=token.name)

    organization_id = await membership_repo.organization_id_for_email(db, token.email)
    if organization_id is None and token.email_verified and polcfg.CONSOLE_SIGNUP_ENABLED:
        # 4. THE PROVISIONING STEP, reached on the first sign-in whose token proves the address -
        # usually the console re-minting its session after the verification link is clicked.
        # BEFORE THIS, AN ACCOUNT IS WORTH NOTHING: no organisation means no ledger, no grant and
        # no admission (application/spend_gate.py refuses an organisation-less account). That is
        # what turns "create Identity Platform accounts in a loop" from a credit tap into a list
        # of inert rows.
        organization_id = await _provision_organization(db, user, token, events)
        provisioned = organization_id is not None
    return Account(
        user_id=str(user.id),
        owner_id=token.email,
        organization_id=str(organization_id) if organization_id else "",
        email_verified=token.email_verified,
        name=token.name or user.name or token.email,
        provisioned=provisioned,
        audit_events=tuple(events),
    )


async def _link(db: AsyncSession, existing, token: VerifiedToken, events: list | None = None):
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
        # A SQUATTED ADDRESS. Somebody registered this address without ever verifying it, and no
        # organisation was created for it (step 4 never ran). A token that DOES prove the mailbox
        # is better evidence than one that never did, so the row moves to it. Anything verified,
        # or anything that already owns a tenant, is still refused below.
        if (token.email_verified and existing.email_verified_at is None
                and await membership_repo.organization_id_for_email(db, existing.email) is None
                and await user_repo.repoint_unverified(db, user_id=existing.id,
                                                       from_uid=existing.firebase_uid,
                                                       to_uid=token.uid)):
            logger.info("sign-in: an unverified, unprovisioned registration was replaced by a "
                        "verified one for the same address")
            if events is not None:
                events.append(("account.unverified_registration_replaced",
                               {"actor": str(existing.id)}))
            return await user_repo.get_by_firebase_uid(db, token.uid)
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


async def _provision_organization(db: AsyncSession, user, token: VerifiedToken,
                                  events: list | None = None):
    # ONE TRANSACTION, three writes and the grant. The session this runs in is committed by the
    # caller's `get_db()` context, so a failure anywhere here leaves no organisation without its
    # owner and no owner half-provisioned. That atomicity is the whole reason the grant is issued
    # here rather than by a later, separately-failing step.
    #
    # THE ROW LOCK serialises two concurrent verified sign-ins for the same person - two tabs
    # re-minting at once. Both saw "no membership"; the second waits here, then sees the first's.
    await user_repo.lock(db, user.id)
    existing = await membership_repo.organization_id_for_email(db, token.email)
    if existing is not None:
        return existing

    # The NAME was typed at sign-up and held on the users row (design decision 6: caller-supplied
    # names are honoured on the provisioning path alone). Blank falls back to the address.
    name = (user.pending_organization_name or "").strip() or token.email
    organization = await organization_repo.create(db, name=name, slug=_slug_for(token.uid))
    await membership_repo.create(db, user_id=user.id, organization_id=organization.id,
                                 role=MembershipRole.owner)
    granted = await credit_service.grant_signup_credits(db, organization_id=organization.id,
                                                        email=token.email, events=events)
    await user_repo.clear_pending_organization_name(db, user_id=user.id)
    logger.info("provisioned organisation %s for a verified account, granted %s credits",
                organization.id, granted)
    # NOT audited here: this still sits inside the caller's uncommitted transaction, and a line
    # describing a rolled-back provisioning would record something that never happened. The
    # committed fact reaches the trail through api/auth.py's auth.session line (provisioned=true).
    return organization.id


#: `organizations.slug` is String(64) with a unique index. "org-" + 40 + "-" + 8 = 53.
_SLUG_UID_CHARS = 40
_SLUG_DIGEST_CHARS = 8


def _slug_for(firebase_uid: str) -> str:
    # Derived from the uid rather than the email: an address contains characters a slug should
    # not, and two people at the same domain must not collide. The uid is already unique and
    # already URL-safe.
    #
    # THE DIGEST IS WHAT MAKES THIS INJECTIVE. Lowercasing and truncating alone are not: two
    # distinct uids that differ only in case, or only after the 40th character, produced the SAME
    # slug and so a unique-index conflict between two different people that surfaced as an opaque
    # 500. Hashing the EXACT uid means a slug collision implies the same uid - and the same uid is
    # serialised on its users row lock in `_provision_organization`.
    digest = hashlib.sha256(firebase_uid.encode("utf-8")).hexdigest()[:_SLUG_DIGEST_CHARS]
    return f"org-{firebase_uid.lower()[:_SLUG_UID_CHARS]}-{digest}"


@dataclass(frozen=True)
class Member:
    """One person who acts within an organisation, already resolved for display.

    Normalised HERE rather than in the route because the degraded view below has no `User` row to
    render and must still produce a member. A route that received `(User, MembershipRole)` tuples
    would have to invent one.
    """
    email: str
    name: str
    role: str


@dataclass(frozen=True)
class OrganizationView:
    #: the `organizations` row, or None when there is nothing to show - see `organization_view`.
    organization: Organization | None
    members: list[Member]


async def organization_view(db: AsyncSession, *, owner_id: str,
                            organization_id: str) -> OrganizationView:
    """What the console shows the caller about the organisation they act within.

    DECISION 12. An absent or malformed organisation is a deployment between 0004 and the image
    that fills the column, or a lookup that could not run - the same states `credit_service`
    answers 0 for. Answering with an empty member list would read to the person whose account it
    is as though their account had vanished, so the caller is shown themselves: the honest
    degraded view, and exactly what `tenant_scope`'s owner fallback means everywhere else.

    AN ORGANISATION ID THAT PARSES BUT NAMES SOMEBODY ELSE'S ORGANISATION TAKES THE SAME DEGRADED
    VIEW, and the membership check below is what makes that true rather than merely stated. The
    id reaches this function from `org_dep`, which derives it server-side today - but "no caller
    can currently pass a foreign id" is a property of a dependency somewhere else, and a service
    that would hand over another tenant's name, slug and full member roster the moment that
    changes is one refactor away from a disclosure. The caller is shown themselves; nothing about
    the other tenant is read, let alone materialised.
    """
    parsed = tenant_scope.parsed_organization_id(organization_id)
    own = await membership_repo.organization_id_for_email(db, owner_id) if parsed else None
    if parsed is None or own != parsed:
        return OrganizationView(organization=None,
                                members=[Member(email=owner_id, name=owner_id, role="owner")])

    row = await organization_repo.get_by_id(db, parsed)
    members = await membership_repo.list_members(db, organization_id=parsed)
    return OrganizationView(
        organization=row,
        members=[Member(email=user.email, name=user.name or user.email,
                        role=getattr(role, "value", role)) for user, role in members])


async def organization_id_for_owner(db: AsyncSession, owner_id: str) -> str:
    # THE SEAM the signed-header credential resolves its tenant through, and the only place that
    # journey is written. A cache belongs here and nowhere else.
    if not owner_id:
        return ""
    organization_id = await membership_repo.organization_id_for_email(db, owner_id)
    return str(organization_id) if organization_id else ""
