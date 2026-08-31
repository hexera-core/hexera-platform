# Responsibility: Read and write the api_keys rows: create one, find one by its public prefix, revoke it, record a use.
# Boundaries: rows only - what a key means and whether it is still valid is the application's judgement.
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import ApiKey


class ApiKeyRepository:

    async def create(self, db: AsyncSession, *, owner_id: str, name: str, key_prefix: str,
                     key_hash: str, plan: str = "", organization_id: uuid.UUID | None = None,
                     expires_at: datetime | None = None) -> ApiKey:
        row = ApiKey(owner_id=owner_id, name=name[:128], key_prefix=key_prefix, key_hash=key_hash,
                     plan=plan[:32], organization_id=organization_id, expires_at=expires_at)
        db.add(row)
        await db.flush()
        return row

    async def get_by_prefix(self, db: AsyncSession, key_prefix: str) -> ApiKey | None:
        # The PUBLIC half is the whole predicate. The secret never reaches SQL, so it cannot end up
        # in a query log, a slow-query report or a database audit trail.
        res = await db.execute(select(ApiKey).where(ApiKey.key_prefix == key_prefix))
        return res.scalar_one_or_none()

    async def list_for_owner(self, db: AsyncSession, owner_id: str) -> list[ApiKey]:
        res = await db.execute(
            select(ApiKey).where(ApiKey.owner_id == owner_id)
            .order_by(ApiKey.created_at.desc(), ApiKey.id.desc()))
        return list(res.scalars().all())

    async def revoke(self, db: AsyncSession, *, owner_id: str, key_id: uuid.UUID,
                     at: datetime) -> bool:
        # Owner-scoped in SQL, like every other tenant-owned row: another tenant's key id is
        # indistinguishable from one that does not exist. `revoked_at is null` makes a repeat
        # revocation report False rather than rewriting the moment it happened.
        res = await db.execute(
            update(ApiKey)
            .where(ApiKey.id == key_id, ApiKey.owner_id == owner_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=at))
        return bool(res.rowcount)

    async def mark_used(self, db: AsyncSession, *, key_id: uuid.UUID, at: datetime) -> None:
        # Unconditional by design: this column records staleness, not order. A concurrent request
        # writing a moment either side of this one leaves the same answer to the only question the
        # column is asked - "is this key still in use?".
        await db.execute(update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=at))
