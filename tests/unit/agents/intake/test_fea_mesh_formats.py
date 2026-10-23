# Responsibility: Verify an FEA part uploaded as a surface mesh (STL, OBJ, PLY, 3MF, GLB) takes the same
# path as a STEP one - its unit is settled on the FEA stage, never by a chat question; the engine is
# never asked; the file is never called a STEP - and that a file which is not one closed solid is told so
# on the stage, with the way on, instead of failing later.
# Boundaries: the message flow's settle step with the store, the naming queue and the repositories stood
# in for; the FEA stage's view and the confirmation's refusal over the stored check facts of Areen's
# Camaro STL (dev-areen session e167cf45, 2026-10-23); the wording guard; the intake prompt text.
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from meshpipeline.agents.intake import message as msg
from meshpipeline.agents.intake import unit_clarification as uc
from meshpipeline.agents.intake.executor import fea_path
from meshpipeline.application import fea_check
from meshpipeline.application import geometry_hold as gh
from meshpipeline.application.fea_wording import settled_asks

SID = uuid.UUID("cccc1111-2222-4222-b222-cccccccccccc")

# what the check stored for the Camaro STL (trimmed to what the FEA stage reads)
CAMARO = {
    "solids": 395, "geometry_kind": "surface", "source_format": "stl", "body_kind": "scene",
    "validity": {"clean": False, "kind": "triangles", "summary": "The geometry is not clean: 20 edge(s) are "
                 "shared by more than two faces (3 more reasons)",
                 "reasons": [{"code": "non_manifold_edges", "severity": "error"},
                             {"code": "non_manifold_vertices", "severity": "error"},
                             {"code": "self_intersection", "severity": "error"},
                             {"code": "bodies_overlap", "severity": "error"}]},
}


async def _settle(monkeypatch, said: str, *, unit_known: bool = False):
    """One user message on a fresh upload whose file states no unit (a triangle file)."""
    monkeypatch.setattr(gh, "hold_applies", lambda sid: "queue")
    queued: list = []
    monkeypatch.setattr(gh, "queue_naming", lambda *a: queued.append(a[2]) or True)
    monkeypatch.setattr(gh, "stored_thermal", lambda sid: (False, ""))
    monkeypatch.setattr(gh, "measured_size_mm", lambda sid: [67.1, 28.25, 19.53])
    monkeypatch.setattr(gh, "measured_unit_inference", lambda sid, words="": None)

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(gh, "ensure_fixed_unit", _none)
    monkeypatch.setattr(gh, "interpretation_payload", _none)

    async def _mode(*a):
        return ""
    monkeypatch.setattr(gh, "upload_check_mode", _mode)
    repo = MagicMock(); repo.append_message = AsyncMock(); repo.set_intake_gate = AsyncMock()
    repo.bind_geometry_interpretation = AsyncMock()
    locked = SimpleNamespace(id=SID, owner_id="alice", job_id=None, geometry_source_id=uuid.uuid4(),
                             geometry_interpretation_id="i1" if unit_known else None, intake_gate={},
                             messages=[])
    messages = [{"role": "assistant", "content": "Geometry received. What are you meshing this for?"},
                {"role": "user", "content": said}]
    inbound = msg.InboundMessage(session_id=SID, owner_id="alice", content=said, organization_id="o")
    out = await msg._settle(inbound, AsyncMock(), gate={}, locked=locked, messages=messages,
                            revision="r1", session_repo=repo, logger=MagicMock())
    return out, queued, repo


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["chevorlet camaro, for FEA", "stress testing", "will this bracket break?"])
async def test_an_fea_part_with_no_unit_is_held_for_its_stage_not_asked_the_unit(monkeypatch, said):
    out, queued, repo = await _settle(monkeypatch, said)
    assert out.status is msg.MessageStatus.geometry_hold, out.reply
    assert queued == [said]                                  # the naming runs: the stage gets a name and a size check
    replies = [c.args[2] for c in repo.append_message.call_args_list]
    assert uc.QUESTION not in replies and not any("unit" in r.lower() for r in replies)


@pytest.mark.asyncio
async def test_a_cfd_part_with_no_unit_is_still_asked_the_unit_first(monkeypatch):
    out, queued, _ = await _settle(monkeypatch, "external aero around this car, 30 m/s")
    assert out.status is msg.MessageStatus.unit_question and queued == []


