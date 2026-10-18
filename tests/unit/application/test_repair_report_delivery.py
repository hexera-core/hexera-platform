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
from meshpipeline.contracts.object_storage import set_object_store
from meshpipeline.persistence.models import ArtifactType
from meshpipeline.persistence.repositories.artifact_repository import DeliveryOutcome

_REPORT = {"status": "repairable", "report": {"summary": "a wire has a gap"}}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, recorder: dict, rows=()):
        self._recorder = recorder
        self._rows = rows

    async def execute(self, _statement):
        # the only read this module makes: which artifact rows already exist for the job
        return _Rows(self._rows)

    async def commit(self):
        self._recorder["committed"] = True


class _Row:
    def __init__(self, storage_key: str):
        self.storage_key = storage_key


def _session_factory(recorder: dict, rows=()):
    @asynccontextmanager
    async def _factory():
        yield _FakeDb(recorder, rows)
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
    # CONTENT-ADDRESSED: the key carries the report's own digest, so a racing writer with
    # different bytes cannot reach the object a committed row points at.
    key = delivered["storage_key"]
    assert key.startswith(f"jobs/{job_id}/repair_report-") and key.endswith(".json")
    stored = json.loads(store.get_bytes(object_key=key))
    assert stored == {"repair_status": "repairable", "report": _REPORT}
    assert delivered["logical_key"] == REPAIR_REPORT_LOGICAL_KEY
    assert delivered["artifact_type"] is ArtifactType.repair_report
    # currency travels with the row, as it does for every other artifact
    assert (delivered["delivery_attempt"], delivered["execution_generation"]) == (2, 3)


def test_the_same_report_delivered_twice_writes_the_same_bytes(store, delivered):
    job_id = str(uuid.uuid4())
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id, report=_REPORT))
    first_key = delivered["storage_key"]
    first = store.get_bytes(object_key=first_key)
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id,
                                      report=dict(reversed(list(_REPORT.items())))))

    # the same content hashes to the same key and the same bytes, so a re-delivery of one attempt
    # is idempotent rather than a conflicting second version of the same evidence
    assert delivered["storage_key"] == first_key
    assert store.get_bytes(object_key=first_key) == first


def test_a_differing_report_cannot_reach_the_committed_object(store, delivered):
    job_id = str(uuid.uuid4())
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id, report=_REPORT))
    committed_key = delivered["storage_key"]

    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id,
                                      report={"status": "clean", "report": {"summary": "other"}}))

    # THE FAILURE THIS PREVENTS: a losing writer overwriting bytes a committed row points at, so
    # a customer downloads a report that does not match the record.
    assert delivered["storage_key"] != committed_key
    assert json.loads(store.get_bytes(object_key=committed_key))["report"] == _REPORT


@pytest.fixture
def reconciled(monkeypatch):
    recorded: list[dict] = []

    async def _record(self, db, **kw):
        recorded.append(kw)

    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.reconciliation_repository."
        "ReconciliationRepository.record_orphan", _record)
    return recorded


def test_a_report_whose_row_never_commits_is_handed_to_the_reconciler(store, monkeypatch,
                                                                      reconciled):
    async def _boom(self, db, **kw):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.artifact_repository."
        "ArtifactRepository.deliver_artifact", _boom)
    job_id = str(uuid.uuid4())

    assert asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id,
                                             report=_REPORT, owner_id="owner-1",
                                             delivery_attempt=2)) == ""

    # not left untracked: recorded for the reconciler, with what this delivery stored
    [record] = reconciled
    key = [k for k in store._objects if job_id in k]
    assert record["object_key"] in key
    assert record["owner_id"] == "owner-1" and record["delivery_attempt"] == 2
    assert record["logical_key"] == REPAIR_REPORT_LOGICAL_KEY
    assert record["artifact_type"] == ArtifactType.repair_report
    # (the double's upload reports no checksum; a real store's is what the generation check reads)
    assert "object_checksum" in record


def test_a_failed_delivery_never_deletes_bytes_a_concurrent_writer_is_committing(
        store, monkeypatch, reconciled):
    """The race: identical content shares one key, so a delivery that fails while another one is
    still committing sees no committed owner - and deleting then removed the bytes the other's
    committed row was about to name. Cleanup is the reconciler's, which adopts on a ready row."""
    async def _boom(self, db, **kw):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.artifact_repository."
        "ArtifactRepository.deliver_artifact", _boom)
    deleted: list[str] = []
    monkeypatch.setattr(store, "delete_object", lambda **kw: deleted.append(kw["object_key"]))
    job_id = str(uuid.uuid4())

    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id, report=_REPORT,
                                      owner_id="owner-1"))

    assert deleted == [], "a delivery deleted a shared content-addressed object on the spot"
    assert [k for k in store._objects if job_id in k]


