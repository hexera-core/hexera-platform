# Responsibility: Verify INS-28: a flat ring face whose hole is a recess in the outside - a cup's pushed-up base inside
# its foot ring - is proposed as not an opening (the faces read it as a ring opening; the part's own wall closes it a
# little way in). A counterbored tube's end, a recess leading into a bore and a hole through a plate keep their
# openings.
# Boundaries: real OpenCASCADE solids written to STEP; the geometry check's CAD reading; no mesher, no model.
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

from meshpipeline.application import geometry_check as gc

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(r, h, p=(0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(0.0, 0.0, 1.0)), r, h).Shape()


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


def _at_z(facts: dict, z: float) -> dict:
    return next(o for o in facts["openings"] if abs(o["centroid_mm"][2] - z) < 0.1 and o.get("kind") == "ring")


def test_a_cups_recessed_base_inside_its_foot_ring_is_no_opening(tmp_path):
    """A 40 mm cup open at the top (34 mm), its 6 mm floor recessed 3 mm from below inside a foot ring (24 mm): the
    faces read the foot ring as a 24 mm ring opening, proposed as the inlet before; it is a recess, not an opening.
    The cup's open top stays its opening."""
    facts = _check(_cut(_cyl(20.0, 60.0), _cyl(17.0, 60.0, (0.0, 0.0, 6.0)), _cyl(12.0, 3.0, (0.0, 0.0, -0.01))),
                   tmp_path)
    foot = _at_z(facts, 0.0)
    assert foot["role"] == "not_an_opening"
    assert any("INS-28" in n for n in facts["notes"])
    assert _at_z(facts, 60.0)["role"] in ("inlet", "outlet")


@pytest.mark.parametrize("name", ["counterbored_tube", "plate_hole"])
def test_a_hole_that_leads_somewhere_keeps_its_opening(tmp_path, name):
    """A tube whose 16 mm bore opens into a 24 mm counterbore at its end, and a 24 mm hole through a 3 mm plate: the
    hole leads into the bore, or through to the open air - never a recess."""
    if name == "counterbored_tube":
        shape = _cut(_cyl(20.0, 60.0), _cyl(12.0, 3.0, (0.0, 0.0, -0.01)), _cyl(8.0, 62.0, (0.0, 0.0, -1.0)))
    else:
        shape = _cut(BRepPrimAPI_MakeBox(gp_Pnt(-30.0, -30.0, 0.0), 60.0, 60.0, 3.0).Shape(),
                     _cyl(12.0, 5.0, (0.0, 0.0, -1.0)))
    facts = _check(shape, tmp_path)
    assert not any("INS-28" in n for n in facts["notes"])
    assert len([o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]) == 2
