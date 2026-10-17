# Responsibility: Verify a park holds its items and closes once, and that a group decision never reaches a paired item.
# Boundaries: real SQL on in-memory sqlite through a thin async adapter; JSONB is rendered as JSON.
#             Only the two review tables are built, from the models, to exercise the repository's
#             statements; the schema authority stays the migration (see the revision tests).
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from meshpipeline.persistence.models import RepairReviewItem, RepairReviewPark
from meshpipeline.persistence.repositories.repair_review_repository import (
    ParkClosedError,
    RepairReviewRepository,
)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(type_, compiler, **kw):
    return "JSON"


class _AsyncSession:
    """The four AsyncSession members the repository uses, over a sync sqlite Session."""

    def __init__(self, session: Session):
        self._s = session

    def add(self, row):
        self._s.add(row)

    def add_all(self, rows):
        self._s.add_all(rows)

    async def flush(self):
        self._s.flush()

    async def execute(self, stmt):
        return self._s.execute(stmt)


@pytest.fixture
def db():
    engine = create_engine(URL.create("sqlite", database=":memory:"), poolclass=StaticPool)
    tables = [RepairReviewPark.__table__, RepairReviewItem.__table__]
    # the FK target (simulation_jobs) is not created: sqlite does not enforce it
    for t in tables:
        t.create(engine)
    with Session(engine) as s:
        yield _AsyncSession(s)


def _item(group="g", pairing="", index=0):
    return {"group_key": group, "defect_code": "open_edge", "severity": "warn",
            "entity_type": "edge", "entity_index": index, "fingerprint": f"fp{index}",
            "measurements": {"gap_mm": 0.1}, "message": "gap", "origin": "triage",
            "pairing_key": pairing, "proposal": None,
            "camera": {"centroid_mm": [0, 0, 0], "normal": [0, 0, 1], "extent_mm": 1.0}}


repo = RepairReviewRepository()


async def test_a_park_holds_its_items_and_closes_once(db):
    job = uuid.uuid4()
    park = await repo.create_park(db, job, [{"op": "x"}], "a" * 64, "sess")
    assert (await repo.open_park(db, job)).id == park.id
    items = await repo.add_items(db, job, park.id, [_item(index=0), _item(index=1)])
    assert [i.state for i in items] == ["open", "open"]
    assert {i.id for i in await repo.items_for(db, job, park.id)} == {i.id for i in items}
    assert await repo.items_for(db, job, None) == []

    await repo.close_park(db, park.id, "run")
    assert await repo.open_park(db, job) is None
    db._s.refresh(park)
    closed_at = park.closed_at
    await repo.close_park(db, park.id, "cancel")       # a second close changes nothing
    db._s.refresh(park)
    assert (park.status, park.closed_by, park.closed_at) == ("closed", "run", closed_at)


async def test_items_without_a_park_belong_to_a_delivered_run(db):
    job = uuid.uuid4()
    await repo.add_items(db, job, None, [_item()])
    assert len(await repo.items_for(db, job, None)) == 1
    assert await repo.open_park(db, job) is None


async def test_group_decision_never_touches_a_paired_item(db):
    job = uuid.uuid4()
    park = await repo.create_park(db, job, [], "a" * 64, "sess")
    plain, paired = await repo.add_items(
        db, job, park.id, [_item(index=0), _item(pairing="rims:abc", index=1)])
    n = await repo.record_group_decision(db, job, "g", "accepted", {"by": "eng"})
    assert n == 1
    db._s.refresh(plain)
    db._s.refresh(paired)
    assert plain.state == "accepted" and plain.decision == {"by": "eng"}
    assert plain.decided_at is not None
    assert paired.state == "open" and paired.decision is None


async def test_decisions_refuse_on_a_closed_park(db):
    job = uuid.uuid4()
    park = await repo.create_park(db, job, [], "a" * 64, "sess")
    (item,) = await repo.add_items(db, job, park.id, [_item()])
    await repo.close_park(db, park.id, "run")
    with pytest.raises(ParkClosedError):
        await repo.record_decision(db, item.id, "accepted", {})
    assert await repo.record_group_decision(db, job, "g", "accepted", {}) == 0
    db._s.refresh(item)
    assert item.state == "open"


async def test_a_decision_on_an_open_park_is_recorded(db):
    job = uuid.uuid4()
    park = await repo.create_park(db, job, [], "a" * 64, "sess")
    (item,) = await repo.add_items(db, job, park.id, [_item(pairing="p")])
    out = await repo.record_decision(db, item.id, "redirected", {"note": "n"})
    assert out.state == "redirected" and out.decision == {"note": "n"}
    with pytest.raises(ValueError):
        await repo.record_decision(db, item.id, "open", {})
