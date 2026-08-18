# Responsibility: Read and record uploaded geometry sources, and their purge state.
# Boundaries: the row is lineage and is never deleted by retention; only the object is.
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import GeometrySource

# Lowercase hex, 64 chars. Enforced here rather than trusted from the caller: this value is the
# only thing that later proves a downloaded object is the geometry the user approved, so a
# mis-cased or truncated digest must never reach the row.
_SHA256_LEN = 64


class GeometrySourceRepository:

    async def create(self, db: AsyncSession, *, owner_id: str, original_filename: str,
                     suffix_hint: str, object_key: str, sha256: str,
                     size_bytes: int) -> GeometrySource:
        digest = (sha256 or "").strip().lower()
        if len(digest) != _SHA256_LEN or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        if int(size_bytes) <= 0:
            raise ValueError("size_bytes must be positive - an empty upload is not a source")
        if not str(owner_id or "").strip():
            raise ValueError("owner_id is required - a source is always owned")
        if not str(object_key or "").strip():
            raise ValueError("object_key is required")
        row = GeometrySource(
            owner_id=owner_id,
            original_filename=str(original_filename or "")[:512],
            suffix_hint=str(suffix_hint or "").lower()[:16],
            object_key=object_key,
            sha256=digest,
            size_bytes=int(size_bytes),
        )
        db.add(row)
        await db.flush()
        return row

    async def get_internal(self, db: AsyncSession,
                           source_id: uuid.UUID) -> GeometrySource | None:
        result = await db.execute(select(GeometrySource).where(GeometrySource.id == source_id))
        return result.scalar_one_or_none()

    async def get_for_owner(self, db: AsyncSession, source_id: uuid.UUID,
                            owner_id: str) -> GeometrySource | None:
        result = await db.execute(
            select(GeometrySource).where(GeometrySource.id == source_id,
                                         GeometrySource.owner_id == owner_id))
        return result.scalar_one_or_none()
