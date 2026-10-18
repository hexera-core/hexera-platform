# Responsibility: Verify a declared port is matched when a body stands in the bore (an annular passage's centre rod).
# annular_001 declares its 151 mm bore around a 125 mm rod (5730 mm2 of annulus). The pipe's end ring
# encloses 17923 mm2 and the rod - a second solid of the file - shows a 12223 mm2 end disc inside it;
# neither measure fit the declaration, and cfMesh and snappy both refused the part before meshing
# ("declared port 'inlet' matches none of the remaining flat faces", lab baseline 2026-10-04).
from __future__ import annotations

import math

import pytest

pytest.importorskip("OCP.STEPControl")


def _annular_step(path, *, bore=150.0, wall=6.0, rod=120.0, length=600.0):
    from OCP.BRep import BRep_Builder
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0))
    outer = BRepPrimAPI_MakeCylinder(ax, bore / 2 + wall, length).Shape()
    hole = BRepPrimAPI_MakeCylinder(ax, bore / 2, length).Shape()
    pipe = BRepAlgoAPI_Cut(outer, hole).Shape()
    centre = BRepPrimAPI_MakeCylinder(ax, rod / 2, length).Shape()
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    b.Add(comp, pipe)
    b.Add(comp, centre)
    w = STEPControl_Writer()
    w.Transfer(comp, STEPControl_AsIs)
    w.Write(str(path))
    return path


def _prepared():
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
    )
    interp = GeometryInterpretation(interpretation_id="t", owner_id="t", geometry_source_id="t",
                                    unit=LengthUnit.millimetre, scale_to_metres=0.001,
                                    basis=ResolutionBasis.file_declared, evidence="t")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def test_the_annulus_around_a_centre_body_is_the_port(tmp_path):
    from meshpipeline.cad.cad_tessellate import tessellate_internal
    from meshpipeline.engines.port_binding import bind_intake, declaration_targets
    step = _annular_step(tmp_path / "annular.step")
    patches = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0],
                "diameter_mm": 150.0, "inner_diameter_mm": 120.0},
               {"name": "outlet", "type": "outlet", "near_mm": [600, 0, 0],
                "diameter_mm": 150.0, "inner_diameter_mm": 120.0},
               {"name": "wall", "type": "wall"}]
    t = tessellate_internal(step, tmp_path / "out", prepared=_prepared(),
                            declared_ports=declaration_targets(patches), fluid_solid=False)
    inlet = t["openings"]["inlet"]
    # the opening is the bore, and the rod's end disc fills the middle of it
    assert inlet["opening"]["area"] == pytest.approx(math.pi * 0.075 ** 2, rel=0.01)
    assert inlet["opening"]["filled"] == pytest.approx(math.pi * 0.06 ** 2, rel=0.01)
    t2, wall, note = bind_intake(t, patches)
    assert set(t2["openings"]) == {"inlet", "outlet"} and wall == "wall", note


def test_a_ring_with_nothing_in_its_bore_is_matched_as_before():
    from meshpipeline.cad.cad_tessellate import select_declared_openings
    # (idx, own area, centroid, opening): a thin end ring around an empty 150 mm bore
    ring = (0, 0.0029, (0.0, 0.0, 0.0), {"area_m2": math.pi * 0.075 ** 2, "centroid": [0, 0, 0],
                                        "wh_m": [0.15, 0.15]})
    far = (1, 0.0029, (0.6, 0.0, 0.0), {"area_m2": math.pi * 0.075 ** 2, "centroid": [0.6, 0, 0],
                                        "wh_m": [0.15, 0.15]})
    port = {"name": "inlet", "area_m2": math.pi * 0.075 ** 2, "near_m": (0, 0, 0), "d_m": 0.15}
    assert select_declared_openings([ring, far], [port]) == [0]
    # a declaration of the annulus alone (no body in this bore) still finds nothing to match
    annulus = dict(port, area_m2=math.pi * (0.075 ** 2 - 0.06 ** 2))
    with pytest.raises(ValueError):
        select_declared_openings([ring, far], [annulus])
