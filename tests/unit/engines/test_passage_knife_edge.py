# Responsibility: Verify the local radius reads the fluid gap past a snapped knife edge, a leaning corner and a lone reading.
# blade_row_passage_001 meshed cleanly on snappy (265,159 cells, non-orthogonality 55) and the resolution
# floor refused it at 0.0 cells across the narrowest wall, median 15.7, at every refinement: 60 of
# 27,054 wall points read a chord under 5 mm - most onto their own snapped face, folded along the
# trailing edge - and the ball-min handed that to every point within 46 mm (2026-09-29).
from __future__ import annotations

import numpy as np
import pytest

import meshpipeline.engines.radius_field as RF
from meshpipeline.engines.passage import measure_passage

_W, _H = 0.12, 0.06                                   # duct section, metres


def _fan(polys):
    """Each polygon fanned from its first vertex, as boundary_triangles_of_polymesh reads a polyMesh."""
    return np.asarray([[p[0], p[k], p[k + 1]] for p in polys for k in range(1, len(p) - 1)],
                      dtype=np.int64)


def _blade_passage(*, nudge=(0.0, 0.0, 0.0), lean=0.0):
    """A 300 x 120 x 60 mm duct, open at both ends, with a knife-edge blade across it from floor
    to ceiling: 4 mm thick at its blunt leading edge (x = 100 mm), nothing at its trailing edge
    (x = 200 mm), leaning `lean` metres sideways per metre of height. Its lower side is meshed at
    half the span step of its upper side, so every upper face on the trailing edge is a pentagon
    carrying one hanging node of the lower side - a snapped polyMesh boundary at a refinement
    step. `nudge` moves those nodes off the straight edge by that much, as snapping leaves
    them. Returns (points, triangles, trailing-edge point ids)."""
    pts: list = []
    polys: list = []

    def add(p):
        pts.append(tuple(float(c) for c in p))
        return len(pts) - 1

    def grid(at, us, vs, flip):
        ids = [[add(at(u, v)) for v in vs] for u in us]
        for a in range(len(us) - 1):
            for b in range(len(vs) - 1):
                q = [ids[a][b], ids[a + 1][b], ids[a + 1][b + 1], ids[a][b + 1]]
                polys.append(q[::-1] if flip else q)
    xs = np.linspace(0.0, 0.3, 31)
    ys = np.array([-60, -48, -36, -24, -12, -6, 6, 12, 24, 36, 48, 60]) / 1000.0  # none under the blade
    zs = np.linspace(-_H / 2, _H / 2, 7)
    grid(lambda x, y: (x, y, -_H / 2), xs, ys, False)                  # floor
    grid(lambda x, y: (x, y, _H / 2), xs, ys, True)                    # ceiling
    grid(lambda x, z: (x, -_W / 2, z), xs, zs, True)                   # the two side walls
    grid(lambda x, z: (x, _W / 2, z), xs, zs, False)
    xb = np.linspace(0.1, 0.2, 11)
    half = 0.002 * (0.2 - xb) / 0.1
    coarse = np.linspace(-_H / 2, _H / 2, 13)
    fine = np.linspace(-_H / 2, _H / 2, 25)
    up = [[add((x, half[k] + lean * z, z)) for z in coarse] for k, x in enumerate(xb[:-1])]
    lo = [[add((x, -half[k] + lean * z, z)) for z in fine] for k, x in enumerate(xb[:-1])]
    te = [add(np.array((0.2, lean * z, z)) + (np.asarray(nudge) if m % 2 else 0.0))
          for m, z in enumerate(fine)]
    for k in range(len(up) - 1):
        for j in range(len(coarse) - 1):
            polys.append([up[k][j], up[k][j + 1], up[k + 1][j + 1], up[k + 1][j]])
    for j in range(len(coarse) - 1):                                    # the trailing-edge pentagons
        polys.append([te[2 * j], te[2 * j + 1], te[2 * j + 2], up[-1][j + 1], up[-1][j]])
    for k in range(len(lo) - 1):
        for j in range(len(fine) - 1):
            polys.append([lo[k][j], lo[k + 1][j], lo[k + 1][j + 1], lo[k][j + 1]])
    for j in range(len(fine) - 1):
        polys.append([lo[-1][j], te[j], te[j + 1], lo[-1][j + 1]])
    for j in range(len(coarse) - 1):                                    # the blunt leading edge
        polys.append([up[0][j], lo[0][2 * j], lo[0][2 * j + 1], lo[0][2 * j + 2], up[0][j + 1]])
    return np.asarray(pts, dtype=float), _fan(polys), np.asarray(te)


