"""The self-intersection check (VMTK's input contract) stays bounded on swept CAD tessellations:
slivers as long as the tube, tilted across all three axes, used to fill the bucket table until the
container was killed (a vessel tree STEP, rc 137) - and it still finds a real crossing."""
from __future__ import annotations

import time

import numpy as np
import pyvista as pv

from meshpipeline.cad.surface_checks import self_intersection_report


def _tilted_sliver_tube(path, crossing=False):
    # a long thin tube tessellated as slivers along its whole length (what OCP gives a sweep),
    # tilted so every sliver spans all three axes, plus a field of small triangles that makes
    # the median triangle tiny
    tube = pv.Cylinder(radius=0.01, height=1.0, resolution=96, capping=True).triangulate()
    tube = tube.rotate_z(35.0).rotate_y(40.0)
    small = pv.Plane(center=(0.0, 0.0, 0.8), i_size=0.05, j_size=0.05, i_resolution=60,
                     j_resolution=60).triangulate()
    parts = [tube, small]
    if crossing:
        parts.append(pv.Plane(center=(0, 0, 0), direction=(1, 0, 0), i_size=0.1,
                              j_size=0.1).triangulate())
    merged = parts[0].merge(parts[1:])
    merged.save(str(path))


def test_long_slivers_do_not_blow_up_the_broad_phase(tmp_path):
    p = tmp_path / "tube.stl"
    _tilted_sliver_tube(p)
    t0 = time.time()
    assert self_intersection_report(p) is None
    assert time.time() - t0 < 60.0


def test_a_real_crossing_is_still_found(tmp_path):
    p = tmp_path / "crossed.stl"
    _tilted_sliver_tube(p, crossing=True)
    rep = self_intersection_report(p)
    assert rep is not None and rep["pairs"]
    assert np.isfinite(rep["first_at_m"]).all()
