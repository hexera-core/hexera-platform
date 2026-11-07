# Responsibility: Verify the geometry check's FEA path at the route: a part the user's words sent to
# FEA is served its own stage (`route` "fea" and the `fea` view, never before the purpose is known),
# is confirmed as a structural solid with no fluid, openings or flow, and a file of several bodies
# is refused with the plain single-part message.
# Boundaries: the route with the session, the store and the chat turn stood in for; no database.
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application.geometry_confirmation import declared_case
from meshpipeline.contracts import object_storage

SID = uuid.UUID("dddd1111-2222-4222-b222-eeeeeeeeeeee")


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
    stored scout of `solids` bodies; returns (session, store)."""
    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.persistence import session as dbmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)

    def _with(words, solids=1):
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
        scout = {"status": "scouted", "named": False, "skin_key": "k", "facts": {
            "size_mm": [200.0, 100.0, 5.0], "scale_to_m": 0.001, "solids": solids, "unit_assumed": False,
            "validity": {"clean": True, "kind": "brep", "summary": "The geometry is clean."}},
            "proposal": {"openings": [], "size_mm": [200.0, 100.0, 5.0], "up_axis": "+z"}}
        store = _Store({"scout.json": scout})
        monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
        return session, store
    return _with


@pytest.mark.asyncio
async def test_the_stage_waits_for_the_purpose_and_an_fea_part_gets_its_own_view(stage):
    stage([])
    served = await route.get_check(SID, "alice", "org-1")
    assert served["route"] == "" and "fea" not in served
    stage(["stress testing"])
    served = await route.get_check(SID, "alice", "org-1")
    assert served["route"] == "fea"
    assert served["fea"]["bodies"] == 1 and served["fea"]["clean"] is True and "assembly" not in served["fea"]
    assert served["proposal"]["unit"] == "mm"
    stage(["airflow over it"])
    assert (await route.get_check(SID, "alice", "org-1"))["route"] == "flow"


@pytest.mark.asyncio
async def test_an_fea_part_is_confirmed_as_a_structural_solid_with_no_flow(stage):
    session, store = stage(["stress testing"])
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm", size_mm=[200.0, 100.0, 5.0],
                                                         scale_to_m=0.001), "alice", "org-1")
    assert out["patches"] == []
    assert declared_case([{"role": "assistant", "content": out["message"]}]) == ("structural", "solid-body")
    assert "One body." in out["message"] and "The geometry is clean." in out["message"]
    assert session.input_kind == "solid-body"
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["check_mode"] == "fea" and record["flow"] is None and record["openings"] == []


@pytest.mark.asyncio
async def test_a_file_of_several_bodies_is_confirmed_as_an_assembly_joined_as_asked(stage):
    from meshpipeline.application import fea_check

    session, store = stage(["static stress"], solids=3)
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert "3 bodies." in out["message"] and "An assembly of several parts, meshed together as solids." in out["message"]
    assert "Parts connected: bonded (welded or glued)" in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_connection"] == "bonded"
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm", fea_connection="contact"), "alice", "org-1")
    assert "Parts connected: contact (they can slide or separate)" in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_connection"] == "contact"
    # the run reads the stored answer
    from meshpipeline.contracts import object_storage
    assert object_storage.get_object_store() is store
    assert fea_check.confirmed_connection(SID) == "contact"
    assert fea_check.confirmed_connection("") == ""


def _cad(store, status=None):
    """The stored check as a STEP part (a mid-surface can be extracted), with a shell check status."""
    key = f"sessions/{SID}/geometry_check/scout.json"
    scout = json.loads(store.objects[key])
    scout["facts"]["source_format"] = "step"
    store.objects[key] = json.dumps(scout).encode()
    if status is not None:
        store.objects[f"sessions/{SID}/geometry_check/shell/status.json"] = json.dumps(status).encode()


_SHELL_PART = {"status": "ready", "scale_to_m": 0.001, "summary": {
    "bodies": [{"body": "Part_1", "verdict": "shell"}], "share": 0.97, "t_min_mm": 2.0, "t_max_mm": 2.0,
    "solid_regions": [], "open_edges": 0, "open_places": [], "open_at_mm": [], "overlay": True,
    "t_min_drawn_mm": 2.0, "t_max_drawn_mm": 2.0}}


@pytest.mark.asyncio
async def test_the_fea_stage_serves_mesh_as_with_the_recommendation(stage):
    session, store = stage(["stress testing"])
    _cad(store, _SHELL_PART)
    served = await route.get_check(SID, "alice", "org-1")
    shell = served["fea"]["shell"]
    assert shell["question"] == "Mesh as" and [o["key"] for o in shell["options"]] == ["auto", "shell", "solid"]
    assert shell["recommend"] == "shell" and shell["reason"].startswith("97% of this part is plate 2 mm thick")
    assert shell["default"] == "auto"


@pytest.mark.asyncio
async def test_proceed_settles_the_choice_and_stores_it_for_the_run(stage):
    from meshpipeline.application import shell_check

    session, store = stage(["stress testing"])
    _cad(store, _SHELL_PART)
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm", fea_mesh_as="auto"), "alice", "org-1")
    assert "Meshed as: a shell" in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell" and record["fea_mesh_as_picked"] == "auto (auto)"
    assert shell_check.confirmed_mesh_as(str(SID)) == "shell"
    # the user's click stands over Auto
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm", fea_mesh_as="solid"), "alice", "org-1")
    assert "Meshed as: a solid. You picked a solid mesh." in out["message"]
    assert shell_check.confirmed_mesh_as(str(SID)) == "solid"


@pytest.mark.asyncio
async def test_proceed_while_measuring_settles_now_and_never_waits(stage):
    import time as _t

    session, store = stage(["stress testing"])
    _cad(store, {"status": "measuring", "started_at": _t.time()})
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert "Meshed as: a solid. The walls were still being measured" in out["message"]
    # shell words in the chat are the user's choice, measured or not
    session, store = stage(["stress testing, CQUAD4 shell model"])
    _cad(store, {"status": "measuring", "started_at": _t.time()})
    served = await route.get_check(SID, "alice", "org-1")
    assert served["fea"]["shell"]["default"] == "shell"
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert "Meshed as: a shell" in out["message"] and "You asked for a shell mesh in the chat." in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell" and record["fea_mesh_as_picked"] == "shell (words)"


@pytest.mark.asyncio
async def test_the_mid_surface_drawing_is_served_once_stored(stage):
    from fastapi import HTTPException

    session, store = stage(["stress testing"])
    with pytest.raises(HTTPException) as no:
        await route.get_check_shell_surface(SID, "alice", "org-1")
    assert no.value.status_code == 404
    store.objects[f"sessions/{SID}/geometry_check/shell/overlay.json"] = b'{"version": 1}'
    got = await route.get_check_shell_surface(SID, "alice", "org-1")
    assert got.body == b'{"version": 1}'