def _radius(pts, tri):
    return RF.local_radius(pts, tri, interior_point=(0.05, 0.0, 0.0), r_lo=1e-6, r_hi=0.2)


def test_a_snapped_knife_edge_does_not_read_as_a_zero_width_passage():
    # the narrowest gap is blade side to duct side at the leading edge: (120 - 4) / 2 = 58 mm
    clean = _radius(*_blade_passage()[:2])
    pts, tri, te = _blade_passage(nudge=(0.0, 1e-4, 0.0))       # a tenth of a millimetre off
    r = _radius(pts, tri)
    # before: the hanging nodes read 0.05 mm (their rays landed on their own pentagon's fan) and
    # the ball-min floored the 5th percentile to that, 0.0 cells across with the median unmoved
    assert r[te].min() > 0.9 * 0.029, "a trailing-edge node read its own face, not the passage"
    assert np.percentile(r, 5) > 0.9 * 0.029
    assert np.percentile(r, 5) == pytest.approx(np.percentile(clean, 5), rel=1e-3), \
        "a nudge of 0.1 mm moved the reading"
    across = measure_passage(pts, tri, r)
    assert across["p05"] >= 0.85 * across["median"], across


def test_a_chord_that_grazes_into_a_leaning_corner_is_not_a_reading():
    # leaning 6 degrees, the blade's foot and head sit 3 mm off the centreline: the narrowest
    # gap is (120 - 4 - 6) / 2 = 55 mm across, so a 27.5 mm radius
    pts, tri, _ = _blade_passage(lean=0.1)
    r = _radius(pts, tri)
    # before: chords that ran into the corners under the leaning blade read how far away the
    # corner was (12.6 mm at the 5th percentile) and the ball-min spread that
    assert np.percentile(r, 5) > 0.9 * 0.0275
    assert np.percentile(r, 5) < 1.1 * 0.03


def test_a_narrow_reading_needs_most_of_its_ring_to_read_it_too():
    ring = [frozenset({0, 1, 2}), frozenset({0, 1, 2, 3}), frozenset({0, 1, 2, 3}),
            frozenset({1, 2, 3, 4}), frozenset({3, 4})]
    r = np.array([0.046, 0.023, 0.046, np.nan, 0.020])
    out = RF._ring_median(r, ring)
    assert out[1] == 0.046, "a lone narrow reading beside wider ones is outvoted"
    assert out[0] == 0.046 and out[2] == 0.046
    assert np.isnan(out[3]), "an unread point is not given a reading here - it borrows later"
    assert np.isnan(out[4]), "a reading its unread ring does not back is dropped - it borrows too"
    # a gap read by the whole patch stays narrow
    assert np.allclose(RF._ring_median(np.full(5, 0.01), ring), 0.01)
    # and when nothing would survive, the readings stand
    lone = np.array([0.01, np.nan, np.nan])
    assert np.array_equal(RF._ring_median(lone, [frozenset({0, 1, 2})] * 3), lone, equal_nan=True)


def test_a_uniform_tube_still_reads_its_radius():
    th = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    xs = np.linspace(0, 0.4, 9)
    pts = np.array([[x, 0.05 * np.cos(t), 0.05 * np.sin(t)] for x in xs for t in th])
    quads = [[i * 24 + j, i * 24 + (j + 1) % 24, (i + 1) * 24 + (j + 1) % 24, (i + 1) * 24 + j]
             for i in range(8) for j in range(24)]
    r = _radius(pts, _fan(quads))
    assert np.allclose(r, 0.05, atol=0.002)
