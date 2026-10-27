# Responsibility: Pin two of the plan's definitions (27 Oct): a BORE - the inside of a tube, sleeve or ring, sharing its axis with the outside - is the part's main wall, not a stress-raising hole (PLAN-fea-4); and two parts that MEET TANGENTIALLY in the fuse (a round of one running onto the other's face) are tied from the start, never fused into a crack no mesher can fill (PLAN-fea-17).
# Boundaries: parts built here with OpenCASCADE and measured or staged in-process; nothing is meshed.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from meshpipeline.cad import fea_assembly as A  # noqa: E402
from meshpipeline.cad.fea_measure import measure_cad  # noqa: E402


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    return A.solids(BRepAlgoAPI_Cut(a, b).Shape())[0]


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


# THE DEFECT (job bf863215, 26 Oct): a 533 mm tube's bore was a "hole" hot spot - 3 elements through its
# 1.2 mm wall along its whole length, 2.6M of the assembly's 3.9M elements; a 0.5 mm-walled cryostat liner's
# bore the same way, 158M.

def test_a_tubes_bore_is_its_wall_not_a_hole():
    tube = _cut(_cyl(10.0, 200.0), _cyl(8.5, 202.0, at=(0, 0, -1)))
    rec = measure_cad(tube, mm_per_unit=1.0)
    assert _holes(rec) == []
    assert len(rec["bores"]) == 1 and rec["bores"][0]["radius_mm"] == pytest.approx(8.5, rel=1e-3)
    assert rec["bores"][0]["outer_radius_mm"] == pytest.approx(10.0, rel=1e-3)
    # the wall rule still holds it: 2 elements through the 1.5 mm wall
    walls = [r.get("thickness_min_mm") for r in rec["faces"].values() if r.get("thickness_min_mm")]
    assert min(walls) == pytest.approx(1.5, rel=0.05)


def test_a_hole_through_a_plate_is_still_a_hole():
    plate = _cut(_box(60.0, 40.0, 5.0), _cyl(4.0, 7.0, at=(30, 20, -1)))
    rec = measure_cad(plate, mm_per_unit=1.0)
    assert len(_holes(rec)) == 1 and not rec.get("bores")


def test_a_lugs_hole_by_its_round_end_is_still_a_hole():
    """A lug's hole shares its axis with the lug's half-round end - the classic stress raiser, never a bore."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    lug = A.solids(BRepAlgoAPI_Fuse(_box(40.0, 20.0, 6.0, at=(0, -10, 0)), _cyl(10.0, 6.0, at=(40, 0, 0))).Shape())[0]
    rec = measure_cad(_cut(lug, _cyl(4.0, 8.0, at=(40, 0, -1))), mm_per_unit=1.0)
    assert len(_holes(rec)) == 1 and not rec.get("bores")


def test_a_washers_and_a_rings_hole_short_against_its_wall_is_still_a_hole():
    washer = _cut(_cyl(12.5, 1.0), _cyl(4.2, 3.0, at=(0, 0, -1)))
    rec = measure_cad(washer, mm_per_unit=1.0)
    assert len(_holes(rec)) == 1 and not rec.get("bores")


def test_a_cross_hole_through_a_tube_is_still_a_hole():
    tube = _cut(_cyl(10.0, 200.0), _cyl(8.5, 202.0, at=(0, 0, -1)))
    crossed = _cut(tube, _cyl(2.0, 30.0, at=(-15, 0, 100), axis=(1, 0, 0)))
    rec = measure_cad(crossed, mm_per_unit=1.0)
    assert len(rec["bores"]) == 1
    # the cross hole crosses both sides of the wall: two holes, both still stress raisers
    assert len(_holes(rec)) == 2 and all(h["radius_mm"] == pytest.approx(2.0, rel=1e-3) for h in _holes(rec))


def test_a_round_lying_tangent_on_a_face_meets_it_tangentially(tmp_path):
    """A rod lying on a block touches it along a line, with a crack of no angle either side."""
    block = _box(40.0, 40.0, 10.0)
    rod = _cyl(5.0, 30.0, at=(5, 20, 15), axis=(1, 0, 0))
    fused = A.fuse([block, rod])
    assert A.tangent_meets(fused) == [(1, 2)]
    side = A.stage(A.compound([block, rod]), A.BONDED, tmp_path)
    assert side["mode"] == A.PAIR_TIE and side["tangent_meets"] == [[1, 2]]
    assert any("meet tangentially" in n for n in side["notes"])


def test_parts_meeting_face_to_face_are_fused_as_before(tmp_path):
    a = _box(20.0, 20.0, 10.0)
    b = _box(10.0, 10.0, 5.0, at=(5, 5, 10))
    assert A.tangent_meets(A.fuse([a, b])) == []
    side = A.stage(A.compound([a, b]), A.BONDED, tmp_path)
    assert side["mode"] == A.PAIR_BONDED and "tangent_meets" not in side
