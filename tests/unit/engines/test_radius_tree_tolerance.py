# Responsibility: Prove the radius field's ray tree is built at the surface's own tolerance, never VTK's absolute 0.01:
#                 on a part a few centimetres across (metres) each ray then tests a few triangles, not all of them,
#                 and reads the same chord.
from __future__ import annotations

import time

import numpy as np
import pyvista as pv

from meshpipeline.engines import radius_chords
from meshpipeline.engines.radius_field import _mean_edge, _vertex_rings


def _small_sphere():
    # a 5 mm ball in metres, ~80 k triangles: a small CAD part's staged wall
    s = pv.Sphere(radius=0.005, theta_resolution=200, phi_resolution=200).triangulate().clean()
    m = s.compute_normals(cell_normals=True, point_normals=True, consistent_normals=True,
                          auto_orient_normals=True)
    pts = np.asarray(m.points, dtype=float)
    f = m.faces.reshape(-1, 4)[:, 1:].astype(np.int64)
    return pts, f, np.asarray(m.point_data["Normals"]), np.asarray(m.cell_data["Normals"])


def test_the_tree_tolerance_is_the_parts_own_size():
    pts = np.array([[0, 0, 0], [0.06, 0.0, 0.0], [0.0, 0.0, 0.0]])
    assert radius_chords.tree_tolerance(pts) == 0.06 * radius_chords.TREE_TOLERANCE_OF_SIZE
    assert radius_chords.tree_tolerance(pts * 100) < 0.01


def test_rays_on_a_small_part_use_its_own_tolerance_and_read_the_chord(monkeypatch):
    # at VTK's 0.01 m every box of a 10 mm ball meets every ray and each ray tests every triangle (abc
    # 00160152: 11 ms a ray, 232 s of a 352 s staging); at the part's own size, 0.2 ms, the same chord
    import vtk
    built: list[float] = []

    class Recorded(vtk.vtkOBBTree):
        def BuildLocator(self):                          # noqa: N802 - VTK's own name
            built.append(self.GetTolerance())
            return super().BuildLocator()
    monkeypatch.setattr(vtk, "vtkOBBTree", Recorded)
    pts, f, n, fn = _small_sphere()
    ring = _vertex_rings(f, len(pts))
    fold = 0.5 * _mean_edge(pts, f)
    idx = np.linspace(0, len(pts) - 1, 400).astype(np.int64)
    t0 = time.perf_counter()
    _first, r = radius_chords.cast(pts, f, n, fn, ring, fold, 1e-7, 0.04, idx)
    took = time.perf_counter() - t0
    diag = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))      # 17.3 mm
    assert built and max(built) <= diag * radius_chords.TREE_TOLERANCE_OF_SIZE * 1.000001
    assert abs(float(np.nanmedian(r)) - 0.005) < 2e-4       # half the chord across the ball: its radius
    assert np.isfinite(r).mean() > 0.95
    assert took < 30.0
