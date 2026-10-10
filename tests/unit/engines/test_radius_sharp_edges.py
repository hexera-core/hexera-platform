"""A short sharp-edged narrowing (an orifice plate's bore) carries vertices only on its two rims,
each on a 90-degree edge whose averaged normal looks diagonally past the bore. Read on request
along each adjoining face's own normal, the bore's rims read the bore; read as before, they read
the pipe (venturi_orifice_003: 145 mm everywhere for a 51 mm bore)."""
from __future__ import annotations

import numpy as np
import pytest

import meshpipeline.engines.radius_field as RF

R, r = 0.10, 0.035          # pipe and bore radius


def _revolve(profile, n=48):
    """A triangle surface of revolution about x through the (x, radius) profile points."""
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pts = np.array([[x, rad * np.cos(t), rad * np.sin(t)] for x, rad in profile for t in th])
    tri = []
    for i in range(len(profile) - 1):
        for j in range(n):
            a, b = i * n + j, i * n + (j + 1) % n
            c, d = (i + 1) * n + (j + 1) % n, (i + 1) * n + j
            tri += [[a, b, c], [a, c, d]]
    return pts, np.asarray(tri)


def _orifice():
    # pipe - plate face - 6 mm bore - plate face - pipe, as a rim-only bore
    prof = [(0.0, R), (0.15, R), (0.2, R), (0.2, r), (0.206, r), (0.206, R), (0.25, R), (0.4, R)]
    return _revolve(prof)


def _bore_rims(pts):
    rad = np.hypot(pts[:, 1], pts[:, 2])
    return np.isclose(rad, r) & (pts[:, 0] > 0.19) & (pts[:, 0] < 0.21)


def test_the_bore_of_an_orifice_reads_the_bore_when_sharp_edges_are_read():
    pts, tri = _orifice()
    rim = _bore_rims(pts)
    read = RF.local_radius(pts, tri, (0.1, 0.0, 0.0), 1e-4, 0.3, read_sharp_edges=True)
    assert read[rim].max() == pytest.approx(r, rel=0.15)


def test_the_passage_measures_read_as_before_unless_asked():
    pts, tri = _orifice()
    before = RF.local_radius(pts, tri, (0.1, 0.0, 0.0), 1e-4, 0.3)
    again = RF.local_radius(pts, tri, (0.1, 0.0, 0.0), 1e-4, 0.3, read_sharp_edges=False)
    assert np.array_equal(before, again)
    # read as before, the bore's rims look past it at the pipe (the defect the option fixes)
    assert before[_bore_rims(pts)].max() > 2 * r


def test_a_smooth_tube_reads_the_same_either_way():
    pts, tri = _revolve([(x, R) for x in np.linspace(0, 0.4, 9)])
    a = RF.local_radius(pts, tri, (0.2, 0.0, 0.0), 1e-4, 0.3)
    b = RF.local_radius(pts, tri, (0.2, 0.0, 0.0), 1e-4, 0.3, read_sharp_edges=True)
    assert np.allclose(a, b) and np.allclose(a, R, rtol=0.03)


def test_a_shallow_groove_is_not_read_as_the_passage():
    # a 0.5 mm deep, 3 mm wide groove in a 19 mm bore (the rocket nozzle's): its two step faces
    # face each other 3 mm apart, but the fluid between them is the groove, not a passage - the
    # bore must keep reading its 19 mm, not a 1.5 mm chord spread over the whole wall
    prof = [(0.0, 0.019), (0.02, 0.019), (0.02, 0.0195), (0.023, 0.0195), (0.023, 0.019),
            (0.045, 0.019)]
    pts, tri = _revolve(prof, n=64)
    read = RF.local_radius(pts, tri, (0.01, 0.0, 0.0), 1e-4, 0.05, read_sharp_edges=True)
    assert np.median(read) == pytest.approx(0.019, rel=0.1)
    assert read.min() > 0.5 * 0.019
