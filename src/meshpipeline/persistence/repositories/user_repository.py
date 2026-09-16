# Responsibility: Read and write users rows: find one by uid or email, create one, link a uid, record a login.
# Boundaries: rows only - whether a caller may become this user is the application's judgement.
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshpipeline.persistence.models import User


class UserRepository:

    async def get_by_firebase_uid(self, db: AsyncSession, firebase_uid: str) -> User | None:
        res = await db.execute(select(User).where(User.firebase_uid == firebase_uid))
        return res.scalar_one_or_none()

    async def get_by_email(self, db: AsyncSession, email: str) -> User | None:
        # Lowercased in the predicate rather than trusting the caller: the column carries a
        # CHECK that it is already lowercase, so lower() here matches the stored form exactly
        # and a mixed-case address from a token still finds its row.
        res = await db.execute(select(User).where(User.email == func.lower(email.strip())))
        return res.scalar_one_or_none()

    async def create(self, db: AsyncSession, *, email: str, name: str,
                     firebase_uid: str | None) -> User:
        row = User(email=email.strip().lower()[:256], name=name[:256],
                   firebase_uid=(firebase_uid or None))
        db.add(row)
        await db.flush()
        return row

    async def attach_firebase_uid(self, db: AsyncSession, *, user_id: uuid.UUID,
                                  firebase_uid: str) -> bool:
        # `firebase_uid is null` is the whole safety property: this links a BACKFILLED row to the
        # account that just signed up, and must never re-point a row that already names one.
        # A second call reports False rather than silently moving somebody's account.
        res = await db.execute(
            update(User)
            .where(User.id == user_id, User.firebase_uid.is_(None))
            .values(firebase_uid=firebase_uid))
        return bool(res.rowcount)

    async def record_login(self, db: AsyncSession, *, user_id: uuid.UUID, at: datetime,
                           email_verified: bool, name: str = "") -> None:
        """Stamp this sign-in, and take the display name the token now carries.

        THE NAME IS REFRESHED HERE because this is the only write on the sign-in path that a
        returning account reaches. `users.name` was otherwise written once, at provisioning, so
        somebody who changed their display name in Identity Platform kept the old one forever in
        every reader of this column - the organisation page's member list above all.

        One UPDATE, not two, and no read first. Deciding "is it different?" in Python would cost
        a round trip to learn something the row is about to be rewritten for anyway: last_login_at
        changes on every call, so carrying `name` in the same SET list is free, and re-writing an
        identical value is a no-op the database already collapses. A BLANK name is never written -
        a token without the claim must not erase a name the account already has.
        """
        values: dict = {"last_login_at": at}
        display_name = (name or "").strip()[:256]
        if display_name:
            values["name"] = display_name
        if email_verified:
            # WHEN it was FIRST proven. `is null` keeps the original moment: re-stamping it on
            # every sign-in would turn "verified since" into "last seen", which is what
            # last_login_at already is.
            await db.execute(
                update(User)
                .where(User.id == user_id, User.email_verified_at.is_(None))
                .values(email_verified_at=at))
        await db.execute(update(User).where(User.id == user_id).values(**values))
