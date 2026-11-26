# Responsibility: Verify FLOW-9: a part whose faces read as a body in a flow while its inside-space map finds a way
# through it (a passage with two mouths or more) is asked, drawn meanwhile as flow through it, with the mouths of
# that way proposed as its openings - never the flat ends' whole discs of metal round them. A body with only blind
# holes, and a body whose inside has one mouth, are read as before.
# Boundaries: real OpenCASCADE solids written to STEP; the geometry check's CAD reading (inside map, openings, flow
# kind); no mesher, no model.
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from meshpipeline.application import flow_kind as fk
from meshpipeline.application import geometry_check as gc

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(r, h, p=(0.0, 0.0, 0.0), d=(0.0, 0.0, 1.0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*d)), r, h).Shape()


def _box(x0, y0, z0, dx, dy, dz):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _cut(a, *bs):
    for b in bs:
        a = BRepAlgoAPI_Cut(a, b).Shape()
    return a


def _check(shape, tmp: Path) -> dict:
    w = STEPControl_Writer()
    assert Interface_Static.SetCVal_s("write.step.unit", "MM")
    w.Transfer(shape, STEPControl_AsIs)
    w.Write(str(tmp / "part.step"))
    facts, _skin = gc._scout_exact(tmp / "part.step", tmp, None, _REF)
    return facts


def _ports(facts: dict) -> list[dict]:
    return [o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]


def test_an_end_plug_with_a_bore_is_asked_as_flow_through_it_with_the_bores_mouths(tmp_path):
    """A 56 mm plug 22 mm long with a 16 mm bore: its faces read a body (its end faces the openings, 54 mm discs of
    metal round the bore); the map finds the bore, a way through with two mouths. Proposed: flow through it, asked,
    its openings the bore's two 16 mm holes; the end faces kept as not openings."""
    facts = _check(_cut(_cyl(28.0, 22.0), _cyl(8.0, 30.0, (0.0, 0.0, -4.0))), tmp_path)
    rec = facts["flow_kind"]
    assert facts["flow_scout"] == "external"
    assert rec["kind"] == fk.UNDECIDED and rec["question"] == fk.QUESTION           # still asked
    assert rec["flow"] == "internal" and rec["best_guess"] == "internal" and rec["guess_rule"] == "FLOW-9"
    assert facts["flow"] == "internal" and facts["input_kind"] == "body-surface"
    ports = _ports(facts)
    assert len(ports) == 2 and {o["role"] for o in ports} == {"inlet", "outlet"}
    assert all(o["kind"] != "disc" and o["diameter_mm"] == pytest.approx(16.0, rel=0.02) for o in ports)
    assert sorted(round(o["centroid_mm"][2], 1) for o in ports) == [0.0, 22.0]
    discs = [o for o in facts["openings"] if o.get("kind") == "disc"]
    assert discs and all(o["role"] == "not_an_opening" for o in discs)
    assert any("FLOW-9" in n for n in facts["notes"])


def test_a_block_with_a_bent_bore_is_drawn_as_flow_through_it(tmp_path):
    """A 60 mm block with a 12 mm bore turning a corner inside it: the map's two mouths were proposed already (its
    faces gave none, INS-2); the part is now drawn as flow through it while asked, those two mouths unchanged."""
    facts = _check(_cut(_box(-30.0, -30.0, 0.0, 60.0, 60.0, 40.0), _cyl(6.0, 31.0, (0.0, 0.0, -1.0)),
                        _cyl(6.0, 40.0, (0.0, 0.0, 30.0), (1.0, 0.0, 0.0))), tmp_path)
    rec = facts["flow_kind"]
    assert rec["kind"] == fk.UNDECIDED and rec["flow"] == "internal" and rec["guess_rule"] == "FLOW-9"
    ports = _ports(facts)
    assert len(ports) == 2 and all(o["diameter_mm"] == pytest.approx(12.0, rel=0.03) for o in ports)
    assert not any("FLOW-9" in n for n in facts["notes"])          # the openings were the mouths already


@pytest.mark.parametrize("name", ["blind_holes", "one_mouth"])
def test_a_body_with_no_way_through_is_read_as_before(tmp_path, name):
    """Two blind holes in a block, and a block with one deep pocket: no passage with two mouths - a body in a
    flow, as before, its openings unchanged."""
    block = _box(-30.0, -30.0, 0.0, 60.0, 60.0, 20.0 if name == "blind_holes" else 40.0)
    shape = (_cut(block, _cyl(5.0, 12.0, (10.0, 0.0, 10.0)), _cyl(5.0, 12.0, (-10.0, 0.0, 10.0)))
             if name == "blind_holes" else _cut(block, _cyl(8.0, 30.0, (0.0, 0.0, 15.0))))
    facts = _check(shape, tmp_path)
    rec = facts["flow_kind"]
    assert not facts["inside"]["has_inside"]
    assert rec["kind"] == "external" and rec["flow"] == "external" and "guess_rule" not in rec
    assert not _ports(facts)
    assert not any("FLOW-9" in n for n in facts["notes"])


def test_the_flow_kind_authority_guesses_through_it_only_for_a_body_the_map_finds_a_way_through():
    """The authority alone, on the facts: faces that read a body and a map with a two-mouth passage - undecided,
    best guess flow through it; the same faces and a map with one mouth - a body in a flow, decided."""
    base = {"flow": "external", "input_kind": "solid-body", "body_kind": "single_solid", "size_mm": [56, 56, 22],
            "openings": [{"role": "inlet", "kind": "ring", "diameter_mm": 16.0},
                         {"role": "outlet", "kind": "ring", "diameter_mm": 16.0}]}
    through = fk.decide({**base, "inside": {"mapped": True, "has_inside": True, "mouths_per_passage": [2],
                                            "enclosed_mm3": 40000.0}})
    assert through["kind"] == fk.UNDECIDED and through["flow"] == "internal" and through["guess_rule"] == "FLOW-9"
    assert through["question"] == fk.QUESTION and through["input_kind"] == "body-surface"
    # the user's answer still decides, both ways (FLOW-1)
    facts = {**base, "inside": {"mapped": True, "has_inside": True, "mouths_per_passage": [2], "enclosed_mm3": 40000.0}}
    assert fk.answer(through, facts, "The flow goes around the outside")["flow"] == "external"
    assert fk.answer(through, facts, "The flow goes through the inside")["flow"] == "internal"
    blind = fk.decide({**base, "inside": {"mapped": True, "has_inside": False, "mouths_per_passage": [1],
                                          "enclosed_mm3": 40000.0}})
    assert blind["flow"] == "external" and "guess_rule" not in blind
