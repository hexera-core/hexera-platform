# Responsibility: Verify gmsh's passage sizing helpers and the measured-passage floor, judging no real mesh.
# The HEX-10 sweep passed 23 of 39 measured fluid meshes under 12 cells across the passage:
# most at exactly the old clamp's 8 across the smallest port, the volutes at 2-4 because neither
# the box nor the ports see the scroll narrowing. Elements now come from the local radius.
from __future__ import annotations

import numpy as np

import meshpipeline.engines.gmsh.driver as D
from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.gmsh import gates as G


def _cylinder(radius=0.05, length=1.0, n_around=24, n_along=40):
    """A closed triangulated tube: wall plus two end caps."""
    th = np.linspace(0, 2 * np.pi, n_around, endpoint=False)
    xs = np.linspace(0, length, n_along)
    ring = np.stack([np.zeros_like(th), radius * np.cos(th), radius * np.sin(th)], axis=1)
    pts = np.concatenate([ring + [x, 0, 0] for x in xs])
    faces = []
    for i in range(n_along - 1):
        for j in range(n_around):
            a, b = i * n_around + j, i * n_around + (j + 1) % n_around
            c, d = a + n_around, b + n_around
            faces += [[a, b, d], [a, d, c]]
    c0, c1 = len(pts), len(pts) + 1
    pts = np.concatenate([pts, [[0, 0, 0], [length, 0, 0]]])
    for j in range(n_around):
        faces.append([c0, (j + 1) % n_around, j])
        base = (n_along - 1) * n_around
        faces.append([c1, base + j, base + (j + 1) % n_around])
    return pts, np.asarray(faces, dtype=np.int64)


def test_sizes_come_from_the_local_radius_and_stay_within_bounds():
    r = np.array([0.05, 0.005, 5.0])
    s = D.passage_sizes(r, h_max=0.02, h_min=0.001)
    assert np.isclose(s[0], 2 * 0.05 / D.PASSAGE_CELLS_ACROSS)   # 13 across a 100 mm bore
    assert s[1] == 0.001                                          # floored, never explodes
    assert s[2] == 0.02                                           # capped at the clamp size


def test_the_callback_only_tightens_what_gmsh_wanted():
    pts = np.array([[0, 0, 0], [1, 0, 0]], float)
    cb = D.passage_size_callback(pts, np.array([0.01, 0.10]))
    assert cb(3, 1, 0.1, 0, 0, 0.05) == 0.01     # nearest wall point wants finer: take it
    assert cb(3, 1, 0.9, 0, 0, 0.05) == 0.05     # nearest wants coarser: keep gmsh's size


def test_the_measure_counts_cells_across_a_tube():
    pts, faces = _cylinder(radius=0.05, n_around=24, n_along=40)
    r = np.full(len(pts), 0.05)
    m = D.measure_passage(pts, faces, r)
    # wall edges are ~13 mm around and ~26 mm along: about 5-7 cells across a 100 mm bore;
    # the two cap centres fan 50 mm spokes, so the 5th percentile sits below the wall figure
    assert 2.0 < m["p05"] <= m["median"] < 9.0 and m["points"] == len(pts)
    # a boundary that carries no usable radius measures nothing
    assert D.measure_passage(pts, faces, np.zeros(len(pts))) == {}


def _ctx(quality):
    c = GateCtx(workspace=None, engine="gmsh", domain="", intake_patches=[], engine_params={})
    c.manifest_or_load = lambda: {"quality": quality}  # type: ignore[method-assign]
    return c


def test_the_narrowest_wall_gates_a_fluid_mesh():
    good_box = {"size_h": 0.005, "bounds": [0, 0, 0, 1.0, 0.05, 0.05], "cells_across_min": 10.0}
    ok, fb = G._gate_resolution_floor(_ctx({**good_box, "passage_cells_across_local":
                                            {"median": 18.0, "p05": 4.6, "min": 2.0}}))
    assert not ok and "narrowest wall" in fb and "4.6" in fb
    ok, _ = G._gate_resolution_floor(_ctx({**good_box, "passage_cells_across_local":
                                           {"median": 18.0, "p05": 12.0, "min": 9.0}}))
    assert ok


def test_without_a_passage_measure_the_bbox_floor_still_applies():
    ok, fb = G._gate_resolution_floor(_ctx({"size_h": 0.040, "bounds": [0, 0, 0, 1.0, 0.05, 0.05]}))
    assert not ok and "narrowest" in fb
