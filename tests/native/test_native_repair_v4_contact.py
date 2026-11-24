# Responsibility: A11 step 2 - the proximity kernel must FIND a contact (two faces at gap 0 sharing no edge) and
#                 must still ignore faces that share an edge. The whole aimed-op change rests on this.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI", reason="the CAD kernel is a native-tier dependency")

from OCP.BRep import BRep_Builder  # noqa: E402
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402
from OCP.TopoDS import TopoDS_Compound  # noqa: E402

from meshpipeline.cad.repair_v4 import kernel_calls  # noqa: E402,F401  (import order: as a worker does)
from meshpipeline.cad.repair_v4.sites_kernel import sites_proximity  # noqa: E402


def _write(shape, path) -> str:
    assert BRepTools.Write_s(shape, str(path))
    return str(path)


def _compound(*shapes):
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for s in shapes:
        b.Add(c, s)
    return c


def _pairs(path, max_dist=50.0):
    return sites_proximity({"brep_path": path, "max_dist": max_dist, "max_pairs": 50})["pairs"]


def test_two_boxes_meeting_at_one_corner_are_a_contact(tmp_path) -> None:
    """The A11 geometry: they touch (gap 0) and share a vertex, so the old rule DROPPED them. 9 of 14 real
    parts carried this class and no op could ever be aimed at it."""
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 10), 10.0, 10.0, 10.0).Shape()   # corner-to-corner
    pairs = _pairs(_write(_compound(a, b), tmp_path / "corner.brep"))

    touching = [p for p in pairs if p["touching"]]
    assert touching, "a corner contact must be reported"
    for p in touching:
        assert p["dist"] == pytest.approx(0.0, abs=1e-7)
        assert p["area_a_mm2"] > 0 and p["area_b_mm2"] > 0


def test_the_faces_of_one_sound_box_are_never_a_clash(tmp_path) -> None:
    """Every pair there either shares an edge (excluded) or is a long way off: a sound part must stay silent."""
    pairs = _pairs(_write(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape(), tmp_path / "box.brep"),
                   max_dist=1.0)
    assert [p for p in pairs if p["touching"]] == []


def test_a_near_miss_is_reported_and_is_not_called_a_contact(tmp_path) -> None:
    a = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    b = BRepPrimAPI_MakeBox(gp_Pnt(10.5, 0, 0), 10.0, 10.0, 10.0).Shape()   # 0.5 mm apart
    pairs = _pairs(_write(_compound(a, b), tmp_path / "near.brep"))
    near = [p for p in pairs if not p["touching"]]
    assert near and min(p["dist"] for p in near) == pytest.approx(0.5, abs=1e-6)
    assert all(not p["touching"] for p in near)


def test_coincident_duplicate_faces_are_a_contact_with_equal_areas(tmp_path) -> None:
    """A11 found exactly this on 7e573047c1a6 (faces 108/109, identical area 9032.0789 mm2)."""
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    same = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()
    pairs = _pairs(_write(_compound(box, same), tmp_path / "dup.brep"), max_dist=1.0)
    twins = [p for p in pairs if p["touching"] and p["area_a_mm2"] == pytest.approx(p["area_b_mm2"])]
    assert twins, "two copies of one face must come back as a contact of equal areas"
