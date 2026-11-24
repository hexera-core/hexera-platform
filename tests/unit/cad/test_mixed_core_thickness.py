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
