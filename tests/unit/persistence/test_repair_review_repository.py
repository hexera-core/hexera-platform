# Responsibility: Verify the repair-review repository's statements keep the park-closed and paired-item rules in their own SQL.
# Boundaries: the SQL each method issues, read off a recording session; no database.
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update

from meshpipeline.persistence.models import RepairReviewItem, RepairReviewPark
from meshpipeline.persistence.repositories.repair_review_repository import (
    ParkClosedError,
    RepairReviewRepository,
)

repo = RepairReviewRepository()


class _Db:
    """Records every statement; `rowcount` answers UPDATEs and `rows` answers SELECTs in order."""

    def __init__(self, rowcount=1, rows=()):
        self.statements: list = []
        self.added: list = []
        self.rowcount = rowcount
        self.rows = list(rows)

    def add(self, row):
        self.added.append(row)

    def add_all(self, rows):
        self.added.extend(rows)

    async def flush(self):
        return None

    async def execute(self, stmt):
        self.statements.append(stmt)
        if isinstance(stmt, Update):
            return SimpleNamespace(rowcount=self.rowcount)
        row = self.rows.pop(0) if self.rows else None
        return SimpleNamespace(scalar_one_or_none=lambda: row, scalars=lambda: SimpleNamespace(
            first=lambda: row, all=lambda: [row] if row is not None else []))


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(),
                            compile_kwargs={"literal_binds": True}))


def _updates(db):
    return [s for s in db.statements if isinstance(s, Update)]


def _item(**kw):
    base = {"group_key": "g", "defect_code": "open_edge", "severity": "warn",
            "entity_type": "edge", "entity_index": 0, "fingerprint": "fp",
            "measurements": {}, "message": "gap", "origin": "triage",
            "camera": {"centroid_mm": [0, 0, 0], "normal": [0, 0, 1], "extent_mm": 1.0}}
    return {**base, **kw}


async def test_a_park_is_created_open_and_items_are_minted_open_under_it():
    db, job = _Db(), uuid.uuid4()
    park = await repo.create_park(db, job, [{"op": "x"}], "a" * 64, "sess")
    assert (park.status, park.job_id, park.kept_operations) == ("open", job, [{"op": "x"}])
    items = await repo.add_items(db, job, park.id, [_item(), _item(pairing_key="rims:abc")])
    assert [i.state for i in items] == ["open", "open"]
    assert [i.pairing_key for i in items] == ["", "rims:abc"]
    assert all(i.park_id == park.id and i.job_id == job for i in items)


async def test_a_delivered_run_mints_items_without_a_park():
    (item,) = await repo.add_items(_Db(), uuid.uuid4(), None, [_item()])
    assert item.park_id is None


async def test_group_decision_never_touches_a_paired_item():
    db = _Db(rowcount=1)
    n = await repo.record_group_decision(db, uuid.uuid4(), "g", "accepted", {"by": "eng"})
    assert n == 1
    (stmt,) = _updates(db)
    where = _sql(stmt.whereclause)
    assert "pairing_key = ''" in where          # the bolt-hole safety rule, in the statement itself
    assert "state = 'open'" in where
    assert "status = 'open'" in where           # and nothing under a closed park


async def test_decisions_are_conditional_on_the_park_still_being_open():
    item = RepairReviewItem(id=uuid.uuid4(), park_id=uuid.uuid4(), state="open")
    db = _Db(rowcount=1, rows=[item])
    assert await repo.record_decision(db, item.id, "accepted", {"n": 1}) is item
    (stmt,) = _updates(db)
    where = _sql(stmt.whereclause)
    assert "repair_review_parks.status = 'open'" in where    # one UPDATE, no read-then-write gap


async def test_decisions_refuse_when_the_park_closed():
    item = RepairReviewItem(id=uuid.uuid4(), park_id=uuid.uuid4(), state="open")
    with pytest.raises(ParkClosedError):
        await repo.record_decision(_Db(rowcount=0, rows=[item]), item.id, "accepted", {})
    with pytest.raises(LookupError):
        await repo.record_decision(_Db(rowcount=0, rows=[None]), uuid.uuid4(), "accepted", {})
    with pytest.raises(ValueError):
        await repo.record_decision(_Db(), item.id, "open", {})


async def test_closing_a_park_is_a_compare_and_set_on_open():
    db = _Db()
    await repo.close_park(db, uuid.uuid4(), "run")
    (stmt,) = _updates(db)
    assert "status = 'open'" in _sql(stmt.whereclause)
    with pytest.raises(ValueError):
        await repo.close_park(db, uuid.uuid4(), "nobody")


def test_the_park_model_names_what_the_repository_filters_on():
    assert {"status", "closed_by", "closed_at"} <= {c.name for c in RepairReviewPark.__table__.columns}
