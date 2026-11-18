# Responsibility: Pin two of the plan's definitions (27 Oct): a CHANNEL running the whole length of a long body (an
# extrusion's centre channel, a drilled shaft) is part of its section, not a stress-raising hole (PLAN-fea-4); and
# RELIEF - lettering, a logo or a shallow texture standing off a flat face by no more than 1/1000 of its part - is
# shape only, with no wall, corner or fillet zone of its own (PLAN-fea-12).
# Boundaries: parts built here with OpenCASCADE and measured (and planned) in-process; nothing is meshed.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.cad.fea_measure import measure_cad  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    return A.solids(BRepAlgoAPI_Cut(a, b).Shape())[0]


def _fuse(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    return A.solids(BRepAlgoAPI_Fuse(a, b).Shape())[0]


def _cyl(r, h, at=(0.0, 0.0, 0.0), axis=(0.0, 0.0, 1.0)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*at), gp_Dir(*axis)), r, h).Shape()


def _box(x, y, z, at=(0.0, 0.0, 0.0)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(*at), x, y, z).Shape()


def _holes(rec):
    return [h for h in rec["hot_spots"] if h["type"] == "hole"]


# THE DEFECTS (corpus planning sweep, 27 Oct): a 10 mm slotted extrusion's 2.5 mm centre channel, 1.5 m long, was
# a "hole" (3 elements through its 0.25 mm wall along the whole length), 87% of a 57M plan; a 15 mm printer
# part's 0.05 mm raised lettering asked for 0.016 mm corners and 2 elements through 0.06 mm strokes, 78% of 27M.

def test_a_channel_along_a_long_bar_is_part_of_its_section():
    bar = _cut(_box(10.0, 10.0, 300.0, at=(-5, -5, 0)), _cyl(1.25, 302.0, at=(0, 0, -1)))
    rec = measure_cad(bar, mm_per_unit=1.0)
    assert _holes(rec) == []
    assert len(rec["bores"]) == 1 and rec["bores"][0]["channel"] is True
    assert rec["bores"][0]["body_length_mm"] == pytest.approx(300.0, rel=1e-3)
    assert any("channel" in n for n in rec["notes"])


def test_a_hole_across_a_long_bar_is_still_a_hole():
    """A pin hole across a link: the body is long, but not along the hole."""
    bar = _cut(_box(10.0, 10.0, 300.0, at=(-5, -5, 0)), _cyl(2.0, 12.0, at=(-6, 0, 150), axis=(1, 0, 0)))
    rec = measure_cad(bar, mm_per_unit=1.0)
    assert len(_holes(rec)) == 1 and not rec.get("bores")


def test_a_short_hole_along_a_long_bar_is_still_a_hole():
    """A tapped hole in a bar's end runs along the bar, but not its length."""
    bar = _cut(_box(10.0, 10.0, 300.0, at=(-5, -5, 0)), _cyl(1.25, 21.0, at=(0, 0, -1)))
    rec = measure_cad(bar, mm_per_unit=1.0)
    assert len(_holes(rec)) == 1 and not rec.get("bores")


def test_a_deep_hole_through_a_block_is_still_a_hole():
    """Through a block as long as it is wide: the hole runs its length, but the block is no bar."""
    block = _cut(_box(40.0, 40.0, 60.0, at=(-20, -20, 0)), _cyl(3.0, 62.0, at=(0, 0, -1)))
    rec = measure_cad(block, mm_per_unit=1.0)
    assert len(_holes(rec)) == 1 and not rec.get("bores")


def _lettered(height: float, *, sunk: bool = False):
    """A 60 x 40 x 10 plate with a raised (or sunk) 'T' of strokes 0.4 mm wide on its top."""
    plate = _box(60.0, 40.0, 10.0)
    z = 10.0 - height if sunk else 10.0
    bar = _fuse(_box(6.0, 0.4, height, at=(20, 20, z)), _box(0.4, 6.0, height, at=(22.8, 14.4, z)))
    return _cut(plate, bar) if sunk else _fuse(plate, bar)


@pytest.mark.parametrize("sunk", [False, True])
def test_lettering_under_a_thousandth_of_the_part_is_relief(sunk):
    rec = measure_cad(_lettered(0.05, sunk=sunk), mm_per_unit=1.0)   # the plate is 73 mm across: 0.073 mm
    assert len(rec["relief"]) == 1
    g = rec["relief"][0]
    assert g["height_mm"] == pytest.approx(0.05, rel=1e-3) and g["raised"] is (not sunk)
    # its corners are none of the part's hot spots, and its strokes are not the part's thinnest wall
    assert not any(set(h["faces"]) & set(g["faces"]) for h in rec["hot_spots"])
    assert rec["thickness"]["min_mm"] > 1.0
    zs = P.zones(rec)
    rel = [z for z in zs if z["kind"] == "relief"]
    assert len(rel) == 1 and rel[0]["size_m"] == pytest.approx(0.05e-3, rel=1e-3)
    assert not any(z["kind"] in ("wall", "corner", "round", "wall_spot") and set(z["faces"]) & set(g["faces"])
                   for z in zs)


def test_lettering_taller_than_a_thousandth_of_the_part_keeps_its_rules():
    rec = measure_cad(_lettered(0.5), mm_per_unit=1.0)
    assert not rec.get("relief")
    assert any(h["type"] == "sharp_corner" for h in rec["hot_spots"])


def test_a_thin_sheet_is_never_relief():
    """A 0.05 mm foil: its other side faces away, so it is a wall, held to 2 elements through it."""
    rec = measure_cad(_box(60.0, 40.0, 0.05), mm_per_unit=1.0)
    assert not rec.get("relief")
    assert rec["thickness"]["min_mm"] == pytest.approx(0.05, rel=0.05)


def test_a_shallow_pad_on_a_face_is_relief_but_a_step_at_its_edge_is_not():
    """A 0.03 mm pad wholly on the top face is relief; one flush with the plate's side touches the side as
    well (a step in the part's outline), so it is not."""
    plate = _fuse(_box(60.0, 40.0, 10.0), _box(59.94, 39.94, 0.03, at=(0.03, 0.03, 10.0)))
    assert len(measure_cad(plate, mm_per_unit=1.0).get("relief") or []) == 1
    flush = _fuse(_box(60.0, 40.0, 10.0), _box(60.0, 20.0, 0.03, at=(0.0, 0.0, 10.0)))
    assert not measure_cad(flush, mm_per_unit=1.0).get("relief")


def test_a_small_fillet_at_a_ribs_root_is_never_relief():
    """A 0.05 mm fillet where a rib meets the plate lies within the slab, but touches the rib: a stress
    raiser, kept."""
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    ribbed = _fuse(_box(60.0, 40.0, 10.0), _box(2.0, 40.0, 8.0, at=(29.0, 0.0, 10.0)))
    mk = BRepFilletAPI_MakeFillet(ribbed)
    ex = TopExp_Explorer(ribbed, TopAbs_EDGE)
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    while ex.More():
        e = TopoDS.Edge_s(ex.Current())
        c = BRepAdaptor_Curve(e)
        p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
        if abs(p.Z() - 10.0) < 1e-6 and min(abs(p.X() - 29.0), abs(p.X() - 31.0)) < 1e-6:
            mk.Add(0.05, e)
        ex.Next()
    rec = measure_cad(A.solids(mk.Shape())[0], mm_per_unit=1.0)
    assert not rec.get("relief")
    assert sum(1 for h in rec["hot_spots"] if h["type"] == "fillet") == 2
