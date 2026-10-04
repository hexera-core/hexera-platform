# Responsibility: Verify the CAD inspection report becomes durable evidence without ever deciding a run's outcome.
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager

import pytest
from tests.object_store_double import InMemoryObjectStore

from meshpipeline.application.repair_report_delivery import (
    REPAIR_REPORT_LOGICAL_KEY,
    deliver_repair_report,
)
from meshpipeline.artifact_keys import repair_report_key
from meshpipeline.contracts.object_storage import set_object_store
from meshpipeline.persistence.models import ArtifactType
from meshpipeline.persistence.repositories.artifact_repository import DeliveryOutcome

_REPORT = {"status": "repairable", "report": {"summary": "a wire has a gap"}}


class _FakeDb:
    def __init__(self, recorder: dict):
        self._recorder = recorder

    async def commit(self):
        self._recorder["committed"] = True


def _session_factory(recorder: dict):
    @asynccontextmanager
    async def _factory():
        yield _FakeDb(recorder)
    return _factory


@pytest.fixture
def store():
    s = InMemoryObjectStore()
    set_object_store(s)
    yield s
    set_object_store(None)


@pytest.fixture
def delivered(monkeypatch):
    calls: dict = {}

    async def _deliver(self, db, **kw):
        calls.update(kw)
        return DeliveryOutcome.created

    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.artifact_repository."
        "ArtifactRepository.deliver_artifact", _deliver)
    return calls


def test_the_report_is_stored_and_bound_to_the_job(store, delivered):
    job_id = str(uuid.uuid4())
    recorder: dict = {}

    outcome = asyncio.run(deliver_repair_report(
        _session_factory(recorder), job_id=job_id, report=_REPORT,
        repair_status="repairable", delivery_attempt=2, execution_generation=3))

    assert outcome == DeliveryOutcome.created.value
    assert recorder["committed"] is True
    stored = json.loads(store.get_bytes(object_key=repair_report_key(job_id)))
    assert stored == {"repair_status": "repairable", "report": _REPORT}
    assert delivered["logical_key"] == REPAIR_REPORT_LOGICAL_KEY
    assert delivered["artifact_type"] is ArtifactType.repair_report
    assert delivered["storage_key"] == repair_report_key(job_id)
    # currency travels with the row, as it does for every other artifact
    assert (delivered["delivery_attempt"], delivered["execution_generation"]) == (2, 3)


def test_the_same_report_delivered_twice_writes_the_same_bytes(store, delivered):
    job_id = str(uuid.uuid4())
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id, report=_REPORT))
    first = store.get_bytes(object_key=repair_report_key(job_id))
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id,
                                      report=dict(reversed(list(_REPORT.items())))))

    # byte-identical, so the repository's compare-and-set permits a re-delivery of one attempt
    # instead of calling it a conflicting second version of the same evidence
    assert store.get_bytes(object_key=repair_report_key(job_id)) == first


def test_no_report_means_nothing_is_stored(store, delivered):
    assert asyncio.run(deliver_repair_report(
        _session_factory({}), job_id=str(uuid.uuid4()), report={})) == ""
    assert not delivered


def test_a_store_that_refuses_the_evidence_never_fails_the_run(monkeypatch, store, delivered):
    def _boom(**kw):
        raise RuntimeError("store unreachable")
    monkeypatch.setattr(store, "upload_file", _boom)

    # "" and no exception: a mesh the user can otherwise download is not withheld because an
    # evidence file would not store, and their geometry is not accused of anything either.
    assert asyncio.run(deliver_repair_report(
        _session_factory({}), job_id=str(uuid.uuid4()), report=_REPORT)) == ""
    assert not delivered


def test_a_refused_row_never_fails_the_run(monkeypatch, store):
    async def _boom(self, db, **kw):
        raise RuntimeError("database unreachable")
    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.artifact_repository."
        "ArtifactRepository.deliver_artifact", _boom)

    assert asyncio.run(deliver_repair_report(
        _session_factory({}), job_id=str(uuid.uuid4()), report=_REPORT)) == ""


def test_a_fenced_worker_is_refused_rather_than_swallowed(store, delivered):
    from meshpipeline.contracts.execution_guard import StaleWorkerFenced

    async def _fence(db):
        raise StaleWorkerFenced("repair report registration")

    # A worker that lost its lease must produce NOTHING observable, so being fenced is the one
    # failure this module re-raises: the caller's whole response is to stop.
    with pytest.raises(StaleWorkerFenced):
        asyncio.run(deliver_repair_report(
            _session_factory({}), job_id=str(uuid.uuid4()), report=_REPORT,
            fence_commit=_fence))


def test_the_report_leaves_no_temporary_file_behind(store, delivered, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=str(uuid.uuid4()),
                                      report=_REPORT))
    assert list(tmp_path.glob("repair_report_*")) == []
