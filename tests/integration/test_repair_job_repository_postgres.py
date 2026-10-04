# Responsibility: Verify repair jobs are owner-isolated and that an illegal or losing status change is refused by the database.
# Boundaries: an ordinary member of the integration tier - the tier's own conftest builds the whole
# schema and this file uses the `db` session it already provides, isolating only its own two tables.
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import delete

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

from meshpipeline.persistence.job_state import TransitionResult
from meshpipeline.persistence.models import (
    CadRepairAttempt,
    CadRepairJob,
    GeometrySource,
    Organization,
)
from meshpipeline.persistence.models import (
    RepairJobStatus as S,
)
from meshpipeline.persistence.repositories.repair_job_repository import RepairJobRepository

_A = "owner-alpha"
_B = "owner-bravo"
_SHA_IN = "a" * 64
_SHA_OUT = "b" * 64


@pytest.fixture(autouse=True)
async def _clean(db):
    # Child-first DELETE, never a TRUNCATE ... CASCADE: this session's schema is shared with every
    # other integration suite, and a cascade off geometry_sources or organizations would take
    # their rows with it.
    for model in (CadRepairAttempt, CadRepairJob):
        await db.execute(delete(model))
    await db.commit()
    yield
    for model in (CadRepairAttempt, CadRepairJob):
        await db.execute(delete(model))
    await db.commit()


async def _source(db, owner_id: str) -> GeometrySource:
    row = GeometrySource(owner_id=owner_id, original_filename="part.step", suffix_hint=".step",
                         object_key=f"sources/{uuid.uuid4()}/part.step",
                         sha256=uuid.uuid4().hex + uuid.uuid4().hex, size_bytes=4096)
    db.add(row)
    await db.flush()
    return row


async def _job(db, owner_id: str, **kw) -> CadRepairJob:
    src = await _source(db, owner_id)
    job = await RepairJobRepository().create(
        db, owner_id=owner_id, geometry_source_id=src.id, target_engine="snappy", **kw)
    await db.commit()
    return job


# OWNER ISOLATION


async def test_another_owner_cannot_read_a_repair_job(db):
    job = await _job(db, _A)
    repo = RepairJobRepository()

    assert (await repo.get_for_owner(db, job.id, _A)) is not None
    # absent, not forbidden: a foreign reader learns nothing about whether the row exists
    assert (await repo.get_for_owner(db, job.id, _B)) is None


async def test_another_owner_sees_none_of_the_queue(db):
    await _job(db, _A)
    await _job(db, _A)
    repo = RepairJobRepository()

    assert len(await repo.queue_for_owner(db, _A)) == 2
    assert await repo.queue_for_owner(db, _B) == []


async def test_an_organisation_scopes_over_the_owner(db):
    org = Organization(name="acme")
    db.add(org)
    await db.flush()
    src = await _source(db, _A)
    repo = RepairJobRepository()
    job = await repo.create(db, owner_id=_A, geometry_source_id=src.id,
                            organization_id=str(org.id))
    await db.commit()

    # a colleague in the same organisation reads it; the owner_id alone no longer decides
    assert (await repo.get_for_owner(db, job.id, _B, organization_id=str(org.id))) is not None
    assert (await repo.get_for_owner(db, job.id, _B)) is None
    # and a malformed organisation id narrows to the owner rather than widening access
    assert (await repo.get_for_owner(db, job.id, _B, organization_id="not-a-uuid")) is None


async def test_the_queue_is_ordered_by_urgency_then_age(db):
    repo = RepairJobRepository()
    low = await _job(db, _A, service_priority=200)
    high = await _job(db, _A, service_priority=10)
    mid = await _job(db, _A, service_priority=100)

    ordered = [j.id for j in await repo.queue_for_owner(db, _A)]
    assert ordered == [high.id, mid.id, low.id]


async def test_the_queue_narrows_to_the_states_asked_for(db):
    repo = RepairJobRepository()
    waiting = await _job(db, _A)
    other = await _job(db, _A)
    assert await repo.transition(db, other.id, S.inspecting) == TransitionResult.applied
    await db.commit()

    found = await repo.queue_for_owner(db, _A, statuses=(S.received,))
    assert [j.id for j in found] == [waiting.id]


# STATUS TRANSITIONS


async def test_a_legal_transition_applies_once_and_is_then_idempotent(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)

    assert await repo.transition(db, job.id, S.inspecting) == TransitionResult.applied
    await db.commit()
    assert await repo.transition(db, job.id, S.inspecting) == TransitionResult.already_at_target


async def test_an_illegal_transition_is_refused_and_changes_nothing(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)

    # received -> delivered is not a route: nothing was inspected, repaired, meshed or reviewed
    assert await repo.transition(db, job.id, S.delivered) == TransitionResult.rejected_current_state
    await db.commit()
    assert (await repo.get_internal(db, job.id)).status is S.received


