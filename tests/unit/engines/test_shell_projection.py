# Responsibility: Hold SHMESH-82: the relaxing's projection onto a curved face, made with one projector per face and
# each point asked once, gives exactly - to the last bit - what a projector made afresh for every point gives.
# Boundaries: OpenCASCADE in this process on a made-up B-spline face and a cylinder; no gmsh.
from __future__ import annotations

import math

import numpy as np
import pytest

OCP = pytest.importorskip("OCP")

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace  # noqa: E402
from OCP.Geom import Geom_CylindricalSurface  # noqa: E402
from OCP.GeomAPI import GeomAPI_PointsToBSplineSurface  # noqa: E402
from OCP.gp import gp_Ax3, gp_Pnt  # noqa: E402
from OCP.TColgp import TColgp_Array2OfPnt  # noqa: E402

from meshpipeline.engines.gmsh import shell_adapter as SA  # noqa: E402


def _bspline_face():
    n = 20
    arr = TColgp_Array2OfPnt(1, n, 1, n)
    for i in range(1, n + 1):
        for j in range(1, n + 1):
            arr.SetValue(i, j, gp_Pnt(i * 10.0, j * 10.0, 5 * math.sin(i / 3.0) * math.cos(j / 4.0)))
    surf = GeomAPI_PointsToBSplineSurface(arr).Surface()
    return BRepBuilderAPI_MakeFace(surf, 1e-6).Face()


def _cylinder_face():
    surf = Geom_CylindricalSurface(gp_Ax3(), 25.0)
    return BRepBuilderAPI_MakeFace(surf, 0.0, math.pi, 0.0, 40.0, 1e-6).Face()


@pytest.mark.parametrize("make", [_bspline_face, _cylinder_face])
def test_one_projector_per_face_gives_the_same_points(make, monkeypatch):
    facts = [{"normal_face": make()}]
    rng = np.random.default_rng(7)
    pts = np.column_stack([rng.uniform(5, 200, 300), rng.uniform(5, 200, 300), rng.uniform(-30, 30, 300)])
    nodes = np.arange(300, dtype=np.int64)
    on_face = np.zeros(300, dtype=np.int64)
    on_face[::7] = -1                                   # a flat face's node is left as given
    monkeypatch.setattr(SA, "PROJECT_TREE", False)
    monkeypatch.setattr(SA, "PROJECT_ONCE", False)
    fresh = SA._projector(facts, on_face, 1.0)(nodes, pts)
    monkeypatch.setattr(SA, "PROJECT_ONCE", True)
    once = SA._projector(facts, on_face, 1.0)
    first = once(nodes, pts)
    again = once(nodes[::-1], pts[::-1])[::-1]          # asked again, in another order: from the points asked once
    assert np.array_equal(first, fresh) and np.array_equal(again, fresh)
    assert np.array_equal(first[::7], pts[::7])


@pytest.mark.parametrize("make", [_bspline_face, _cylinder_face])
def test_the_tree_search_finds_the_same_closest_points(make, monkeypatch):
    # SHMESH-82b: OCC tree search, the same closest point to under a micrometre (6.7e-8 mm at worst here)
    facts = [{"normal_face": make()}]
    rng = np.random.default_rng(11)
    pts = np.column_stack([rng.uniform(5, 200, 300), rng.uniform(5, 200, 300), rng.uniform(-30, 30, 300)])
    nodes = np.arange(300, dtype=np.int64)
    on_face = np.zeros(300, dtype=np.int64)
    monkeypatch.setattr(SA, "PROJECT_ONCE", False)
    monkeypatch.setattr(SA, "PROJECT_TREE", False)
    fresh = SA._projector(facts, on_face, 1.0)(nodes, pts)
    monkeypatch.setattr(SA, "PROJECT_ONCE", True)
    monkeypatch.setattr(SA, "PROJECT_TREE", True)
    tree = SA._projector(facts, on_face, 1.0)(nodes, pts)
    assert float(np.abs(tree - fresh).max()) < 1e-6


def test_a_point_is_laid_on_the_surface_from_its_node_not_on_a_far_minimum(monkeypatch):
    # SHMESH-82c: a node on a half cylinder at x = +25 asked to move to a point just past the axis; the global search
    # takes the far wall (x = -25, 0.2 mm nearer the point); laid from the node's own place it stays on its wall
    facts = [{"normal_face": _full_cylinder()}]
    on_face = np.zeros(1, dtype=np.int64)
    pts = np.array([[-0.1, 0.0, 10.0]])
    here = np.array([[25.0, 0.0, 10.0]])
    monkeypatch.setattr(SA, "PROJECT_LOCAL", True)
    proj = SA._projector(facts, on_face, 1.0)
    far = proj(np.zeros(1, dtype=np.int64), pts)
    near = proj(np.zeros(1, dtype=np.int64), pts, here)
    assert far[0, 0] < -24.0 and near[0, 0] > 24.0


def test_laid_from_the_node_the_two_searches_agree(monkeypatch):
    facts = [{"normal_face": _bspline_face()}]
    rng = np.random.default_rng(5)
    n = 200
    here = np.column_stack([rng.uniform(20, 180, n), rng.uniform(20, 180, n), np.zeros(n)])
    monkeypatch.setattr(SA, "PROJECT_ONCE", True)
    monkeypatch.setattr(SA, "PROJECT_LOCAL", False)
    here = SA._projector(facts, np.zeros(n, dtype=np.int64), 1.0)(np.arange(n), here)   # (nodes on the face)
    pts = here + rng.normal(0, 1.0, (n, 3))
    monkeypatch.setattr(SA, "PROJECT_LOCAL", True)
    got = {}
    for tree in (False, True):
        monkeypatch.setattr(SA, "PROJECT_TREE", tree)
        got[tree] = SA._projector(facts, np.zeros(n, dtype=np.int64), 1.0)(np.arange(n), pts, here)
    assert float(np.abs(got[True] - got[False]).max()) < 1e-9


def _full_cylinder():
    from OCP.Geom import Geom_CylindricalSurface
    surf = Geom_CylindricalSurface(gp_Ax3(), 25.0)
    return BRepBuilderAPI_MakeFace(surf, 0.0, 2 * math.pi, 0.0, 40.0, 1e-6).Face()
