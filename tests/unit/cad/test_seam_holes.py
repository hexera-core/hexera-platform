# Responsibility: Verify INS-21 - a side hole that crosses a tube's seam (no inner wire of the tube's faces) is
# found and sealed into the wall like any other undeclared hole, so the bore no longer leaks through it; the
# tube's end circles are never taken for holes.
# Boundaries: real OpenCASCADE solids written to STEP; the B-rep internal staging (cad_tessellate); no mesher.
from __future__ import annotations

from pathlib import Path

import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_flat_holes import _write
from tests.unit.cad.test_scout import _prepared

from meshpipeline.cad.cad_tessellate import seam_holes, tessellate_internal
from meshpipeline.engines.port_binding import declaration_targets


def _cyl(r, h, base=(0.0, 0.0, 0.0), d=(0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*base), gp_Dir(*d)), r, h).Shape()


def _tube(side: str):
    """A 20 mm tube (bore 16 mm), 60 mm long on z, with a 4 mm side hole half way along: drilled along x, it
    crosses the cylinders' seam (OpenCASCADE puts it at +x); along y it does not."""
    tube = BRepAlgoAPI_Cut(_cyl(10.0, 60.0), _cyl(8.0, 62.0, (0, 0, -1))).Shape()
    d = (1, 0, 0) if side == "x" else (0, 1, 0)
    return BRepAlgoAPI_Cut(tube, _cyl(2.0, 12.0, (0, 0, 30.0), d)).Shape()


PORTS = [{"name": "inlet", "type": "inlet", "diameter_mm": 16.0, "near_mm": [0.0, 0.0, 0.0]},
         {"name": "outlet", "type": "outlet", "diameter_mm": 16.0, "near_mm": [0.0, 0.0, 60.0]},
         {"name": "wall", "type": "wall"}]


def _stage(side: str, tmp: Path):
    path = _write(_tube(side), tmp / f"tube_{side}.step")
    return tessellate_internal(path, tmp / f"st_{side}", prepared=_prepared(path), fluid_solid=False,
                               declared_ports=declaration_targets(PORTS))


@pytest.mark.parametrize("side", ["x", "y"])
def test_a_side_hole_is_sealed_whether_or_not_it_crosses_the_seam(tmp_path, side):
    t = _stage(side, tmp_path)
    sealed = t["sealed"]["undeclared_openings"]
    near = [s for s in sealed if abs(s["centroid"][2] * 1000.0 - 30.0) < 3.0]
    assert near, (side, sealed)
    assert sorted(round(r["centroid"][2] * 1000.0) for r in t["openings"].values()) == [0, 60]


def test_the_seam_holes_of_a_tube_are_its_side_hole_never_its_end_circles():
    shape = _tube("x")
    found = []
    ex = TopExp_Explorer(shape, TopAbs_FACE)
    while ex.More():
        found.append(len(seam_holes(TopoDS.Face_s(ex.Current()))))
        ex.Next()
    # the outer and the inner cylinder each hold the hole; the hole's own bore and the flat ends hold none
    assert sorted(found)[-2:] == [1, 1] and sum(found) == 2, found
    # a hole clear of the seam is an inner wire, never a seam hole
    clear = []
    ex = TopExp_Explorer(_tube("y"), TopAbs_FACE)
    while ex.More():
        clear.append(len(seam_holes(TopoDS.Face_s(ex.Current()))))
        ex.Next()
    assert sum(clear) == 0, clear


def test_a_hole_clear_of_the_seam_is_sealed_once_beside_one_across_it(tmp_path):
    # both side holes in one tube: each rim sealed exactly once (an inner wire is never read again as a seam hole)
    tube = BRepAlgoAPI_Cut(_tube("x"), _cyl(2.0, 12.0, (0, 0, 15.0), (0, 1, 0))).Shape()
    path = _write(tube, tmp_path / "two.step")
    t = tessellate_internal(path, tmp_path / "st2", prepared=_prepared(path), fluid_solid=False,
                            declared_ports=declaration_targets(PORTS))
    rims = [tuple(round(v * 1000.0, 1) for v in s["centroid"]) for s in t["sealed"]["undeclared_openings"]]
    assert len(rims) == 4 and len(set(rims)) == 4, rims
    assert sorted(round(c[2]) for c in rims) == [15, 15, 30, 30], rims
