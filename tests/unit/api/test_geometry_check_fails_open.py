# Responsibility: Verify the geometry check is served through the time box - a scout whose worker
# died reads as failed with a reason and a way on, a naming that never answered too, a scouted
# check with no naming asked is served as it is - and that the retry route runs the right step
# again and the confirm waits for the file's unit.
# Boundaries: the routes with the session, the store and the queue seam stood in for; no
# database, no worker.
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts import geometry_check as seam
from meshpipeline.contracts import object_storage

pytestmark = pytest.mark.asyncio

SID = uuid.UUID("aaaa1111-2222-4222-b222-aaaaaaaaaaaa")
SOURCE = {"source_id": "s1", "owner_id": "alice", "object_key": "uploads/s1/part.stl", "sha256": "0" * 64,
          "size_bytes": 10, "original_filename": "part.stl", "suffix_hint": ".stl"}


class _Store:
    def __init__(self, objects: dict):
        self.objects = dict(objects)
        self.deleted: list[str] = []

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = local_path.read_bytes()

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)
        self.deleted.append(object_key)

    def create_download_url(self, *, object_key, expires_in):
        return f"https://pictures/{object_key}"


def _key(name: str) -> str:
    return f"sessions/{SID}/geometry_check/{name}"


def _scouted() -> dict:
    return {"status": "scouted", "named": False, "written_at": 1.0, "facts": {"size_mm": [1, 2, 3]},
            "proposal": {"openings": []}, "snapshots": [], "skin_key": _key("skin.json")}


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    session = SimpleNamespace(id=SID, job_id=None, geometry_source_id=uuid.uuid4(),
                              geometry_interpretation_id=None, messages=[{"role": "user", "content": "a pipe"}])

    async def _owned(session_id, owner_id, organization_id):
        return session
    monkeypatch.setattr(route, "_owned_session", _owned)

    @asynccontextmanager
    async def _db():
        yield None
    from meshpipeline.persistence import session as dbmod
    monkeypatch.setattr(dbmod, "get_db", _db)

    async def _interp(db, sess, owner_id, organization_id):
        return None
    monkeypatch.setattr(gh, "interpretation_payload", _interp)

    async def _source(db, sess, owner_id, organization_id):
        return SOURCE
    monkeypatch.setattr(route, "_source_payload", _source)

    def _with(objects: dict, **session_over):
        for k, v in session_over.items():
            setattr(session, k, v)
        store = _Store({_key(n): json.dumps(v).encode() for n, v in objects.items()})
        monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
        return store
    return _with


async def test_a_scout_whose_worker_died_reads_as_failed_with_a_reason_and_a_way_on(served):
    served({"scout.json": {"status": "pending", "session_id": str(SID),
                           "written_at": time.time() - gc.SCOUT_HARD_LIMIT_S - gc.STALE_MARGIN_S - 5}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "failed" and d["reason"] == gc.SCOUT_TOO_LONG and d["retry"] == "scout"
    assert d["naming_requested"] is False and d["unit_needed"] is True and d["named"] is False


async def test_a_fresh_pending_check_is_still_pending(served):
    served({"scout.json": {"status": "pending", "session_id": str(SID), "written_at": time.time()}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "pending" and "retry" not in d


async def test_a_scouted_check_with_no_naming_asked_is_served_for_the_stage_to_open(served):
    served({"scout.json": _scouted()}, geometry_interpretation_id=uuid.uuid4())
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "scouted" and d["named"] is False and d["naming_requested"] is False
    assert d["skin"] is True and d["unit_needed"] is False and "facts" not in d
    assert d["proposal"] == {"openings": []}


async def test_a_naming_that_never_answered_reads_as_failed_and_keeps_the_part_for_the_stage(served):
    served({"scout.json": _scouted(),
            "naming.json": {"requested_at": time.time() - gc.NAMING_HARD_LIMIT_S - gc.STALE_MARGIN_S - 5,
                            "purpose_text": "a pipe"}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "failed" and d["reason"] == gc.NAMING_TOO_LONG and d["retry"] == "naming"
    assert d["skin"] is True and d["proposal"] == {"openings": []} and d["naming_requested"] is True


async def test_a_naming_that_gave_up_waiting_for_the_scout_is_not_reported_as_running(served):
    served({"scout.json": {"status": "pending", "written_at": time.time()},
            "naming.json": {"requested_at": 1.0, "withdrawn": True}})
    d = await route.get_check(SID, "alice", "org-1")
    assert d["status"] == "pending" and d["naming_requested"] is False


async def test_the_retry_of_a_dead_scout_queues_the_scout_again_and_forgets_the_old_naming(served):
    store = served({"scout.json": {"status": "pending", "written_at": time.time() - 5000},
                    "naming.json": {"requested_at": 1.0, "purpose_text": "old words"}})
    queued: list = []
    seam.set_scout_enqueuer(lambda **kw: queued.append(kw))
    try:
        d = await route.retry_check(SID, None, "alice", "org-1")
    finally:
        seam.set_scout_enqueuer(None)
    assert queued and queued[0]["source"] == SOURCE and queued[0]["session_id"] == str(SID)
    assert d["status"] == "pending" and d["naming_requested"] is False
    assert _key("naming.json") in store.deleted
    assert json.loads(store.objects[_key("scout.json")])["written_at"] > time.time() - 60   # the box starts again


async def test_the_retry_of_a_naming_that_never_answered_runs_it_again_over_the_scouts_facts(served):
    store = served({"scout.json": _scouted(),
                    "naming.json": {"requested_at": time.time() - 5000, "purpose_text": "a pipe"}},
                   geometry_interpretation_id=uuid.uuid4())
    queued: list = []
    seam.set_naming_enqueuer(lambda **kw: queued.append(kw))
    try:
        d = await route.retry_check(SID, None, "alice", "org-1")
    finally:
        seam.set_naming_enqueuer(None)
    assert queued and queued[0]["purpose_text"] == "a pipe"
    assert d["status"] == "scouted" and d["naming_requested"] is True and d["skin"] is True
    assert json.loads(store.objects[_key("naming.json")])["requested_at"] > time.time() - 60


async def test_a_ready_check_has_nothing_to_run_again(served):
    served({"scout.json": {"status": "ready", "named": True, "facts": {}, "proposal": {}, "snapshots": []}})
    with pytest.raises(HTTPException) as refused:
        await route.retry_check(SID, None, "alice", "org-1")
    assert refused.value.status_code == 409


async def test_the_confirm_waits_for_the_files_unit(served):
    """The numbers on the stage are the file's own read as millimetres until the chat says
    otherwise: a confirmation in a unit nobody has named would bind the ports at the wrong scale."""
    served({"scout.json": _scouted()})
    body = route.ConfirmIn(input_kind="body-surface", flow="internal", openings=[])
    with pytest.raises(HTTPException) as refused:
        await route.confirm_check(SID, body, "alice", "org-1")
    assert refused.value.status_code == 409 and "unit" in refused.value.detail
    assert route.unit_needed(SimpleNamespace(geometry_source_id=uuid.uuid4(), geometry_interpretation_id=uuid.uuid4())) is False
    assert route.unit_needed(SimpleNamespace(geometry_source_id=None, geometry_interpretation_id=None)) is False
