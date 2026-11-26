# Responsibility: Verify INS-24 on built parts: a ring adapter whose upper wall is notched down to its foot (the
# batch-4 adapter with a tall tab) has one passage with one mouth at each end - the notched wall's outline (one
# loop round its foot, up the notch, round its top) is no opening's rim - and the check proposes two openings, not
# three; an equal tee's saddle-shaped branch hole stays a mouth.
# Boundaries: real OpenCASCADE solids written to STEP; the map on the skin the geometry check draws and the check's
# proposal; no mesher, no model.
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

try:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
except Exception:  # noqa: BLE001
    pytest.skip("OCP not available", allow_module_level=True)

from tests.unit.cad.test_solid_stub_ends import _write

from meshpipeline.cad import inside_space as ins
from meshpipeline.cad.open_ends import surface

_REF = SimpleNamespace(owner_id="o", source_id="s", suffix_hint=".step", original_filename="part.step")


def _cyl(r, h, p=(0.0, 0.0, 0.0), d=(0.0, 0.0, 1.0)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p), gp_Dir(*d)), r, h).Shape()


def _ring(r0, r1, z0, z1):
    return BRepAlgoAPI_Cut(_cyl(r1, z1 - z0, (0, 0, z0)), _cyl(r0, z1 - z0 + 2, (0, 0, z0 - 1))).Shape()


def _adapter(notch: bool = True):
    """A ring adapter: a 66 mm bore from z = -12 to 1.7, flaring out at 45 degrees to a 73 mm bore at z = 5, and an
    upper wall (4.7 mm thick) on up to z = 20.5; the upper wall notched on -x from its foot to its top (a tall tab
    is what is left)."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone

    flare = BRepAlgoAPI_Cut(_cyl(41.0, 3.3, (0, 0, 1.7)),
                            BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(0, 0, 1.7), gp_Dir(0, 0, 1)), 33.0, 36.3, 3.3).Shape())
    part = BRepAlgoAPI_Fuse(_ring(33.0, 41.0, -12.0, 1.7), flare.Shape()).Shape()
    part = BRepAlgoAPI_Fuse(part, _ring(36.3, 41.0, 5.0, 20.5)).Shape()
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

    if notch:
        part = BRepAlgoAPI_Cut(part, BRepPrimAPI_MakeBox(gp_Pnt(-45, -20, 5), gp_Pnt(-20, 20, 21)).Shape()).Shape()
    unify = ShapeUpgrade_UnifySameDomain(part, True, True, False)
    unify.Build()
    part = unify.Shape()
    if notch:
        # the notch's inner corners rounded, as drawn: the wall's inside runs smoothly round onto its cut ends
        from OCP.BRep import BRep_Tool
        from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_VERTEX
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopoDS import TopoDS

        fillet = BRepFilletAPI_MakeFillet(part)
        ee = TopExp_Explorer(part, TopAbs_EDGE)
        while ee.More():
            e = TopoDS.Edge_s(ee.Current())
            ee.Next()
            pts = []
            ve = TopExp_Explorer(e, TopAbs_VERTEX)
            while ve.More():
                p = BRep_Tool.Pnt_s(TopoDS.Vertex_s(ve.Current()))
                pts.append((p.X(), p.Y(), p.Z()))
                ve.Next()
            if len(pts) == 2 and all(abs(np.hypot(x, y) - 36.3) < 0.05 and x < 0 for x, y, _z in pts) \
                    and abs(pts[0][2] - pts[1][2]) > 10:
                fillet.Add(2.0, e)
        part = fillet.Shape()
    return part


def _tee():
    """An equal tee: a 20 mm bore 100 mm long along x, a branch of the same bore up from its middle."""
    main = BRepAlgoAPI_Cut(_cyl(12.0, 100.0, (-50, 0, 0), (1, 0, 0)), _cyl(10.0, 102.0, (-51, 0, 0), (1, 0, 0))).Shape()
    branch = _cyl(12.0, 50.0, (0, 0, 0))
    part = BRepAlgoAPI_Fuse(main, branch).Shape()
    return BRepAlgoAPI_Cut(part, _cyl(10.0, 51.0, (0, 0, 0))).Shape()


def _map(shape, tmp: Path):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.cad.stl_io import read_stl_triangles

    facts, skin = gc._scout_exact(_write(shape, tmp / "part.step"), tmp, None, _REF)
    tris = np.asarray(read_stl_triangles(skin), dtype=float).reshape(-1, 3, 3)
    return facts, ins.read_inside(surface(tris))


def test_a_notched_walls_outline_is_no_mouth(tmp_path):
    _facts, m = _map(_adapter(), tmp_path)
    [p] = m.passages()                                     # one passage (before: two, the notch's band cap between)
    z = sorted(round(float(m.mouth(i).centroid[2]) * 1000.0, 1) for i in p.mouths)
    assert z == [-12.0, 1.7], z                             # its two ends: the bore's foot and its top


def test_the_check_proposes_one_opening_per_end(tmp_path):
    facts, _m = _map(_adapter(), tmp_path)
    ports = [o for o in facts["openings"] if o.get("role") in ("inlet", "outlet")]
    assert len(ports) == 2, ports
    assert all(o["diameter_mm"] == pytest.approx(66.0, rel=0.02) for o in ports), ports


def test_an_equal_tees_saddle_hole_stays_a_mouth(tmp_path):
    _facts, m = _map(_tee(), tmp_path)
    [p] = m.passages()
    assert len(p.mouths) == 3


class _OpenAir:
    """A stand-in map: every point asked about is open air, its cube 0.1 mm."""
    h = 1e-4

    @staticmethod
    def where(points):
        return ["out" for _ in points]


def _flat(k: int, d_mm: float, z_mm: float) -> dict:
    return {"face": k, "centroid_m": [0.0, 0.0, z_mm / 1000.0], "centroid_mm": [0.0, 0.0, z_mm],
            "normal": [0.0, 0.0, 1.0], "diameter_mm": d_mm, "area_mm2": 3.14159 * d_mm * d_mm / 4.0,
            "width_mm": d_mm, "height_mm": d_mm}


@pytest.mark.parametrize(("outer_mm", "expected"), [(5.9, 1), (20.0, 0)])
def test_a_mouth_at_a_counterbores_floor_opens_at_its_top(outer_mm, expected):
    """A threaded counterbore's own rim is no clean loop, so the map's mouth is the bore's step at its floor (GL45
    cap: 3 mm at z = 3, its port 5.9 mm at z = 9 with open air between): the counterbore's top hole is the opening.
    A recess far wider than the hole in its floor is no counterbore: the mouth stays the hole it is."""
    from meshpipeline.application.geometry_check import flat_holes_by_map

    facts = {"flat_holes": [_flat(0, 3.0, 3.0), _flat(1, outer_mm, 9.0)]}
    holes = [{"passage": True, "centroid_m": [0.0, 0.0, 0.003], "normal": [0.0, 0.0, 1.0], "diameter_mm": 3.0}]
    flat_holes_by_map(facts, holes, _OpenAir())
    assert holes[0]["flat_hole"] == expected