def test_an_orphan_with_no_owner_to_record_against_is_kept(store, monkeypatch, reconciled):
    async def _boom(self, db, **kw):
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(
        "meshpipeline.persistence.repositories.artifact_repository."
        "ArtifactRepository.deliver_artifact", _boom)
    job_id = str(uuid.uuid4())
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id, report=_REPORT))
    assert reconciled == []
    assert [k for k in store._objects if job_id in k], "kept: a leak beats a wrong delete"


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


# A FAILED RETRY MUST NOT TAKE THE COMMITTED COPY WITH IT


def test_a_retry_that_fails_leaves_the_already_committed_object_alone(store, delivered):
    """The regression: content-addressed keys mean a retry recomputes a COMMITTED key.

    Identical bytes hash to the identical key, so the second delivery of the same report uploads
    over the object an earlier, successful delivery already registered. If that retry's own row
    then fails, treating the object as an orphan deletes the bytes a committed artifact row points
    at - and the customer's download becomes a 404 for a report the record says they have.
    """
    job_id = str(uuid.uuid4())
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id, report=_REPORT))
    committed_key = delivered["storage_key"]
    assert store.get_bytes(object_key=committed_key)

    async def _boom(self, db, **kw):
        raise RuntimeError("database unreachable")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("meshpipeline.persistence.repositories.artifact_repository."
                   "ArtifactRepository.deliver_artifact", _boom)
        assert asyncio.run(deliver_repair_report(
            _session_factory({}, rows=[_Row(committed_key)]), job_id=job_id,
            report=_REPORT)) == ""

    assert store.get_bytes(object_key=committed_key), "deleted a committed customer artifact"


def test_an_unanswerable_ownership_check_keeps_the_object(store, monkeypatch):
    """Unknown is read as "keep". A leaked object costs storage; a wrong delete costs a deliverable."""
    async def _boom(self, db, **kw):
        raise RuntimeError("row refused")

    monkeypatch.setattr("meshpipeline.persistence.repositories.artifact_repository."
                        "ArtifactRepository.get_by_job", _boom)
    monkeypatch.setattr("meshpipeline.persistence.repositories.artifact_repository."
                        "ArtifactRepository.deliver_artifact", _boom)
    job_id = str(uuid.uuid4())

    assert asyncio.run(deliver_repair_report(_session_factory({}), job_id=job_id,
                                             report=_REPORT)) == ""
    assert [k for k in store._objects if job_id in k], "deleted on an unknown answer"


# THE REPAIRED FILE'S OWN DELIVERY IS BEST EFFORT TOO


def test_a_repaired_file_that_cannot_be_hashed_does_not_end_the_run(store, delivered, tmp_path,
                                                                    monkeypatch):
    """A terminal finalise only expects to be fenced, so nothing else here may raise at it."""
    from meshpipeline.application.repair_report_delivery import deliver_repaired_cad

    repaired = tmp_path / "part-repaired.step"
    repaired.write_bytes(b"ISO-10303-21;")

    def _boom(_path):
        raise OSError("file vanished after the check")

    monkeypatch.setattr("meshpipeline.contracts.geometry_source.sha256_of", _boom)

    assert asyncio.run(deliver_repaired_cad(
        _session_factory({}), job_id=str(uuid.uuid4()), local_path=repaired)) == ""
    assert not delivered


def test_the_delivered_report_carries_what_the_repair_did_beside_the_inspection(store, delivered):
    """The downloadable report held only the upload's inspection: the operations, refusals, stop
    reason and remaining defects lived in `repair_attempt` and never reached the customer, who got
    repaired CAD with nothing durable explaining it. The inspection itself is unchanged."""
    attempt = {"attempted": True, "stopped_because": "resolved", "resolved": True,
               "operations": [{"name": "sew_faces", "status": "kept"}],
               "refusals": [{"defect": "edge:r", "reason": "paired_rim"}],
               "defects_remaining": [{"code": "small_face"}]}
    asyncio.run(deliver_repair_report(_session_factory({}), job_id=str(uuid.uuid4()),
                                      report=_REPORT, repair_status="repairable",
                                      repair_attempt=attempt))
    stored = json.loads(store.get_bytes(object_key=delivered["storage_key"]))
    assert stored["report"] == _REPORT
    assert stored["repair_attempt"] == attempt
