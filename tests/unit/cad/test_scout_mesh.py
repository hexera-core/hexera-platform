# Responsibility: Verify the triangle scout reads what the CAD scout reads - openings, kind, flow -
# from meshes built on paper: an open tube, a capped tube, a pipe wall with ring ends, a box, and a
# block of boxes on the ground.
# Boundaries: numpy meshes written as STL; no OpenCASCADE, no model, no storage.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad.scout_mesh import read_triangles, scout_mesh, write_mesh_skin
from meshpipeline.cad.stl_io import _box_triangles, write_stl_binary


def _ring(r, z, n=32):
    return [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n), z) for i in range(n)]


def _band(inner_ring, outer_ring):
    """Quads between two rings of equal count, as triangles."""
    n = len(inner_ring)
    tris = []
    for i in range(n):
        a, b = inner_ring[i], inner_ring[(i + 1) % n]
        c, d = outer_ring[i], outer_ring[(i + 1) % n]
        tris.append((list(a), list(b), list(d)))
        tris.append((list(a), list(d), list(c)))
    return tris


def _fan(centre, ring, flip=False):
    n = len(ring)
    tris = []
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        tris.append((list(centre), list(b), list(a)) if flip else (list(centre), list(a), list(b)))
    return tris


def _tube(r=0.05, length=1.0):
    return _band(_ring(r, 0.0), _ring(r, length))


@pytest.fixture
def stl(tmp_path):
    def _write(name, tris):
        p = tmp_path / f"{name}.stl"
        write_stl_binary(p, tris)
        return p
    return _write


def test_an_open_tube_reads_its_two_rims_as_the_openings(stl):
    res = scout_mesh(stl("tube", _tube()), scale_to_m=1.0)
    assert res.flow == "internal" and res.input_kind == "body-surface"
    assert len(res.openings) == 2
    assert {o.role for o in res.openings} == {"inlet", "outlet"}
    for o in res.openings:
        assert o.kind == "rim" and o.shape == "circle"
        assert abs(o.equivalent_diameter - 0.1) < 0.006      # a 32-gon is a hair under the circle
    zs = sorted(o.centroid[2] for o in res.openings)
    assert abs(zs[0]) < 1e-6 and abs(zs[1] - 1.0) < 1e-6
    assert res.seed_point is not None
    assert any("close, not exact" in n for n in res.notes)


def test_a_capped_tube_is_a_fluid_body_with_two_disc_mouths(stl):
    tris = _tube() + _fan((0, 0, 0), _ring(0.05, 0.0), flip=True) + _fan((0, 0, 1.0), _ring(0.05, 1.0))
    res = scout_mesh(stl("fluid", tris), scale_to_m=1.0)
    assert res.flow == "internal" and res.input_kind == "fluid-domain"
    assert [o.kind for o in res.openings] == ["disc", "disc"]
    assert all(o.on_extremity for o in res.openings)


def test_a_pipe_wall_with_ring_ends_is_read_as_a_wall(stl):
    outer, inner, L = 0.06, 0.05, 1.0
    tris = _band(_ring(outer, 0.0), _ring(outer, L))                 # outside skin
    tris += _band(_ring(inner, L), _ring(inner, 0.0))                 # inside skin, wound the other way
    tris += _band(_ring(inner, 0.0), _ring(outer, 0.0))               # the two annular ends
    tris += _band(_ring(outer, L), _ring(inner, L))
    res = scout_mesh(stl("wall", tris), scale_to_m=1.0)
    assert res.body_kind == "pipe_wall" and res.input_kind == "body-surface" and res.flow == "internal"
    assert len(res.openings) == 2 and all(o.kind == "ring" for o in res.openings)
    for o in res.openings:
        assert abs(o.equivalent_diameter - 2 * inner) < 0.01      # the HOLE is the opening


def test_a_box_is_a_solid_body_in_a_flow(stl):
    res = scout_mesh(stl("box", _box_triangles((0, 0, 0), (2.0, 1.0, 0.8))), scale_to_m=1.0)
    assert res.flow == "external" and res.input_kind == "solid-body"
    assert res.openings == []
    assert res.extra["flow_axis_guess"] == "+x"


def test_a_block_of_boxes_on_the_ground_is_a_scene(stl):
    tris = []
    for i in range(3):
        for j in range(3):
            x, y = 3.0 * i, 3.0 * j
            tris += _box_triangles((x, y, 0.0), (x + 1.0, y + 1.0, 2.0 + i))
    res = scout_mesh(stl("city", tris), scale_to_m=1.0)
    assert res.flow == "external" and res.solids == 9 and res.body_kind == "scene"
    assert res.extra["grounded"] is True
    assert any("scene" in n for n in res.notes)


def test_the_unit_scales_every_measurement(stl):
    p = stl("mm_tube", _tube(r=50.0, length=1000.0))
    res = scout_mesh(p, scale_to_m=0.001)
    assert abs(res.size[2] - 1.0) < 1e-6
    assert abs(res.openings[0].equivalent_diameter - 0.1) < 0.006
    skin = write_mesh_skin(p, p.with_name("skin.stl"), scale_to_m=0.001)
    assert np.abs(read_triangles(skin)).max() <= 1.0 + 1e-6


def test_obj_files_read_the_same_triangles(tmp_path):
    p = tmp_path / "box.obj"
    lo, hi = (0, 0, 0), (1, 1, 1)
    P = [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]), (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
         (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]), (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2])]
    F = [(1, 4, 3, 2), (5, 6, 7, 8), (1, 2, 6, 5), (2, 3, 7, 6), (3, 4, 8, 7), (4, 1, 5, 8)]
    p.write_text("".join(f"v {x} {y} {z}\n" for x, y, z in P) + "".join("f " + " ".join(map(str, f)) + "\n" for f in F))
    tris = read_triangles(p)
    assert tris.shape == (12, 3, 3)
    res = scout_mesh(p, scale_to_m=1.0)
    assert res.flow == "external" and res.solids == 1
