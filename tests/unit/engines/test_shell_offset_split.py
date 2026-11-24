# Responsibility: Verify SHMESH-11h - before SHMESH-11's seam cut, an offset face is re-made as a B-spline only when it
# has a seam (it is then cut there, as SHMESH-11g does) or its base surface is small; an open offset of a large skin
# goes on as it is (converting one took minutes on a stamped panel, the shell mesh sat in it until its 15-minute stop).
# Boundaries: OCP faces built here; split_closed on a written brep, with the B-spline conversion counted from outside.
from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from OCP import BRepBuilderAPI as BB  # noqa: E402
from OCP.BRepTools import BRepTools  # noqa: E402
from OCP.Geom import Geom_CylindricalSurface, Geom_OffsetSurface, Geom_RectangularTrimmedSurface  # noqa: E402
from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface  # noqa: E402
from OCP.GeomConvert import GeomConvert  # noqa: E402
from OCP.gp import gp_Ax3, gp_Pnt  # noqa: E402
from OCP.TColgp import TColgp_Array2OfPnt  # noqa: E402

from meshpipeline.engines.gmsh import shell_adapter as SA  # noqa: E402


def _skin(n: int):
    """An open, gently waved B-spline skin of n x n poles, 100 x 100 mm."""
    pts = TColgp_Array2OfPnt(1, n, 1, n)
    for i in range(1, n + 1):
        for j in range(1, n + 1):
            x, y = 100.0 * (i - 1) / (n - 1), 100.0 * (j - 1) / (n - 1)
            pts.SetValue(i, j, gp_Pnt(x, y, 2.0 * math.sin(x / 15.0) * math.cos(y / 20.0)))
    return GeomAPI_PointsToBSplineSurface(pts).Surface()


def _face(surf, u=(0.0, 1.0), v=(0.0, 1.0)):
    return BB.BRepBuilderAPI_MakeFace(surf, u[0], u[1], v[0], v[1], 1e-6).Face()


def _write(tmp: Path, f) -> Path:
    p = tmp / "one.brep"
    BRepTools.Write_s(f, str(p))
    return p


@pytest.fixture
def converts(monkeypatch):
    seen = []
    real = BB.BRepBuilderAPI_NurbsConvert

    def counting(*a, **k):
        seen.append(1)
        return real(*a, **k)

    import importlib

    # (both the package attribute and the module the mesher imports from at call time)
    for mod in {id(BB): BB, id(m_ := importlib.import_module("OCP.BRepBuilderAPI")): m_}.values():
        monkeypatch.setattr(mod, "BRepBuilderAPI_NurbsConvert", counting)
    return seen


def test_an_open_offset_of_a_large_skin_is_not_converted(tmp_path, converts, monkeypatch):
    base = _skin(30)
    monkeypatch.setattr(SA, "OFFSET_OPEN_POLES", base.NbUPoles() * base.NbVPoles() - 1)   # the skin just over the cap
    surf = Geom_OffsetSurface(base, 1.0)
    assert not SA._offset_worth(_face(surf))
    SA.split_closed(_write(tmp_path, _face(surf)), ["a"], tmp_path / "out.brep")
    assert converts == []


def test_an_open_offset_of_a_small_skin_is_converted_as_before(tmp_path, converts, monkeypatch):
    base = _skin(6)
    monkeypatch.setattr(SA, "OFFSET_OPEN_POLES", base.NbUPoles() * base.NbVPoles())     # at the cap: converted
    surf = Geom_OffsetSurface(base, 1.0)
    assert SA._offset_worth(_face(surf))


def test_a_closed_offset_face_is_converted_and_cut_at_its_seam(tmp_path, converts, monkeypatch):
    monkeypatch.setattr(SA, "OFFSET_OPEN_POLES", 0)    # (a closed face converts whatever its size)
    # (an offset of a closed B-spline: an offset of an elementary surface is written back as that surface)
    ring = GeomConvert.SurfaceToBSplineSurface_s(
        Geom_RectangularTrimmedSurface(Geom_CylindricalSurface(gp_Ax3(), 10.0), 0.0, 2 * math.pi, 0.0, 20.0))
    ring.SetUPeriodic()                                 # (a seam, as a closed face read from a STEP file has)
    lo_u, hi_u, lo_v, hi_v = ring.Bounds()
    f = _face(Geom_OffsetSurface(ring, 1.0), (lo_u, hi_u), (lo_v, hi_v))
    assert SA._offset_worth(f)
    owners = SA.split_closed(_write(tmp_path, f), ["a"], tmp_path / "out.brep")
    assert converts and owners is not None and len(owners) >= 2
