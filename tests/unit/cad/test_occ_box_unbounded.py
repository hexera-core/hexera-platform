"""A face on an unbounded surface with no triangles never sets the part's size (cad/occ_box)."""
from __future__ import annotations

import pytest

pytest.importorskip("OCP")


def test_an_unbounded_face_is_left_out_of_the_size():
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pln
    from OCP.TopoDS import TopoDS_Compound

    from meshpipeline.cad.occ_box import diagonal, incremental_mesh, surface_box
    solid = BRepPrimAPI_MakeBox(1.0, 2.0, 3.0).Shape()
    incremental_mesh(solid, 0.01, 0.5)
    plane = BRepBuilderAPI_MakeFace(gp_Pln()).Face()      # infinite: nothing to triangulate
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    b.Add(comp, solid)
    b.Add(comp, plane)
    box = surface_box(comp)
    assert box is not None
    assert diagonal(box) == pytest.approx((1 + 4 + 9) ** 0.5, rel=1e-3)
