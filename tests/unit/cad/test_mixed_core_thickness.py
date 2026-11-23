# Responsibility: Verify MIXED-11, A PIECE IS AS THICK AS ITS DEEPEST POINT: the mixed split measures a left-over
# piece MIXED-1b turns down (twice its volume over its area under half the thinnest wall) by its deepest inside point;
# a compact pad is a thick core, a thin ring round a hole still is not.
# Boundaries: shapes built in the test (tests/fea_fixtures, tests/shell_fixtures); the real extraction and split.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402
from tests.fea_fixtures import _box, _cut, _cyl  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad import mixed_split as mx  # noqa: E402


def test_a_pieces_thickness_is_its_deepest_point():
    ring = _cut(_cyl(0, 0, 0, 10.6, 4.0), _cyl(0, 0, -1, 10.0, 6.0))     # a 0.6 mm ring, 4 mm high
    pad = _box(0, 0, 0, 10, 10, 2)
    cube = _box(0, 0, 0, 3, 3, 3)
    assert mx.thickness(ring) <= 0.8
    assert mx.thickness(pad) == pytest.approx(2.0, abs=0.3)
    assert mx.thickness(cube) == pytest.approx(3.0, abs=0.4)
    # what MIXED-1b reads: the pad and the cube far thinner than they are
    for s, t in ((pad, 2.0), (cube, 3.0)):
        assert 2 * ms._volume(s) / ms._area(s) < 0.75 * t


def _split(shape):
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    mids = {m["id"]: m for m in rec["mids"]}
    kept = [(f, float(mids[o]["thickness_mm"]), float(mids[o].get("offset_mm", 0.0)))
            for f, o in zip(ms._faces(surface), rec["surface_faces"], strict=True)]
    faces = ms._faces(shape)
    thick = [faces[k] for k, r in enumerate(rec["faces"]) if r["role"] in ("thick", "solid")]
    return rec, mx.split_body(mx._solids(shape)[0], kept, thick, scale=1.0)


def _stud_split():
    """A 100 x 60 x 2 mm plate with a 2.5 x 2.5 x 3 mm stud on it, split with the plate's mid-face (z = 1) as its one
    shell wall and the stud's faces as the thick faces."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
    from tests.fea_fixtures import _fuse

    body = _fuse(_box(0, 0, 0, 100, 60, 2), _box(40, 25, 2, 2.5, 2.5, 3))
    mid = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 1), gp_Dir(0, 0, 1)), 0.0, 100.0, 0.0, 60.0).Face()
    stud = [f for f in ms._faces(body) if ms._box(f)[1][2] > 2.0 + 1e-6]
    return mx.split_body(mx._solids(body)[0], [(mid, 2.0, 0.0)], stud, scale=1.0)


def test_a_small_stud_on_a_plate_is_a_thick_core(monkeypatch):
    """MIXED-1b read the stud as 0.88 mm thin against its 1 mm floor (half the 2 mm wall): no core, and the split said
    "no thick region is left once the shell walls are taken out" (so on cen_s0143's two pads). It is 2.5 mm thick: a
    core, cut out with the plate round it."""
    split = _stud_split()
    assert split.ok and split.cores == 1, split.why
    monkeypatch.setattr(mx, "CORE_THICKNESS", False)
    before = _stud_split()
    assert not before.ok and before.why == "no thick region is left once the shell walls are taken out"


def test_a_core_mixed_1b_takes_is_taken_as_before(monkeypatch):
    shape = sf.plate_with_boss()
    _rec, split = _split(shape)
    monkeypatch.setattr(mx, "CORE_THICKNESS", False)
    _rec, before = _split(shape)
    assert split.ok and before.ok and split.cores == before.cores == 1
    assert split.solid_volume == pytest.approx(before.solid_volume, rel=1e-12)


def test_a_thickness_reading_past_its_bound_is_not_known():
    # MIXED-19: pub_0018's left-over piece cost about a second a reading and ran past 7 minutes
    cube = _box(0, 0, 0, 3, 3, 3)
    assert mx.thickness(cube, limit_s=0.0) is None
    assert mx.thickness(cube) == pytest.approx(3.0, abs=0.4)


def test_a_split_whose_thickness_reading_runs_out_stops_and_says_so(monkeypatch):
    monkeypatch.setattr(mx, "CORE_THICKNESS_S", 0.0)
    split = _stud_split()
    assert not split.ok and split.why == "a left-over piece's thickness could not be measured in 0 s"


def test_a_split_stops_at_the_first_left_over_past_its_share_before_the_costly_readings(monkeypatch):
    """MIXED-19: a plate with two studs, one read thick, the other (larger) not: the larger is read first, is left over
    past LEFT_OVER_MAX, and the split stops there - the thick stud's MIXED-11 reading is never made."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt
    from tests.fea_fixtures import _fuse

    body = _fuse(_fuse(_box(0, 0, 0, 100, 60, 2), _box(40, 25, 2, 2.5, 2.5, 3)), _box(10, 10, 2, 4, 4, 3))
    mid = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 1), gp_Dir(0, 0, 1)), 0.0, 100.0, 0.0, 60.0).Face()
    stud = [f for f in ms._faces(body) if ms._box(f)[1][2] > 2.0 + 1e-6 and ms._box(f)[0][0] >= 40.0 - 1e-6]

    def never(*a, **k):
        raise AssertionError("read")

    monkeypatch.setattr(mx, "LEFT_OVER_MAX", 0.0)
    monkeypatch.setattr(mx, "thickness", never)
    split = mx.split_body(mx._solids(body)[0], [(mid, 2.0, 0.0)], stud, scale=1.0)
    assert not split.ok and "of the metal is neither in a shell wall nor in a thick region" in split.why, split.why


def _far_wall():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    return BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 1000), gp_Dir(0, 0, 1)), 0.0, 1.0, 0.0, 1.0).Face()


def test_a_regions_metal_is_never_more_than_the_body():
    """MIXED-17: cen_r0553's one region (682 of 747 faces) read 120% of the part's volume - grid cells of ~3.5 mm on
    2 mm walls, each counted whole. A thin plate whose every face is the region, its grid coarser than its wall, now
    reads its own exact volume."""
    plate = _box(0, 0, 0, 100, 100, 2)
    got = ms._thick_measure(plate, [ms._faces(plate)], [(_far_wall(), 0.01)], mm=1.0, work=[10 ** 6])
    assert got[0]["volume_mm3"] == pytest.approx(ms._volume(plate), rel=1e-3)
    assert got[0]["volume_mm3"] <= ms._volume(plate) * (1 + 1e-9)


def test_a_flat_regions_size_is_its_metals_not_its_faces():
    """MIXED-17: pub_0018's region (one flat annulus face) was told as "60.5 x 60.5 x 2e-07 mm" for 5,123 mm3 of
    metal: the size was its faces' box. It is now the box of the metal counted for it."""
    block = _box(0, 0, 0, 10, 10, 4)
    top = [f for f in ms._faces(block) if ms._box(f)[0][2] > 4.0 - 1e-6]
    got = ms._thick_measure(block, [top], [(_far_wall(), 4.0)], mm=1.0, work=[10 ** 6])
    assert got[0]["volume_mm3"] == pytest.approx(400.0, rel=1e-3)
    assert got[0]["size_mm"][0] == pytest.approx(10.0, abs=2.5) and got[0]["size_mm"][2] > 1.0
