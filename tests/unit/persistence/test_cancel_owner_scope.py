# Responsibility: Verify the cancel lock matches the job's owner as well as its tenant, so one member cannot stop another member's run.
from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from meshpipeline.persistence.repositories.job_repository import JobRepository

OWNER = "owner@example.com"


class _Result:
    def scalar_one_or_none(self):
        return None


class _CapturingDB:
    def __init__(self): self.statements = []

    async def execute(self, stmt, *a, **k):
        self.statements.append(stmt)
        return _Result()


async def _compiled(organization_id: str):
    db = _CapturingDB()
    await JobRepository().lock_for_owner(db, uuid.uuid4(), OWNER, organization_id=organization_id)
    (stmt,) = db.statements
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), compiled.params


async def test_inside_an_organisation_the_lock_still_requires_the_owner():
    org = uuid.uuid4()
    sql, params = await _compiled(str(org))
    where = sql.split("WHERE", 1)[1]
    # the owner AND the tenant - the tenant alone is every member of the organisation
    assert "simulation_jobs.owner_id =" in where, "a colleague in the same organisation could cancel"
    assert "simulation_jobs.organization_id =" in where, "the tenant predicate was dropped"
    assert OWNER in params.values() and org in params.values()
    assert "FOR UPDATE" in sql, "the cancel no longer holds the row it decides on"


async def test_without_an_organisation_the_lock_is_the_owner_alone():
    sql, params = await _compiled("")
    where = sql.split("WHERE", 1)[1]
    assert "simulation_jobs.owner_id =" in where
    assert "organization_id" not in where
    assert list(params.values()).count(OWNER) >= 1


async def test_the_read_scope_is_unchanged_for_members():
    # SEEING a colleague's run is still the organisation's; only CHANGING it is narrowed
    db = _CapturingDB()
    org = uuid.uuid4()
    await JobRepository().get_for_owner(db, uuid.uuid4(), OWNER, organization_id=str(org))
    where = str(db.statements[0].compile(dialect=postgresql.dialect())).split("WHERE", 1)[1]
    assert "simulation_jobs.organization_id =" in where
    assert "simulation_jobs.owner_id =" not in where
