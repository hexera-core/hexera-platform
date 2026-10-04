# Responsibility: Verify the sealed-space reading of an external body - which inner spaces the far
# field reaches only through gaps too narrow to mesh, and which faces it actually wets.
# Boundaries: the measurement on synthetic shells; snappyHexMesh's leak closure is the lab's.
from __future__ import annotations

import numpy as np

from meshpipeline.engines.sealed_cavities import outward_signs, read_cavities


def _quad(a, b, c, d, outward):
    """Two triangles for the quad abcd, wound so their normal points along `outward`."""
    a, b, c, d = (np.asarray(p, float) for p in (a, b, c, d))
    n = np.cross(b - a, c - a)
    if float(n @ np.asarray(outward, float)) < 0:
        a, b, c, d = a, d, c, b
    return [np.array([a, b, c]), np.array([a, c, d])]


def _box(lo, hi, *, inward=False, skip_face=None):
    """The six faces of an axis box, normals out of it (or into it); one face may be left out."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    tris = []
    for ax in range(3):
        for side, val in ((-1, lo[ax]), (1, hi[ax])):
            if skip_face == (ax, side):
                continue
            u, v = [i for i in range(3) if i != ax]
            corners = []
            for cu, cv in ((lo[u], lo[v]), (hi[u], lo[v]), (hi[u], hi[v]), (lo[u], hi[v])):
                p = np.zeros(3)
                p[ax], p[u], p[v] = val, cu, cv
                corners.append(p)
            out = np.zeros(3)
            out[ax] = -side if inward else side
            tris += _quad(*corners, out)
    return tris


def _face_with_hole(x, lo, hi, g, outward):
    """The x = const face of the box [lo, hi]^2 in (y, z), with a centred g x g hole: an annulus
    of four quads between the face's corners and the hole's, so every edge is shared (no
    T-junction with the neighbouring faces or the slit walls)."""
    m0, m1 = 0.5 * (lo + hi) - 0.5 * g, 0.5 * (lo + hi) + 0.5 * g
    outer = [(x, lo, lo), (x, hi, lo), (x, hi, hi), (x, lo, hi)]
    hole = [(x, m0, m0), (x, m1, m0), (x, m1, m1), (x, m0, m1)]
    tris = []
    for i in range(4):
        j = (i + 1) % 4
        tris += _quad(outer[i], outer[j], hole[j], hole[i], outward)
    return tris


def _hollow_shell(size=0.1, wall=0.002, gap=None):
    """A hollow cube: outer skin, inner skin (normals into the cavity, out of the wall material),
    and - when gap is given - a square slit gap x gap through the +x wall."""
    s, t = size, wall
    if gap is None:
        return _box((0, 0, 0), (s, s, s)) + _box((t, t, t), (s - t, s - t, s - t), inward=True)
    tris = _box((0, 0, 0), (s, s, s), skip_face=(0, 1))
    tris += _face_with_hole(s, 0.0, s, gap, (1, 0, 0))
    tris += _box((t, t, t), (s - t, s - t, s - t), inward=True, skip_face=(0, 1))
    tris += _face_with_hole(s - t, t, s - t, gap, (-1, 0, 0))
    m0, m1 = 0.5 * s - 0.5 * gap, 0.5 * s + 0.5 * gap
    x0, x1 = s - t, s
    # the slit's own walls, normals into the slit (out of the wall material)
    tris += _quad((x0, m0, m0), (x1, m0, m0), (x1, m0, m1), (x0, m0, m1), (0, 1, 0))
    tris += _quad((x0, m1, m0), (x1, m1, m0), (x1, m1, m1), (x0, m1, m1), (0, -1, 0))
    tris += _quad((x0, m0, m0), (x1, m0, m0), (x1, m1, m0), (x0, m1, m0), (0, 0, 1))
    tris += _quad((x0, m0, m1), (x1, m0, m1), (x1, m1, m1), (x0, m1, m1), (0, 0, -1))
    return np.asarray(tris)


def test_a_hollow_body_reached_through_a_narrow_slit_is_sealed_with_a_point_inside():
    # 100 mm cube, 2 mm wall, 6 mm slit; at 4 mm wall cells a gap under 8 mm carries no flow
    tris = _hollow_shell(gap=0.006)
    r = read_cavities(tris, cell_m=0.004)
    assert r is not None and len(r.cavities) == 1
    c = r.cavities[0]
    assert all(0.01 < v < 0.09 for v in c.point), "the point sits deep inside the cavity"
    assert 0.5 * 0.096 ** 3 < c.volume_m3 < 0.096 ** 3
    assert c.clearance_m > 0.02


def test_a_wide_opening_is_part_of_the_flow_and_is_not_sealed():
    # a 30 mm opening at 4 mm cells is several cells across: the inside is the user's flow
    r = read_cavities(_hollow_shell(gap=0.03), cell_m=0.004)
    assert r is not None and r.cavities == ()


def test_a_properly_closed_void_is_left_alone():
    # no slit at all: snappyHexMesh never reaches the inside, so nothing needs sealing
    r = read_cavities(_hollow_shell(gap=None), cell_m=0.004)
    assert r is not None and r.cavities == ()


def test_the_far_field_wets_the_outer_skin_and_not_the_sealed_inner_one():
    tris = _hollow_shell(gap=0.006)
    r = read_cavities(tris, cell_m=0.004)
    c = tris.mean(axis=1)
    outer = np.isclose(c, 0.0).any(axis=1) | np.isclose(c, 0.1).any(axis=1)
    inner = (np.isclose(c, 0.002).any(axis=1) | np.isclose(c, 0.098).any(axis=1)) & ~outer
    assert (r.wet_sides[outer] != 0).mean() > 0.95
    assert (r.wet_sides[inner] == 0).mean() > 0.95, (
        "the cavity's own skin faces only the sealed space - it is no part of the meshed wall")


def test_outward_signs_follow_each_closed_shell_and_skip_open_sheets():
    cube = np.asarray(_box((0, 0, 0), (1, 1, 1)))
    assert set(outward_signs(cube).tolist()) == {1}
    assert set(outward_signs(cube[:, ::-1]).tolist()) == {-1}, "reversed winding reads -1"
    sheet = np.asarray(_quad((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0), (0, 0, 1)))
    assert set(outward_signs(sheet).tolist()) == {0}, "an open sheet has no inside"


def test_nothing_measurable_gives_none_not_a_guess():
    assert read_cavities(np.zeros((0, 3, 3)), cell_m=0.004) is None
    assert read_cavities(_hollow_shell(gap=0.006), cell_m=0.0) is None
