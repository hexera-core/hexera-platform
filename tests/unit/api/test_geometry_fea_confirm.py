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


def _named(store):
    """The FEA naming has landed: its reading of the words is stored with the facts."""
    key = f"sessions/{SID}/geometry_check/scout.json"
    scout = json.loads(store.objects[key])
    scout["facts"]["mesh_as_words"] = {"value": "", "by": "", "why": "no mesh-as words"}
    store.objects[key] = json.dumps(scout).encode()


@pytest.mark.asyncio
async def test_plain_fea_serves_no_shell_and_confirms_a_solid_as_before(stage):
    """(a) "FEA" alone, even on a part that is all plate: no shell view, no shell check started, a solid confirmed
    with no shell words in the declaration."""
    from meshpipeline.application import shell_check
    from meshpipeline.contracts import geometry_check as seam

    session, store = stage(["FEA of this bracket"])
    _cad(store, None)
    _named(store)
    got = []
    seam.set_shell_check_enqueuer(lambda **kw: got.append(kw) or True)
    try:
        served = await route.get_check(SID, "alice", "org-1")
        assert "shell" not in served["fea"] and got == []
        # an old console's pick is ignored: only words make a shell
        out = await route.confirm_check(SID, route.ConfirmIn(unit="mm", fea_mesh_as="shell"), "alice", "org-1")
    finally:
        seam.set_shell_check_enqueuer(None)
    assert got == [] and f"sessions/{SID}/geometry_check/shell/status.json" not in store.objects
    assert "Meshed as" not in out["message"] and "shell" not in out["message"].lower()
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "solid" and record["fea_mesh_as_picked"] == ""
    assert shell_check.confirmed_mesh_as(str(SID)) == "solid"


@pytest.mark.asyncio
async def test_a_said_shell_serves_the_shell_and_confirms_it(stage):
    """(b) "FEA, shell meshing": the shell view with the mid-surface's legend; Proceed settles a shell."""
    from meshpipeline.application import shell_check

    session, store = stage(["FEA, shell meshing"])
    _cad(store, _SHELL_PART)
    served = await route.get_check(SID, "alice", "org-1")
    shell = served["fea"]["shell"]
    assert shell["mesh_as"] == "shell" and shell["said_words"] == "You asked for a shell mesh."
    assert shell["overlay"] and shell["legend_mm"] == [2.0, 2.0] and "options" not in shell
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert "Meshed as: a shell" in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell" and record["fea_mesh_as_picked"] == "shell (words)"
    assert shell_check.confirmed_mesh_as(str(SID)) == "shell"


@pytest.mark.asyncio
async def test_a_shell_asked_for_after_the_naming_starts_the_check_and_proceed_never_waits(stage):
    """A shell said in a later message: the stage's read starts the check then; Proceed while it measures is a
    shell all the same (the run makes it)."""
    from meshpipeline.contracts import geometry_check as seam

    session, store = stage(["stress testing", "and mesh it as a mid-surface shell"])
    _cad(store, None)
    _named(store)
    got = []
    seam.set_shell_check_enqueuer(lambda **kw: got.append(kw) or True)
    try:
        served = await route.get_check(SID, "alice", "org-1")
        assert len(got) == 1 and served["fea"]["shell"]["state"] == "measuring"
        assert served["fea"]["shell"]["measuring_words"].startswith("Making the mid-surface")
        out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    finally:
        seam.set_shell_check_enqueuer(None)
    assert len(got) == 1                                          # started once
    assert "Meshed as: a shell" in out["message"] and "You asked for a shell mesh." in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell"


@pytest.mark.asyncio
async def test_a_shell_asked_on_a_chunky_part_confirms_a_solid_with_the_reason(stage):
    """(d) No wall thin enough: a solid, said in one sentence; never a refusal."""
    session, store = stage(["FEA, shell meshing"])
    _cad(store, {"status": "ready", "scale_to_m": 0.001, "summary": {
        "bodies": [{"body": "Part_1", "verdict": "solid"}], "share": 0.0, "solid_regions": [
            {"body": "Part_1", "whole": True, "why": "no wall is thin against its size"}],
        "open_edges": 0, "open_places": [], "open_at_mm": [], "overlay": False}})
    served = await route.get_check(SID, "alice", "org-1")
    assert served["fea"]["shell"]["mesh_as"] == "solid"
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert ("Meshed as: a solid. You asked for a shell, but this part has no plate or thin wall to make one from, so it is "
            "meshed as a solid.") in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "solid" and record["fea_mesh_as_picked"] == "shell (words)"


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


# ---------------------------------------------------------------------------------- SURF-1/4/6: a surface file
_SHEET = {"kind": "surface", "why": "it holds no solid and its faces do not close: the open edges span 74% of the "
          "surface", "faces": 11, "sheets": 1, "open_share": 0.74, "sew_tolerance_mm": 0.019, "free_edges": 14,
          "unmet_edges": 0, "size_mm": 149.0}


