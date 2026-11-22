# Responsibility: Verify SHMESH-94: SHMESH-68's closing guard no longer refuses a narrow strip only for a quad round
# it that comes out past a solver limit SHMESH-10's cut mends (two triangles inside every solver limit and outer line);
# such a strip closes only where no element comes out past any outer line it was inside (the angle lines too). It
# still refuses for a quad no cut mends. Refused for a mendable taper, a strip stayed open and its own quads were
# merged into triangles beyond the triangle-min line (lab s15: 17.6 deg).
# Boundaries: pure functions on a few nodes. No gmsh, no solver.
from __future__ import annotations

import numpy as np

from meshpipeline.engines import shell_quality as SQ
from meshpipeline.engines.gmsh import shell_adapter as SA

# a trapezoid 1.6 high, its sides 4.0 and 1.1 long: taper (4.0 - 1.1) / (4.0 + 1.1) = 0.57, past Nastran's 0.5
TAPERED = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [2.55, 1.6, 0.0], [1.45, 1.6, 0.0]])
# the same at sides 4.0 and 1.4: 0.48, inside it
INSIDE = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [2.7, 1.6, 0.0], [1.3, 1.6, 0.0]])
# a long thin quad bent into a chevron: every cut leaves a triangle past a solver limit
NEEDLE = np.array([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 0.05, 0.0], [5.0, 3.0, 0.0]])


def _lines(outer_min_angle: float) -> SQ.InForce:
    """Nastran's own limits and one user line: triangles' smallest angle 30, outer `outer_min_angle`."""
    return SQ.InForce(hard=SQ.IN_FORCE.hard, aims=(("tria", "min_angle_tria", ">=", 30.0, outer_min_angle, 0.05),))


def test_the_tapered_quad_is_past_the_solver_limit_and_its_cut_mends_it():
    q = np.array([[0, 1, 2, 3]])
    assert SQ._quad_past(SQ.quad_measures(TAPERED, q, extra=True))[0]
    assert not SQ._quad_past(SQ.quad_measures(INSIDE, q, extra=True))[0]
    assert SA._clean_cut(TAPERED, [0, 1, 2, 3], SQ.IN_FORCE)


def test_a_quad_no_cut_mends_has_no_clean_cut():
    assert not SA._clean_cut(NEEDLE, [0, 1, 2, 3], SQ.IN_FORCE)


def test_a_cut_past_an_outer_line_is_no_clean_cut():
    tm = SQ.tria_measures(TAPERED, np.array([[0, 1, 2], [0, 2, 3], [0, 1, 3], [1, 2, 3]]))
    best = max(tm["min_angle_tria"][:2].min(), tm["min_angle_tria"][2:].min())
    assert SA._clean_cut(TAPERED, [0, 1, 2, 3], _lines(best - 1.0))
    assert not SA._clean_cut(TAPERED, [0, 1, 2, 3], _lines(best + 1.0))


def test_a_taper_the_cut_mends_is_not_worse_when_mends_are_weighed():
    conn = [0, 1, 2, 3]
    assert not SA._worse_after(INSIDE, conn, TAPERED, conn, SQ.IN_FORCE, None, mends=True)
    assert SA._worse_after(INSIDE, conn, TAPERED, conn, SQ.IN_FORCE, None)


def test_a_quad_no_cut_mends_is_worse_either_way():
    assert SA._worse_after(INSIDE, [0, 1, 2, 3], NEEDLE, [0, 1, 2, 3], SQ.IN_FORCE, None, mends=True)


def test_an_element_newly_past_an_outer_line_is_seen():
    tri = [0, 1, 2]
    fat = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [2.0, 3.0, 0.0]])
    thin = np.array([[0.0, 0.0, 0.0], [4.0, 0.0, 0.0], [2.0, 0.3, 0.0]])
    assert SA._beyond_after(fat, tri, thin, tri, _lines(20.0))
    assert not SA._beyond_after(thin, tri, thin, tri, _lines(20.0))
    assert not SA._beyond_after(fat, tri, fat, tri, _lines(20.0))
