# Responsibility: Verify the stage is served the other reading of a doubtful unit beside the part's
# size - a "117 mm" wind turbine blade once the user has said what it is, a triangle file whose
# numbers read as a 1 mm car - and nothing once the user has named the unit; and that a unit
# changed on the stage is stated in the declaration and withdraws a live run proposal with it.
# Boundaries: the routes with the session, the store and the interpretation stood in for.
from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import meshpipeline.settings.geometry_check as gcfg
from meshpipeline.agents.intake import approval as ap
from meshpipeline.api.v1 import geometry as route
from meshpipeline.application import geometry_check as gc
from meshpipeline.application import geometry_hold as gh
from meshpipeline.contracts import object_storage

pytestmark = pytest.mark.asyncio

SID = uuid.UUID("ffff1111-2222-4222-b222-ffffffffffff")
DECLARED_MM = {"interpretation_id": "i1", "geometry_source_id": "s1", "unit": "mm", "scale_to_metres": 0.001,
               "basis": "file_declared", "evidence": "declared in the file as millimetre"}


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
        return f"https://pictures/{object_key}"


def _scouted(size, scale=0.001) -> dict:
    return {"status": "scouted", "named": False, "written_at": time.time(),
            "facts": {"size_mm": size, "scale_to_m": scale},
            "proposal": {"openings": [], "size_mm": size}, "snapshots": [], "skin_key": "k"}


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(gcfg, "GEOMETRY_CHECK_ENABLED", True)
    session = SimpleNamespace(id=SID, job_id=None, geometry_source_id=uuid.uuid4(),
                              geometry_interpretation_id=uuid.uuid4(), messages=[], intake_gate={})

    async def _owned(session_id, owner_id, organization_id):
        return session
    monkeypatch.setattr(route, "_owned_session", _owned)

    @asynccontextmanager
    async def _db():
        yield SimpleNamespace(commit=AsyncMock())
    from meshpipeline.persistence import session as dbmod
    monkeypatch.setattr(dbmod, "get_db", _db)

    def _with(check: dict, interpretation: dict | None, **over):
        for k, v in over.items():
            setattr(session, k, v)
        store = _Store({"scout.json": check})
        monkeypatch.setattr(object_storage, "get_object_store", lambda: store)

        async def _interp(db, sess, owner_id, organization_id):
            return interpretation
        monkeypatch.setattr(gh, "interpretation_payload", _interp)
        return session, store
    return _with


async def test_a_117_mm_blade_is_served_with_the_117_m_reading_once_the_user_has_said_what_it_is(served):
    served(_scouted([6.836, 6.595, 117.0]), DECLARED_MM,
           messages=[{"role": "user", "content": "External flow around the IEA 15 MW wind turbine blade"}])
    d = await route.get_check(SID, "alice", "org-1")
    s = d["proposal"]["unit_suggestion"]
    assert d["proposal"]["unit"] == "mm" and d["proposal"]["unit_basis"] == "file_declared"
    assert s["unit"] == "m" and s["instead_of"] == "mm"
    assert s["words"] == "117 mm long - or 117 m if the file is in metres"
    assert s["sizes"] == {"mm": "117 mm", "m": "117 m"}


async def test_the_naming_models_estimate_is_enough_on_its_own(served):
    check = _scouted([6.836, 6.595, 117.0])
    check["proposal"].update(part="wind turbine blade", real_length_m=117.0)
    served(check, DECLARED_MM, messages=[{"role": "user", "content": "mesh this"}])
    d = await route.get_check(SID, "alice", "org-1")
    assert d["proposal"]["unit_suggestion"]["unit"] == "m"


async def test_nothing_is_suggested_for_a_believable_part_or_once_the_user_named_the_unit(served):
    served(_scouted([6.836, 6.595, 117.0]), DECLARED_MM, messages=[{"role": "user", "content": "a small pin"}])
    assert "unit_suggestion" not in (await route.get_check(SID, "alice", "org-1"))["proposal"]
    confirmed = {**DECLARED_MM, "basis": "user_confirmed"}
    served(_scouted([6.836, 6.595, 117.0]), confirmed,
           messages=[{"role": "user", "content": "the IEA 15 MW wind turbine blade"}])
    assert "unit_suggestion" not in (await route.get_check(SID, "alice", "org-1"))["proposal"]