def _surface_file(store, reading):
    """The stored check as a STEP file with no solid whose faces do not close, measured (its surface reading kept)."""
    key = f"sessions/{SID}/geometry_check/scout.json"
    scout = json.loads(store.objects[key])
    scout["facts"].update(source_format="step", solids=0, validity={
        "clean": False, "kind": "brep", "summary": "The geometry is not clean: the surface is not closed",
        "reasons": [{"code": "open_shell", "severity": "error", "message": "the surface is not closed"}]},
        fea_measure={"measured": True, "bodies": 1, "surface_model": reading},
        mesh_as_words={"value": "", "by": "", "why": "no mesh-as words"})
    store.objects[key] = json.dumps(scout).encode()


@pytest.mark.asyncio
async def test_a_surface_file_is_served_as_a_shell_and_confirmed_at_the_thickness_said(stage):
    """A file that is a surface: no stop, the one line in the stage's shell fold, no mid-surface check started;
    Proceed settles a shell at the thickness the user's words gave, and the run reads both."""
    from meshpipeline.application import shell_check, surface_check
    from meshpipeline.cad.fea_measure import FACT_KEY
    from meshpipeline.contracts import geometry_check as seam

    assert FACT_KEY == "fea_measure"
    session, store = stage(["FEA of this stamped bracket, 1.2 mm thick"], solids=0)
    _surface_file(store, _SHEET)
    got = []
    seam.set_shell_check_enqueuer(lambda **kw: got.append(kw) or True)
    try:
        served = await route.get_check(SID, "alice", "org-1")
        fea = served["fea"]
        assert fea["can_proceed"] is True and fea["single_part_only"] == ""
        assert fea["clean_short"] == "A surface, not a solid" and fea["clean"] is True
        assert fea["shell"]["label"] == "This file is a surface, not a solid. It is meshed as a shell, 1.2 mm thick."
        out = await route.confirm_check(SID, route.ConfirmIn(unit="mm", fea_shell_thickness_mm=9.0), "alice", "org-1")
    finally:
        seam.set_shell_check_enqueuer(None)
    assert got == [], "a surface has no mid-surface to extract: no shell check is started"
    assert "Meshed as: a shell - plate elements on the surface as given" in out["message"]
    assert "It is meshed as a shell, 1.2 mm thick." in out["message"]
    # the declaration does not call the sheet's own open edges "not clean"
    assert "not clean" not in out["message"] and "Clean check: The file is a surface" in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell" and record["fea_mesh_as_picked"] == "shell (surface file)"
    assert record["fea_shell_thickness_mm"] == 1.2          # the server's reading, never the client's 9.0
    assert shell_check.confirmed_mesh_as(str(SID)) == "shell"
    assert surface_check.confirmed_thickness(str(SID)) == 1.2


@pytest.mark.asyncio
async def test_a_surface_file_with_no_thickness_said_still_proceeds(stage):
    from meshpipeline.application import surface_check

    session, store = stage(["stress testing"], solids=0)
    _surface_file(store, _SHEET)
    served = await route.get_check(SID, "alice", "org-1")
    assert served["fea"]["shell"]["label"] == "This file is a surface, not a solid. It is meshed as a shell."
    assert served["fea"]["shell"]["reason"].startswith("No thickness given yet.")
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert "It is meshed as a shell." in out["message"]
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell" and record["fea_shell_thickness_mm"] is None
    assert surface_check.confirmed_thickness(str(SID)) is None


@pytest.mark.asyncio
async def test_a_closed_skin_with_a_gap_is_refused_with_both_ways_on_until_a_shell_is_said(stage):
    from fastapi import HTTPException

    session, store = stage(["stress testing"], solids=0)
    _surface_file(store, {**_SHEET, "kind": "gap", "open_share": 0.002})
    served = await route.get_check(SID, "alice", "org-1")
    assert served["fea"]["can_proceed"] is False and served["fea"]["blocked_kind"] == "gap"
    assert "shell" not in served["fea"]
    with pytest.raises(HTTPException) as err:
        await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    assert err.value.status_code == 409 and "close the gaps in your CAD" in err.value.detail
    assert 'say so (for example "shell, 1.2 mm thick")' in err.value.detail
    session.messages.append({"role": "user", "content": "it is a sheet: shell, 0.8 mm thick"})
    out = await route.confirm_check(SID, route.ConfirmIn(unit="mm"), "alice", "org-1")
    record = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert record["fea_mesh_as"] == "shell" and record["fea_shell_thickness_mm"] == 0.8
    assert "0.8 mm thick" in out["message"]
