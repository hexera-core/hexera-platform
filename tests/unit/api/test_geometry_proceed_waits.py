# Responsibility: Verify that Proceed waits for the geometry check at the route: while the check is
# at work (queued, a scout step, a preview still being measured, the naming - an FEA part's walls,
# hot spots and joints) the served check says `checking` and a confirm is refused with the plain
# reason; once the check is done, failed or timed out, waiting on the user, or a preview kept, the
# confirm goes through - never a dead end.
# Boundaries: the route with the session, the store and the chat turn stood in for; no database.
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application import geometry_progress as gp
from meshpipeline.contracts import object_storage

SID = uuid.UUID("abcd1111-2222-4222-b222-eeeeeeeeee01")
FACTS = {"size_mm": [200.0, 100.0, 5.0], "scale_to_m": 0.001, "solids": 1, "unit_assumed": False,
         "validity": {"clean": True, "kind": "brep", "summary": "The geometry is clean."}}


class _Store:
    def __init__(self, objects: dict):
        self.objects = {f"sessions/{SID}/geometry_check/{k}": json.dumps(v).encode() for k, v in objects.items()}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = local_path.read_bytes()

    def delete_object(self, *, object_key):
        self.objects.pop(object_key, None)

    def create_download_url(self, *, object_key, expires_in):
        return "https://example/" + object_key


@pytest.fixture
def stage(monkeypatch):
    """The routes with a session whose user said `words`, a file declared in millimetres, and the
    stored check objects given (scout.json, naming.json, progress.json); returns the store."""
    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.persistence import session as dbmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)

    def _with(words, **objects):
        session = SimpleNamespace(id=SID, job_id=None, geometry_source_id=uuid.uuid4(),
                                  geometry_interpretation_id=uuid.uuid4(), intake_gate={},
                                  messages=[{"role": "user", "content": w} for w in words])

        async def _owned(session_id, owner_id, organization_id):
            return session
        monkeypatch.setattr(route, "_owned_session", _owned)

        async def _interp(db, sess, owner_id, organization_id):
            return {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm",
                    "scale_to_metres": 0.001, "basis": "file_declared", "evidence": "declared in the file"}
        monkeypatch.setattr(gh, "interpretation_payload", _interp)

        @asynccontextmanager
        async def _db():
            yield SimpleNamespace(commit=AsyncMock())
        monkeypatch.setattr(dbmod, "get_db", _db)

        class _Sessions:
            async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
                return session
        monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

        async def _turn(body, owner_id, organization_id):
            return SimpleNamespace(model_dump=lambda mode="json": {"reply": "What will you analyze?"})
        monkeypatch.setattr(chatmod, "chat_message", _turn)
        store = _Store({k.replace("_", ".", 1) if k.endswith("_json") else k: v for k, v in objects.items()})
        monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
        return store
    return _with


def _scouted(**extra) -> dict:
    return {"status": "scouted", "named": False, "skin_key": "k", "written_at": time.time() - 60,
            "facts": dict(FACTS), "proposal": {"openings": [], "size_mm": [200.0, 100.0, 5.0], "up_axis": "+z"},
            **extra}


def _scout_record(now: float, *, ended: bool = True) -> dict:
    rec = {"picked_at": now - 55, "current": "pictures", "work": {}, "beat_at": now - 5,
           "steps": {"reading": {"at": now - 55, "end": now - 50}, "pictures": {"at": now - 50, "end": now - 45}}}
    if ended:
        rec["ended_at"] = now - 45
    return rec


async def _confirm():
    return await route.confirm_check(SID, route.ConfirmIn(unit="mm", size_mm=[200.0, 100.0, 5.0],
                                                          scale_to_m=0.001), "alice", "org-1")


async def _refused():
    with pytest.raises(HTTPException) as err:
        await _confirm()
    assert err.value.status_code == 409 and err.value.detail == gp.STILL_CHECKING
    return err.value


@pytest.mark.asyncio
async def test_a_confirm_while_the_scout_is_still_running_is_refused(stage):
    now = time.time()
    store = stage(["stress testing"], scout_json={"status": "pending", "written_at": now - 20},
                  progress_json={"scout": {"marker": now - 20, "picked_at": now - 18, "current": "measuring",
                                           "beat_at": now - 2, "work": {},
                                           "steps": {"reading": {"at": now - 18, "end": now - 10},
                                                     "measuring": {"at": now - 10}}}})
    served = await route.get_check(SID, "alice", "org-1")
    assert served["checking"] is True
    await _refused()
    assert f"sessions/{SID}/geometry_check/confirmed.json" not in store.objects    # nothing recorded