async def test_a_triangle_file_that_reads_as_a_1_mm_car_is_served_the_metre_reading(served):
    """The Ahmed body STL is 1.044 units long: no unit, read as millimetres until the user says -
    and a 1 mm car is not a car. The stage offers 1.04 m beside it."""
    served(_scouted([1.044, 0.389, 0.338]), None, geometry_interpretation_id=None,
           messages=[{"role": "user", "content": "flow around this"}])
    d = await route.get_check(SID, "alice", "org-1")
    s = d["proposal"]["unit_suggestion"]
    assert d["unit_needed"] is True and d["proposal"]["unit"] == "mm" and d["proposal"]["unit_basis"] == "assumed"
    assert s["unit"] == "m" and s["words"] == "1.04 mm long - or 1.04 m if the file is in metres"


async def test_the_suggestion_reads_the_file_units_back_through_the_scale_they_were_measured_under():
    # a naming that re-read the facts in metres serves 117000 "mm" under a scale of 1.0: the file's
    # own number is still 117, and in metres (the user's unit) nothing is doubted
    re_read = {"size_mm": [6836.0, 6595.0, 117000.0], "scale_to_m": 1.0}
    assert gc.unit_suggestion(re_read, {**DECLARED_MM, "unit": "m", "scale_to_metres": 1.0},
                              "a wind turbine blade") is None
    assert gc.unit_suggestion({"size_mm": [1, 2, 3]}, None, "") is None                # no scale served
    assert gc.unit_suggestion({"size_mm": "x", "scale_to_m": 0.001}, None, "") is None


async def test_a_unit_changed_on_the_stage_withdraws_a_live_proposal_in_the_same_transaction(served, monkeypatch):
    """The unit, the re-read sizes and the withdrawal of a run proposed with the old sizes land
    together - the withdrawal by the intake authority, inside the confirmation's transaction -
    even when the chat turn that follows the confirmation fails."""
    from meshpipeline.api.v1 import chat as chatmod
    from meshpipeline.contracts.geometry_units import GeometryInterpretation, LengthUnit
    from meshpipeline.persistence.repositories import geometry_interpretation_repository as girmod
    from meshpipeline.persistence.repositories import session_repository as srmod

    live = {"id": "snap", "status": ap.AWAITING, "expires_at": time.time() + 600}
    session, store = served(_scouted([6.836, 6.595, 117.0]), DECLARED_MM, messages=[], intake_gate={"approval": live})
    written: list = []

    class _Interps:
        async def record(self, db, *, owner_id, geometry_source_id, unit, basis, evidence, organization_id=""):
            return GeometryInterpretation(interpretation_id=str(uuid.uuid4()), owner_id=owner_id,
                                          geometry_source_id=str(geometry_source_id), unit=LengthUnit(unit),
                                          scale_to_metres=1.0, basis=basis, evidence=evidence)
    monkeypatch.setattr(girmod, "GeometryInterpretationRepository", _Interps)

    class _Sessions:
        async def get_for_owner(self, db, session_id, owner_id, organization_id=""):
            return session

        async def bind_geometry_interpretation(self, db, session_id, interpretation_id):
            written.append(("bind", interpretation_id))

        async def set_intake_gate(self, db, session_id, gate):
            written.append(("gate", gate))
            session.intake_gate = gate
    monkeypatch.setattr(srmod, "SessionRepository", _Sessions)

    async def _turn_fails(body, owner_id, organization_id):
        raise RuntimeError("the model is down")
    monkeypatch.setattr(chatmod, "chat_message", _turn_fails)
    body = route.ConfirmIn(input_kind="solid-body", flow="external", size_mm=[6.836, 6.595, 117.0], flow_axis="+z",
                           reference_length_mm=117.0, scale_to_m=0.001, unit="m")
    out = await route.confirm_check(SID, body, "alice", "org-1")
    assert "Reference length: 117000 mm along the flow." in out["message"]
    assert "The file is in metres: the part is 117 m long." in out["message"]
    assert [w[0] for w in written] == ["bind", "gate"]
    assert session.intake_gate["approval"]["status"] == ap.INVALIDATED
    assert out["next"] is None                                  # the turn failed; the withdrawal stands
    # the stored copy says the scale its sizes are in, for any later change to re-read from
    stored = json.loads(store.objects[f"sessions/{SID}/geometry_check/confirmed.json"])
    assert stored["scale_to_m"] == 1.0 and stored["unit"] == "m" and stored["reference_length_mm"] == 117000.0
