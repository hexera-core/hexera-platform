# Responsibility: Hold the shell mesher's geometry healing (engines/gmsh/shell_adapter.py: the sewn surface with some
# faces re-made, fused back) to parts of KNOWN geometry: only the re-made faces and the faces they can touch are fused
# again (SHMESH-30), and the surface comes back as joined as the whole fuse makes it.
# Boundaries: OpenCASCADE only; no gmsh.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import shell_fixtures as sf  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.engines.gmsh import shell_adapter as sa  # noqa: E402


def _read(path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    shape = TopoDS_Shape()
    BRepTools.Read_s(shape, str(path), BRep_Builder())
    return shape


def _free(shape) -> float:
    return sum(ms._length(e) for e in ms._free_edge_list(shape))


def _surface(tmp_path, shape):
    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    ms._write(rec, surface, tmp_path / "x")
    return tmp_path / "x" / ms.SURFACE_FILE, list(rec["surface_faces"])


@pytest.mark.parametrize("name", ["tube_on_plate", "pipe_branch", "duct_elbow"])
def test_the_closed_faces_cut_and_fused_back_locally_are_the_whole_fuses_surface(name, tmp_path, monkeypatch):
    brep, owners = _surface(tmp_path, getattr(sf, name)())
    local = sa.split_closed(brep, owners, tmp_path / "local.brep")
    monkeypatch.setattr(sa, "LOCAL_REBUILD", False)
    whole = sa.split_closed(brep, owners, tmp_path / "whole.brep")
    a, b = _read(tmp_path / "local.brep"), _read(tmp_path / "whole.brep")
    # the same faces, each its mid-face's, the same area, and no more free edge than the sewn surface had
    assert sorted(local) == sorted(whole)
    assert ms._area(a) == pytest.approx(ms._area(b), rel=1e-9)
    assert _free(a) == pytest.approx(_free(b), rel=1e-6, abs=1e-9)
    assert _free(a) <= _free(_read(brep)) * (1 + 1e-6) + 1e-9


def test_a_face_no_re_made_face_can_touch_is_put_back_as_it_was(tmp_path):
    # SHMESH-30: a plate far from the tube's closed face is not fused again: the very same face comes back
    from tests.fea_fixtures import _box, _fuse

    shape = _fuse(sf.tube_on_plate(), _box(400, 0, 0, 100, 100, 5))
    brep, owners = _surface(tmp_path, shape)
    before = ms._faces(_read(brep))
    far = [f for f in before if ms._box(f)[0][0] >= 399.0]
    assert far
    sa.split_closed(brep, owners, tmp_path / "local.brep")
    after = ms._faces(_read(tmp_path / "local.brep"))
    for f in far:
        lo, hi = ms._box(f)
        assert any(((ms._box(g)[0] - lo) ** 2).sum() < 1e-18 and ((ms._box(g)[1] - hi) ** 2).sum() < 1e-18
                   and abs(ms._area(g) - ms._area(f)) < 1e-9 for g in after)


def test_a_local_fuse_that_leaves_a_crack_is_not_used(tmp_path):
    # the guard: a re-made face that no longer meets its neighbours (moved half a wall off) leaves more free edge
    # than the sewn surface had - the local fuse hands back nothing, and the whole fuse is made instead
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Trsf, gp_Vec

    brep, owners = _surface(tmp_path, sf.tube_on_plate())
    shape = _read(brep)
    before = ms._faces(shape)
    k = max(range(len(before)), key=lambda i: ms._area(before[i]))

    def change(i, f):
        if i != k:
            return None
        tr = gp_Trsf()
        tr.SetTranslation(gp_Vec(0, 0, 2.5))
        return ms._faces(BRepBuilderAPI_Transform(f, tr, True).Shape())

    assert sa._rebuild_local(shape, before, owners, change, {k}, tmp_path / "local.brep") is None