@pytest.mark.asyncio
async def test_an_fea_part_waits_for_its_walls_hot_spots_and_joints_then_proceeds(stage):
    now = time.time()
    naming = {"session_id": str(SID), "purpose_text": "stress testing", "requested_at": now - 30}
    measuring = {"scout": _scout_record(now),
                 "naming": {"picked_at": now - 28, "current": "naming", "beat_at": now - 3, "work": {},
                            "steps": {"naming": {"at": now - 27}}}}
    stage(["stress testing"], scout_json=_scouted(), naming_json=naming, progress_json=measuring)
    served = await route.get_check(SID, "alice", "org-1")
    assert served["route"] == "fea" and served["progress"]["state"] == "running"
    assert served["checking"] is True
    await _refused()
    # the measurement lands: the check is done, and Proceed goes through
    measured = {"scout": _scout_record(now),
                "naming": {**measuring["naming"], "ended_at": now - 1, "ok": True}}
    stage(["stress testing"], scout_json=_scouted(status="ready", named=True), naming_json=naming,
          progress_json=measured)
    assert (await route.get_check(SID, "alice", "org-1"))["checking"] is False
    out = await _confirm()
    assert out["ok"] is True


@pytest.mark.asyncio
async def test_a_naming_whose_worker_stopped_never_holds_proceed(stage):
    now = time.time()
    naming = {"session_id": str(SID), "purpose_text": "stress testing", "requested_at": now - 900}
    dead = {"scout": _scout_record(now),
            "naming": {"picked_at": now - 890, "current": "naming", "beat_at": now - gp.BEAT_STALE_S - 60,
                       "work": {}, "steps": {"naming": {"at": now - 889}}}}
    stage(["stress testing"], scout_json=_scouted(), naming_json=naming, progress_json=dead)
    served = await route.get_check(SID, "alice", "org-1")
    assert served["status"] == "failed" and served["checking"] is False      # the time box failed it
    assert (await _confirm())["ok"] is True                                  # the way on stays open


@pytest.mark.asyncio
async def test_a_preview_still_measured_waits_and_a_kept_one_proceeds(stage):
    now = time.time()
    beating = {"scout": {**_scout_record(now, ended=False), "current": "openings"}}
    stage(["stress testing"], scout_json=_scouted(preview="running"), progress_json=beating)
    assert (await route.get_check(SID, "alice", "org-1"))["checking"] is True
    await _refused()
    # the full reading's worker stopped: the quick drawing stands, and the user goes on with it
    stopped = {"scout": {**beating["scout"], "beat_at": now - gp.BEAT_STALE_S - 60}}
    stage(["stress testing"], scout_json=_scouted(preview="running"), progress_json=stopped)
    served = await route.get_check(SID, "alice", "org-1")
    assert served["preview"] == "kept" and served["checking"] is False
    assert (await _confirm())["ok"] is True


@pytest.mark.asyncio
async def test_a_check_waiting_on_the_users_answer_does_not_hold_proceed(stage):
    now = time.time()
    stage(["stress testing"], scout_json=_scouted(), progress_json={"scout": _scout_record(now)})
    served = await route.get_check(SID, "alice", "org-1")
    assert served["progress"]["state"] == "waiting" and served["checking"] is False
    assert (await _confirm())["ok"] is True


def test_a_naming_landed_on_a_preview_waits_only_while_the_full_reading_beats():
    now = time.time()
    view = {"state": "done"}
    ready = {"status": "ready", "named_on_preview": True}
    assert gp.still_checking(view, ready, {"scout": {"beat_at": now - 5}}, now) is True
    assert gp.still_checking(view, ready, {"scout": {"beat_at": now - gp.BEAT_STALE_S - 1}}, now) is False
    assert gp.still_checking(view, ready, {"scout": {"beat_at": now - 5, "ended_at": now - 1}}, now) is False
    assert gp.still_checking(view, {**ready, "confirmed": {}}, {"scout": {"beat_at": now - 5}}, now) is False
    assert gp.still_checking(None) is False
    for state in ("failed", "unsupported", "waiting", "done"):
        assert gp.still_checking({"state": state}) is False
    for state in ("queued", "running"):
        assert gp.still_checking({"state": state}) is True
