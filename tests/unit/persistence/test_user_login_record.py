# tests/unit/persistence/test_user_login_record.py
# Responsibility: Verify what a recorded sign-in writes, and what it refuses to overwrite.
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from meshpipeline.persistence.repositories.user_repository import UserRepository

pytestmark = pytest.mark.asyncio

USER = uuid.UUID("44444444-4444-4444-4444-444444444444")
AT = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


class _Session:
    """Captures the statements record_login issues without a database.

    The repository uses no result from either execute, so a recorder is enough - and it keeps
    this a unit test rather than one more thing gated on Postgres being up.
    """

    def __init__(self):
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return None


def _set_columns(statement) -> dict:
    # SQLAlchemy wraps a literal in a BindParameter on the way into the SET list; the value the
    # database will actually be sent is the one under it.
    return {column.name: getattr(value, "value", value)
            for column, value in statement._values.items()}


async def test_a_sign_in_carries_the_tokens_name_into_the_same_update():
    # users.name used to be written once, at provisioning, so changing your display name in
    # Identity Platform left the console - the organisation member list above all - showing the
    # name you signed up with forever.
    db = _Session()
    await UserRepository().record_login(db, user_id=USER, at=AT, email_verified=False,
                                        name="Renamed Engineer")

    assert len(db.statements) == 1, "the name must ride along, not cost a second statement"
    columns = _set_columns(db.statements[0])
    assert columns["last_login_at"] == AT
    assert columns["name"] == "Renamed Engineer"


async def test_a_token_with_no_name_does_not_erase_the_one_on_file():
    # A blank claim is an absence of information, not an instruction to forget.
    db = _Session()
    await UserRepository().record_login(db, user_id=USER, at=AT, email_verified=False, name="   ")

    assert "name" not in _set_columns(db.statements[0])


async def test_the_name_is_truncated_to_the_column_it_is_written_to():
    db = _Session()
    await UserRepository().record_login(db, user_id=USER, at=AT, email_verified=False,
                                        name="n" * 400)

    assert len(_set_columns(db.statements[0])["name"]) == 256


async def test_a_verified_sign_in_still_stamps_the_first_proof_separately():
    # The email_verified_at write carries its own `is null` predicate - it records WHEN it was
    # first proven - so it cannot merge into the unconditional update the name rides in.
    db = _Session()
    await UserRepository().record_login(db, user_id=USER, at=AT, email_verified=True, name="A")

    assert len(db.statements) == 2
    assert "email_verified_at" in _set_columns(db.statements[0])
    assert set(_set_columns(db.statements[1])) == {"last_login_at", "name"}
