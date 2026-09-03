# Responsibility: Issue, revoke and verify the API keys that let a programmatic caller act as an owner.
# Owns: what a presented key means and when it stops meaning it; the row itself belongs to the repository.
# Boundaries: it decides identity, never authorisation - what that identity may then do is the caller's question.
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.contracts import api_key
from meshpipeline.contracts.identity import Credential, Principal
from meshpipeline.persistence.repositories.api_key_repository import ApiKeyRepository

logger = logging.getLogger(__name__)

api_key_repo = ApiKeyRepository()

#: How coarse `last_used_at` is. Every authenticated request would otherwise UPDATE the hottest row
#: in the table to answer a question ("is this key still in use?") that a minute's precision
#: already answers.
LAST_USED_RESOLUTION_SECONDS = 60


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    owner_id: str
    name: str
    plan: str
    key_prefix: str
    #: THE ONLY TIME the secret exists outside the holder's hands. Never logged, never stored,
    #: never recoverable: a lost key is replaced, not looked up.
    secret: str
    presented: str
    expires_at: datetime | None


async def issue(db: AsyncSession, *, owner_id: str, name: str = "", plan: str = "",
                organization_id: uuid.UUID | None = None,
                expires_at: datetime | None = None) -> IssuedKey:
    minted = api_key.mint()
    row = await api_key_repo.create(
        db, owner_id=owner_id, name=name, key_prefix=minted.key_prefix,
        key_hash=minted.key_hash, plan=plan, organization_id=organization_id,
        expires_at=expires_at)
    return IssuedKey(key_id=str(row.id), owner_id=owner_id, name=name, plan=plan,
                     key_prefix=minted.key_prefix, secret=minted.secret,
                     presented=minted.presented, expires_at=expires_at)


async def revoke(db: AsyncSession, *, owner_id: str, key_id: uuid.UUID,
                 now: datetime | None = None) -> bool:
    return await api_key_repo.revoke(db, owner_id=owner_id, key_id=key_id,
                                     at=now or _now())


async def authenticate(db: AsyncSession, presented: str | None, *,
                       now: datetime | None = None) -> Principal | None:
    # One answer for every refusal: None. The caller returns the same 401 whether the key was
    # malformed, unknown, forged, revoked or expired - the difference is only useful to someone
    # probing which of their guesses is close.
    parsed = api_key.parse(presented)
    if parsed is None:
        return None

    row = await api_key_repo.get_by_prefix(db, parsed.key_prefix)
    if row is None:
        # Compare against a hash of a secret nobody holds, so an unknown prefix costs what a known
        # one costs. Skipping this makes response time an enumeration oracle for valid prefixes.
        api_key.secret_matches(parsed.secret, api_key.DECOY_HASH)
        return None

    # The secret is checked BEFORE the row's state is trusted: a caller who does not hold the key
    # learns nothing about it, not even that the prefix names a revoked one.
    if not api_key.secret_matches(parsed.secret, row.key_hash):
        return None

    at = now or _now()
    if row.revoked_at is not None:
        return None
    if row.expires_at is not None and _utc(row.expires_at) <= at:
        return None

    await _record_use(db, row, at)
    return Principal(
        owner_id=row.owner_id,
        organization_id=str(row.organization_id) if row.organization_id else "",
        plan=row.plan or "",
        credential=Credential.api_key,
        key_id=str(row.id),
    )


async def _record_use(db: AsyncSession, row, at: datetime) -> None:
    last = _utc(row.last_used_at) if row.last_used_at is not None else None
    if last is not None and at - last < timedelta(seconds=LAST_USED_RESOLUTION_SECONDS):
        return
    try:
        await api_key_repo.mark_used(db, key_id=row.id, at=at)
    except Exception as exc:
        # Staleness bookkeeping never decides whether a proven caller is served.
        logger.warning("api key %s: could not record use (%s)", row.key_prefix, exc)


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    # A driver, fixture or export that hands back a naive value is read as UTC rather than being
    # compared against an aware one - which raises, and an expiry check that raises is an expiry
    # check that does not happen.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
