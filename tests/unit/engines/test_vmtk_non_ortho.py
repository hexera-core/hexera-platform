# Responsibility: Pin VER-2 on VMTK - the delivered tet mesh's non-orthogonality is measured as checkMesh reads it (the angle between a shared face's normal and the line joining the two tets' centres), so VMTK is judged by the same number as every other engine.
from __future__ import annotations

import math

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")

from meshpipeline.engines.vmtk import vmtk_runner as V  # noqa: E402


def _grid(points, tets):
    cells = np.hstack([[4, *t] for t in tets]).astype(np.int64)
    types = np.full(len(tets), V._VTK_TETRA, dtype=np.uint8)
    return pv.UnstructuredGrid(cells, types, np.asarray(points, dtype=float))


def test_two_mirror_tets_are_orthogonal_across_their_face():
    pts = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1 / 3, 1 / 3, 1], [1 / 3, 1 / 3, -1]]
    r = V.tet_non_orthogonality(_grid(pts, [[0, 1, 2, 3], [0, 2, 1, 4]]))
    assert r["faces"] == 1 and r["max"] == pytest.approx(0.0, abs=1e-6)


def test_a_sheared_neighbour_reads_the_angle_checkmesh_reads():
    # the second apex moved sideways by s: the centre line leans atan(s/4 / (2/4)) off the face normal
    s = 1.0
    pts = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1 / 3, 1 / 3, 1], [1 / 3 + s, 1 / 3, -1]]
    r = V.tet_non_orthogonality(_grid(pts, [[0, 1, 2, 3], [0, 2, 1, 4]]))
    want = math.degrees(math.atan((s / 4) / (2 / 4)))
    assert r["max"] == pytest.approx(want, abs=1e-3)
    assert r["over_70"] == 0


def test_a_cube_cut_into_tets_is_measured_on_every_shared_face():
    grid = pv.ImageData(dimensions=(4, 4, 4)).triangulate()
    r = V.tet_non_orthogonality(grid)
    assert r["faces"] > 0 and 0.0 <= r["mean"] <= r["max"] < 90.0
    # one tet alone shares no face: nothing to judge
    assert V.tet_non_orthogonality(_grid([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], [[0, 1, 2, 3]])) == {}
