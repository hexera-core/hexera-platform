"""Which axis a surface is extruded along, measured rather than assumed.

WHY IT IS MEASURED. An octree cell is a cube, so resolving a 0.098 mm channel's WIDTH also buys
cells through its 1.9 mm height whether or not the height needs them. cfMesh can stretch cells
along an axis (`anisotropicSources`) and the cell saving is exactly the stretch factor - measured
on a 10 mm box at 1 mm cells, 2026-10-29: 896 cells plain, 320 at scaleY 4.

"For free" is doing real work in that claim. It is free only where the shape is PRISMATIC along
that axis; stretching an axis that carries features smears the things the mesh exists to resolve.
So the licence to stretch is a geometric measurement, and this is it.

THE READING IS THE CAP FRACTION. An extruded surface has every side-wall normal perpendicular to
the extrusion axis and only its caps pointing along it, so the area-weighted mean of |n . axis| is
the share of the surface that is cap. For the first customer's channels - 0.098 mm wide, 1.9 mm
tall - that predicts 0.098/(1.9+0.098) = 4.9%. Measured on their real `fluid_fine.stl` (32,454
triangles, 2026-10-29) it reads 0.0694 on y, and their whole coolant surface - manifold loop
included - correctly reads as having no prismatic axis at all.
"""
import numpy as np
import pytest

from meshpipeline.cad.scales import MAX_PRISMATIC_NORMAL, prismatic_axis


def _extruded_square(axis: int, *, side=0.0001, length=0.002, caps=True):
    """A square tube of `side` across, extruded `length` along `axis`. The shape a microchannel
    is: tall and thin, prismatic along its height."""
    o = [k for k in range(3) if k != axis]            # the two in-plane axes
    tris = []

    def P(a, b, c):
        p = [0.0, 0.0, 0.0]
        p[axis] = a
        p[o[0]] = b
        p[o[1]] = c
        return p

    for lo, hi in ((0.0, side),):                      # the four side walls
        for b, bb in ((lo, lo), (hi, hi)):
            tris += [(P(0, b, lo), P(length, bb, lo), P(length, bb, hi)),
                     (P(0, b, lo), P(length, bb, hi), P(0, b, hi))]
        for c, cc in ((lo, lo), (hi, hi)):
            tris += [(P(0, lo, c), P(length, lo, cc), P(length, hi, cc)),
                     (P(0, lo, c), P(length, hi, cc), P(0, hi, c))]
    if caps:
        for a in (0.0, length):
            tris += [(P(a, 0.0, 0.0), P(a, side, 0.0), P(a, side, side)),
                     (P(a, 0.0, 0.0), P(a, side, side), P(a, 0.0, side))]
    pts = np.asarray([v for t in tris for v in t], dtype=float)
    faces = np.arange(len(pts)).reshape(-1, 3)
    return pts, faces


def _sphere(n=24):
    """A shape with no preferred axis: every axis should read about 0.5."""
    u = np.linspace(0, np.pi, n)
    v = np.linspace(0, 2 * np.pi, n)
    uu, vv = np.meshgrid(u, v, indexing="ij")
    x, y, z = (np.sin(uu) * np.cos(vv), np.sin(uu) * np.sin(vv), np.cos(uu))
    pts, faces = [], []
    grid = np.stack([x, y, z], axis=-1).reshape(-1, 3)
    for i in range(n - 1):
        for j in range(n - 1):
            a, b = i * n + j, i * n + j + 1
            c, d = (i + 1) * n + j + 1, (i + 1) * n + j
            faces += [[a, b, c], [a, c, d]]
    pts = grid
    return pts, np.asarray(faces)


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_an_extruded_tube_names_its_own_axis(axis):
    got = prismatic_axis(*_extruded_square(axis))
    assert got is not None, "a square tube is prismatic by construction"
    assert got[0] == axis, f"read {got[0]} for a tube extruded along {axis}"
    assert got[1] < MAX_PRISMATIC_NORMAL


def test_the_reading_is_the_cap_fraction():
    """The number means something: it is the share of the area that is cap, so a longer
    extrusion reads lower. 0.0001 across and 0.002 long gives caps of 2 x 0.0001^2 against
    sides of 4 x 0.0001 x 0.002, i.e. 0.0001/(0.002 x 2 + 0.0001) = 0.0244."""
    got = prismatic_axis(*_extruded_square(1, side=0.0001, length=0.002))
    assert got is not None
    assert got[1] == pytest.approx(2 * 0.0001**2 / (2 * 0.0001**2 + 4 * 0.0001 * 0.002), rel=0.02)


def test_a_longer_extrusion_reads_flatter():
    short = prismatic_axis(*_extruded_square(1, length=0.0005))
    long_ = prismatic_axis(*_extruded_square(1, length=0.004))
    assert short and long_ and long_[1] < short[1]


def test_a_shape_with_no_preferred_axis_is_refused():
    """A sphere reads about 0.5 on every axis. Stretching it would smear it."""
    assert prismatic_axis(*_sphere()) is None


def test_a_cube_is_refused():
    """Equal caps and sides: 1/3 on every axis, above the bar. A cube has no long way."""
    got = prismatic_axis(*_extruded_square(1, side=0.001, length=0.001))
    assert got is None, "a cube is not an extrusion"


def test_an_open_tube_is_still_prismatic():
    """Caps are what the reading counts, so a tube with none reads ~0 - still prismatic, more so."""
    got = prismatic_axis(*_extruded_square(2, caps=False))
    assert got is not None and got[0] == 2
    assert got[1] == pytest.approx(0.0, abs=1e-9)


def test_an_empty_surface_is_survived():
    assert prismatic_axis(np.zeros((0, 3)), np.zeros((0, 3), dtype=int)) is None


def test_a_degenerate_surface_is_survived():
    """Zero-area triangles carry no normal and must not divide by zero."""
    pts = np.zeros((3, 3))
    assert prismatic_axis(pts, np.asarray([[0, 1, 2]])) is None