@pytest.mark.asyncio
async def test_a_unit_the_fea_user_names_is_still_recorded(monkeypatch):
    recorded = SimpleNamespace(interpretation_id=str(uuid.uuid4()))

    async def _record(*a, **k):
        return recorded
    monkeypatch.setattr(uc, "record", _record)
    out, queued, repo = await _settle(monkeypatch, "for FEA, the file is in millimetres")
    assert repo.bind_geometry_interpretation.await_count == 1
    assert out.status is msg.MessageStatus.geometry_hold and queued


def test_the_fea_path_holds_from_the_words_before_any_stage():
    assert fea_path(SimpleNamespace(declared_case=None, user_texts=("chevorlet camaro, for FEA",)))
    assert not fea_path(SimpleNamespace(declared_case=None, user_texts=("air flow around the car",)))
    assert not fea_path(SimpleNamespace(declared_case=None, user_texts=()))


@pytest.mark.parametrize("reply", [
    "Before meshing I need to know what one unit in your geometry means. Are the coordinates in "
    "millimetres, centimetres, metres, or inches?",
    "For a full-size Chevrolet Camaro, should I treat the STEP coordinates as **millimetres**?",
])
def test_a_chat_unit_question_on_the_fea_path_is_caught(reply):
    assert "the unit" in settled_asks(reply)


def test_the_intake_never_calls_an_upload_a_step():
    from meshpipeline.settings import policy as polcfg

    text = polcfg.prompts.intake_system
    assert "A STEP geometry file" not in text and "STEP filename" not in text
    assert "surface mesh" in text and "STL" in text


# ------------------------------------------------------------- not a closed solid ----
def test_a_car_body_surface_is_told_it_is_not_a_solid_with_the_way_on():
    view = fea_check.fea_view(CAMARO)
    assert view["can_proceed"] is False and view["blocked_kind"] == "surface"
    said = view["single_part_only"]
    assert said.startswith("This is a surface, not a closed solid: it is made of 395 separate surface pieces")
    assert "shell meshes for surfaces like this are coming" in said
    assert "395 separate bodies" not in said                 # not the multi-part message
    assert view["way_out"] == fea_check.WAY_OUT_SOLID and "closed solid" in view["way_out"]


@pytest.mark.parametrize("facts, kind", [
    # an open STL skin
    ({"solids": 1, "source_format": "stl",
      "validity": {"clean": False, "kind": "triangles", "reasons": [{"code": "open_boundary", "severity": "error"}]}},
     "surface"),
    # a STEP of faces with no solid in it
    ({"solids": 0, "source_format": "step", "validity": {"clean": True, "kind": "brep", "reasons": []}}, "surface"),
    # a STEP of open shells
    ({"solids": 0, "source_format": "step",
      "validity": {"clean": False, "kind": "brep", "reasons": [{"code": "open_shell", "severity": "error"}]}},
     "surface"),
    # three clean solids: an assembly, meshed - no stop (design 14)
    ({"solids": 3, "source_format": "step", "validity": {"clean": True, "kind": "brep", "reasons": []}}, ""),
    # three solids that pass into each other: interference between parts is a meshing choice - no stop
    ({"solids": 3, "source_format": "step",
      "validity": {"clean": False, "kind": "brep", "reasons": [{"code": "bodies_overlap", "severity": "error"}]}},
     ""),
    # a closed part whose surface passes through itself, from any format: it needs CAD repair
    ({"solids": 1, "source_format": "stl",
      "validity": {"clean": False, "kind": "triangles",
                   "reasons": [{"code": "self_intersection", "severity": "error"}]}}, "self_overlap"),
    # one clean closed solid, from any format: nothing stops it
    ({"solids": 1, "source_format": "step", "validity": {"clean": True, "kind": "brep", "reasons": []}}, ""),
    ({"solids": 1, "source_format": "obj", "validity": {"clean": True, "kind": "triangles", "reasons": []}}, ""),
    # a warning is not a reason to stop
    ({"solids": 1, "source_format": "stl", "validity": {"clean": True, "kind": "triangles",
                                                        "reasons": [{"code": "self_intersection",
                                                                     "severity": "warning"}]}}, ""),
])
def test_what_stops_version_1_is_read_the_same_for_every_format(facts, kind):
    blocked, got, way_out = fea_check.blocker(facts)
    assert got == kind and bool(blocked) == bool(kind) and bool(way_out) == bool(kind)