async def test_a_delivered_job_cannot_be_moved_back_into_the_pipeline(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)
    for target in (S.inspecting, S.awaiting_strategy, S.meshing, S.mesh_review, S.delivered):
        assert await repo.transition(db, job.id, target) == TransitionResult.applied
    await db.commit()

    for target in (S.meshing, S.repairing, S.awaiting_strategy, S.cancelled):
        assert await repo.transition(db, job.id, target) == TransitionResult.rejected_current_state
    # the customer's right of reply is the one way out
    assert await repo.transition(db, job.id, S.delivery_disputed) == TransitionResult.applied
    await db.commit()


async def test_only_one_of_two_racing_transitions_wins(db):
    # TWO OPERATORS ON ONE QUEUE ITEM is the ordinary case here, so the database decides - a
    # check-then-act would let the later writer overwrite a decision it never saw. Each claim gets
    # its OWN session, because two writers sharing one transaction would never contend.
    from meshpipeline.persistence.session import get_db

    repo = RepairJobRepository()
    job = await _job(db, _A)
    await repo.transition(db, job.id, S.inspecting)
    await repo.transition(db, job.id, S.awaiting_strategy)
    await db.commit()
    job_id = job.id

    async def _claim(target):
        async with get_db() as s:
            out = await repo.transition(s, job_id, target)
            await s.commit()
            return out

    results = await asyncio.gather(_claim(S.repairing), _claim(S.meshing),
                                   return_exceptions=True)
    outcomes = [r for r in results if isinstance(r, TransitionResult)]
    assert sum(o == TransitionResult.applied for o in outcomes) == 1, (
        f"exactly one writer may win: {results}")
    # and the loser is told it was too late, not that the job vanished
    assert TransitionResult.not_found not in outcomes


async def test_a_transition_only_writes_the_fields_it_was_given(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)
    await repo.transition(db, job.id, S.inspecting, repair_status="repairable")
    await repo.transition(db, job.id, S.awaiting_strategy, current_strategy="conservative")
    await db.commit()

    row = await repo.get_internal(db, job.id)
    # the strategy transition said nothing about repair_status, so it must still be there
    assert (row.repair_status, row.current_strategy) == ("repairable", "conservative")


async def test_a_missing_job_is_reported_as_missing_rather_than_refused(db):
    assert await RepairJobRepository().transition(
        db, uuid.uuid4(), S.inspecting) == TransitionResult.not_found


# ATTEMPT HISTORY


async def test_attempts_are_numbered_in_order_and_append_only(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)

    first = await repo.record_attempt(db, repair_job_id=job.id, mode="inspect",
                                      input_sha256=_SHA_IN, status="repairable",
                                      report={"summary": "a wire has a gap"})
    second = await repo.record_attempt(db, repair_job_id=job.id, mode="repair",
                                       input_sha256=_SHA_IN, output_sha256=_SHA_OUT,
                                       profile="conservative", tool_version="OCCT 7.8",
                                       status="repaired")
    await db.commit()

    assert (first.attempt_no, second.attempt_no) == (1, 2)
    history = await repo.attempts_for_job(db, job.id)
    assert [a.attempt_no for a in history] == [1, 2]
    # THE AUDIT PAIR: an inspection produced no geometry, a repair produced new bytes, and in
    # neither case did the input digest change
    assert history[0].output_sha256 is None
    assert history[1].output_sha256 == _SHA_OUT
    assert {a.input_sha256 for a in history} == {_SHA_IN}


async def test_an_attempt_cannot_claim_a_number_another_already_holds(db):
    from sqlalchemy.exc import IntegrityError

    repo = RepairJobRepository()
    job = await _job(db, _A)
    await repo.record_attempt(db, repair_job_id=job.id, mode="inspect", input_sha256=_SHA_IN)
    await db.commit()

    db.add(CadRepairAttempt(repair_job_id=job.id, attempt_no=1, mode="repair",
                            input_sha256=_SHA_IN))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_a_malformed_digest_never_reaches_the_row(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)

    for bad in ("", "abc", _SHA_IN.upper(), "z" * 64):
        with pytest.raises(ValueError, match="input_sha256"):
            await repo.record_attempt(db, repair_job_id=job.id, mode="inspect", input_sha256=bad)
    await db.rollback()


async def test_deleting_a_repair_job_takes_its_attempts_with_it(db):
    repo = RepairJobRepository()
    job = await _job(db, _A)
    await repo.record_attempt(db, repair_job_id=job.id, mode="inspect", input_sha256=_SHA_IN)
    await db.commit()

    await db.execute(delete(CadRepairJob).where(CadRepairJob.id == job.id))
    await db.commit()
    assert await repo.attempts_for_job(db, job.id) == []


async def test_the_customer_upload_cannot_be_deleted_from_under_a_job(db):
    from sqlalchemy.exc import IntegrityError

    job = await _job(db, _A)
    row = await RepairJobRepository().get_internal(db, job.id)

    # RESTRICT: the repair job is ABOUT those bytes, so their identity outlives it
    await db.execute(delete(GeometrySource).where(GeometrySource.id == row.geometry_source_id))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
