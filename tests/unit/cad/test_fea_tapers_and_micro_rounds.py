# Responsibility: Pin two measurement rules (27 Oct): a TAPER is read on the CAD's own edges, so two faces meeting at an acute edge are never a wall even when their triangles were not welded (PLAN-fea-3); and a round under the measure's own floor is a micro detail, never a hot spot or a round zone (PLAN-fea-12).
# Boundaries: parts built here with OpenCASCADE and measured in-process; nothing is meshed.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from meshpipeline.cad import fea_features as ff  # noqa: E402
from meshpipeline.cad.fea_measure import _brep_tapers, measure_cad  # noqa: E402


def _faces_map(shape):
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    m = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, m)
    return m


def _prism(points):
    """A prism 20 mm deep of a triangle in the xy plane."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Vec

    poly = BRepBuilderAPI_MakePolygon()
    for x, y in points:
        poly.Add(gp_Pnt(x, y, 0.0))
    poly.Close()
    return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(poly.Wire()).Face(), gp_Vec(0, 0, 20)).Shape()


def test_a_knife_edge_is_a_taper_on_the_cad_and_a_box_has_none():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    wedge = _prism([(0, 0), (50, 0), (50, 2.0)])          # 2.3 and 87.7 degrees: two acute edges, one right
    m = _faces_map(wedge)
    tapers = _brep_tapers(wedge, m)
    assert len(tapers) == 2
    box = BRepPrimAPI_MakeBox(20.0, 20.0, 20.0).Shape()
    assert _brep_tapers(box, _faces_map(box)) == []


def test_a_wedge_reads_no_wall_at_its_knife_edge():
    rec = measure_cad(_prism([(0, 0), (50, 0), (50, 2.0)]), mm_per_unit=1.0)
    # the wall the faces agree on is the wedge's body, never the knife edge's vanishing thickness
    assert rec["thickness"]["min_mm"] > 0.2


def _filleted_box(x, y, z, r):
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    box = BRepPrimAPI_MakeBox(x, y, z).Shape()
    mk = BRepFilletAPI_MakeFillet(box)
    mk.Add(r, TopoDS.Edge_s(TopExp_Explorer(box, TopAbs_EDGE).Current()))
    return mk.Shape()


def test_a_round_under_the_measure_floor_is_a_micro_detail_not_a_zone():
    """A kart frame's 0.0016 mm rounds on 600 mm tubes asked for 0.4 um elements (27 Oct)."""
    floor = ff.FLOOR_FRACTION * (4000.0 ** 2 + 2 * 1000.0 ** 2) ** 0.5           # 0.049 mm
    rec = measure_cad(_filleted_box(4000.0, 1000.0, 1000.0, 0.02), mm_per_unit=1.0)
    assert not any((r or {}).get("radius_mm") for r in rec["faces"].values())
    # read as a round, it is set aside under the floor; too small to tessellate, it is no round at all
    assert all(m["size_mm"] < floor for m in rec.get("sub_floor") or [])
    assert 0.02 < floor


def test_a_round_over_the_floor_keeps_its_zone():
    rec = measure_cad(_filleted_box(400.0, 100.0, 100.0, 0.02), mm_per_unit=1.0)        # floor 0.0042 mm
    assert any((r or {}).get("radius_mm") == pytest.approx(0.02, rel=1e-3) for r in rec["faces"].values())
    assert not rec.get("sub_floor")
