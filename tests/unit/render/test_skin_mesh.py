# Responsibility: Verify the skin preparation gives a CAD viewer what it needs - shared vertices,
# normals that smooth a curve and keep a corner, and the sharp edges as lines.
# Boundaries: numpy meshes built on paper; no OpenCASCADE, no rendering.
from __future__ import annotations

import math

import numpy as np

from meshpipeline.cad.stl_io import _box_triangles
from meshpipeline.render.skin_mesh import edges_block, prepare_skin, skin_patch


def _tube(r=0.05, length=1.0, n=32):
    ring = lambda z: [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z) for i in range(n)]  # noqa: E731
    a, b = ring(0.0), ring(length)
    tris = []
    for i in range(n):
        p, q, s, t = a[i], a[(i + 1) % n], b[i], b[(i + 1) % n]
        tris.append((p, q, t)); tris.append((p, t, s))
    return np.asarray(tris, dtype=float)


def test_a_box_keeps_its_corners_and_all_twelve_edges():
    prep = prepare_skin(np.asarray(_box_triangles((0, 0, 0), (2.0, 1.0, 1.0)), dtype=float))
    assert prep["tri_count"] == 12
    assert len(prep["points"]) == 24                 # eight corners, three normal groups each
    assert len(prep["normals"]) == 24
    # every normal is one of the six axis directions: nothing was smoothed across a corner
    assert all(np.isclose(np.abs(n).max(), 1.0, atol=1e-6) for n in prep["normals"])
    assert prep["edge_lines"][0::3].tolist() == [2] * 12
    assert len(prep["edge_points"]) == 8


def test_a_tube_is_smoothed_around_and_shows_only_its_rims():
    prep = prepare_skin(_tube())
    assert len(prep["points"]) == 64                 # the two rings of vertices, unsplit
    # a point normal on the wall is radial, not the normal of one facet: neighbouring facets
    # were averaged
    p0, n0 = prep["points"][0], prep["normals"][0]
    radial = np.array([p0[0], p0[1], 0.0]); radial /= np.linalg.norm(radial)
    assert float(np.dot(n0, radial)) > 0.99
    assert len(prep["edge_lines"]) // 3 == 64        # the two open rims, 32 segments each


def test_the_viewer_blocks_carry_what_the_stage_reads():
    prep = prepare_skin(np.asarray(_box_triangles((0, 0, 0), (1.0, 1.0, 1.0)), dtype=float))
    patch = skin_patch(prep)
    assert set(patch) >= {"name", "points_b64", "polys_b64", "normals_b64", "tri_count", "face_count"}
    edges = edges_block(prep)
    assert edges["count"] == 12 and "points_b64" in edges and "lines_b64" in edges
