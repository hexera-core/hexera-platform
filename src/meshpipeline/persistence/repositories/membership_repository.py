# Responsibility: Read and write memberships, and answer which organisation an owner acts within.
# Boundaries: rows only; what that organisation then scopes is the repositories' and routes' question.
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import Membership, MembershipRole, User


class MembershipRepository:

    async def create(self, db: AsyncSession, *, user_id: uuid.UUID,
                     organization_id: uuid.UUID,
                     role: MembershipRole = MembershipRole.member) -> Membership:
        row = Membership(user_id=user_id, organization_id=organization_id, role=role)
        db.add(row)
        await db.flush()
        return row

    async def organization_id_for_email(self, db: AsyncSession,
                                        email: str) -> uuid.UUID | None:
        # THE SEAM the header credential resolves through. owner_id is the lowercased email, so
        # this is the whole journey from "who is calling" to "which tenant". One indexed join;
        # it is deliberately a single function so a cache can go here later without touching a
        # single call site.
        res = await db.execute(
            select(Membership.organization_id)
            .join(User, User.id == Membership.user_id)
            .where(User.email == func.lower(email.strip()))
            .order_by(Membership.created_at.asc())
            .limit(1))
        return res.scalar_one_or_none()
