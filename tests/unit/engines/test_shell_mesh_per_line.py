# Responsibility: Hold SHMESH-84: under the user's limits a relaxing move (SHMESH-14) and a mend change (SHMESH-28)
# are kept only where no line's count rises round what they touch - each solver limit, each of the user's targets and
# outer limits, the edge lines, counted apart and never summed - with the one counting helper (shell_quality).
# Boundaries: numpy on small perturbed grids (found by a seeded search, coordinates kept to 4 decimals); no gmsh.
from __future__ import annotations

import numpy as np
import pytest

from meshpipeline.engines import shell_quality as sq
from meshpipeline.engines.gmsh import shell_adapter as SA

LIM = sq.InForce(aims=(("quad", "min_angle_quad", ">=", 45.0, 30.0, 0.01),
                       ("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01),
                       ("quad", "aspect", "<=", 4.0, 6.0, 0.01)))
INNER = [5, 6, 9, 10]


def _grid(points: list[list[float]]) -> tuple[np.ndarray, np.ndarray]:
    xyz = np.array([[x, y, 0.0] for x, y in points], dtype=float)
    q = np.array([[j * 4 + i, j * 4 + i + 1, (j + 1) * 4 + i + 1, (j + 1) * 4 + i] for j in range(3) for i in range(3)],
                 dtype=np.int64)
    return xyz, q


def _lines(xyz, q, t=None) -> np.ndarray:
    out = sq.line_matrix(xyz, q, LIM).sum(axis=0)
    if t is not None and len(t):
        out = out + sq.line_matrix(xyz, t, LIM).sum(axis=0)
    return out


@pytest.fixture
def summed(monkeypatch):
    # (the strict form, as the defaults now hold it - SHMESH-87b; set_(True) puts the summed rule back)
    monkeypatch.setattr(sq, "RELAX_PER_LINE", True)
    monkeypatch.setattr(sq, "MEND_PER_LINE", True)
    monkeypatch.setattr(sq, "LINE_RULE", "strict")
    # (the per-line rule alone: SHMESH-89 is held by its own tests)
    monkeypatch.setattr(sq, "SEVERE_FIRST", False)
    monkeypatch.setattr(sq, "MEND_SEVERE_FIRST", False)

    def set_(on: bool) -> None:
        monkeypatch.setattr(sq, "PER_LINE", not on)
    return set_


#: a 3 x 3 grid whose quad (0..) is past Nastran's taper; the middle of its free nodes mends it but takes a quad past
#: the user's 135 degree target
RELAX_CASE = [[-0.0145, -0.2191], [1.2578, -0.2323], [1.7118, -0.146], [2.7521, -0.29], [0.0919, 0.7792],
              [0.8643, 1.0901], [2.0478, 0.9862], [3.0124, 1.141], [-0.1133, 2.199], [0.7034, 1.7185],
              [1.9218, 2.0618], [3.0648, 1.8357], [-0.1025, 2.8374], [0.8662, 3.1577], [2.0776, 3.2777],
              [3.2701, 2.8231]]
#: a grid with three quads past the taper limit: summed, the mend re-cut two of them and took a quad under the 45 degree
#: target
MEND_CASE = [[0.0955, 0.2596], [0.912, 0.0871], [1.8314, 0.2639], [3.0706, -0.2048], [-0.1458, 0.7955],
             [1.0028, 1.1203], [2.088, 1.2171], [2.8663, 0.9514], [-0.1396, 2.1402], [0.4205, 1.889],
             [1.7241, 1.7663], [3.1429, 1.8081], [-0.1197, 2.7339], [1.1534, 2.9183], [2.0936, 2.8303],
             [2.7131, 2.8935]]


def test_a_relaxing_move_never_trades_one_line_for_another(summed):
    xyz, q = _grid(RELAX_CASE)
    mov = np.zeros(len(xyz), dtype=bool)
    mov[INNER] = True
    before = _lines(xyz, q)
    summed(True)
    a, _n = sq.relax_past(xyz, q, np.zeros((0, 3), np.int64), mov, rounds=1, parallelogram=True, limits=LIM)
    assert (_lines(a, q) > before).any()                 # summed: the taper mended, a quad past the 135 target
    summed(False)
    b, n = sq.relax_past(xyz, q, np.zeros((0, 3), np.int64), mov, rounds=1, parallelogram=True, limits=LIM)
    after = _lines(b, q)
    assert n >= 1 and not (after > before).any() and after.sum() < before.sum()


def test_a_mend_change_never_trades_one_line_for_another(summed):
    xyz, q = _grid(MEND_CASE)
    kind = np.full(16, sq.AT_VERTEX, dtype=np.int8)
    kind[INNER] = sq.INSIDE
    mov = np.zeros(16, dtype=bool)
    mov[INNER] = True
    before = _lines(xyz, q)

    def mend():
        X, Q2, T2, _qf, _tf, done = sq.mend(xyz, q, np.zeros((0, 3), np.int64), [0] * 9, [], kind, np.full(16, -1),
                                            {}, limits=LIM, movable=mov, rounds=1)
        return _lines(X, Q2, T2), done
    summed(True)
    a, _d = mend()
    assert (a[:-1] > before[:-1]).any()
    summed(False)
    b, done = mend()
    assert sum(done.values()) >= 1 and not (b[:-1] > before[:-1]).any()


def test_the_adapter_counts_lines_with_the_one_helper():
    xyz, q = _grid(MEND_CASE)
    edges = (0.9, 0.85, 1.5, 1.6)
    assert np.array_equal(SA._line_counts(xyz, q, "quad", LIM, edges), sq.line_counts(xyz, q, "quad", LIM, edges))
    m = sq.line_matrix(xyz, q, sq.with_edges(LIM, edges))
    names = sq.line_names(sq.with_edges(LIM, edges))
    assert m.shape == (9, len(names))
    k = names.index("under shortest edge")
    lo = np.linalg.norm(xyz[q] - xyz[np.roll(q, -1, axis=1)], axis=2).min(axis=1)
    assert np.array_equal(m[:, k], lo < 0.9)


def test_the_edge_lines_ride_with_the_limits_only_under_the_users_limits():
    assert sq.with_edges(sq.IN_FORCE, (1.0, 0.9, None, None)) is sq.IN_FORCE
    assert sq.with_edges(LIM, (None, None, None, None)) is LIM
    assert sq.with_edges(LIM, (1.0, 0.9, None, None)).edges == (1.0, 0.9, None, None)


# ------------------------------------------------------------------ SHMESH-87: the ranked rule --------------------
def _rise(**by_name) -> tuple[np.ndarray, np.ndarray]:
    names = sq.line_names(LIM_E)
    r = np.zeros(len(names), dtype=np.int64)
    for k, v in by_name.items():
        r[names.index(k)] = v
    slack = np.zeros(len(names), dtype=np.int64)
    slack[names.index("quad max_angle_quad target")] = 3        # this line's band has room for 3 more
    return r, slack


LIM_E = sq.with_edges(LIM, (0.9, 0.8, None, None))


def _rule(*a, **k):
    # the rule as it reads without SHMESH-89 (an outer or solver count lowered keeps a change whatever else rises)
    return sq.line_rule_ok(*a, severe_first=False, **k)


def test_ranked_a_count_past_an_outer_or_solver_limit_never_rises():
    r, slack = _rise(**{"quad min_angle_quad outer": 1, "Q4_TAPER": -2})
    assert not _rule(r, slack, LIM_E, "ranked")[0]
    r, slack = _rise(Q4_IAMAX=1, **{"quad min_angle_quad outer": -1})
    assert not _rule(r, slack, LIM_E, "ranked")[0]


def test_ranked_a_band_fills_only_within_its_allowance_and_only_to_pay_for_a_worse_count():
    # a quad past the solver's taper mended, one more quad in a band that has room: kept
    r, slack = _rise(Q4_TAPER=-1, **{"quad max_angle_quad target": 1})
    assert _rule(r, slack, LIM_E, "ranked")[0]
    # the same band rise with nothing worse lowered: refused (the strict form with slack would keep it)
    r, slack = _rise(**{"quad max_angle_quad target": 1, "quad min_angle_quad target": -1})
    assert not _rule(r, slack, LIM_E, "ranked")[0]
    assert _rule(r, slack, LIM_E, "strict")[0]
    # past the band's room: refused
    r, slack = _rise(Q4_TAPER=-1, **{"quad max_angle_quad target": 4})
    assert not _rule(r, slack, LIM_E, "ranked")[0]
    # a band with no room (the line over its allowance): refused
    r, slack = _rise(Q4_TAPER=-1, **{"quad min_angle_quad target": 1})
    assert not _rule(r, slack, LIM_E, "ranked")[0]


def test_ranked_nothing_else_rises():
    r, slack = _rise(Q4_TAPER=-1, **{"triangles": 1})
    assert not _rule(r, slack, LIM_E, "ranked")[0]
    r, slack = _rise(**{"under shortest edge": 1})
    assert not _rule(r, slack, LIM_E, "ranked")[0]
    r, slack = _rise(Q4_TAPER=-1)
    assert _rule(r, slack, LIM_E, "ranked")[0]


# ------------------------------------------------------------------ SHMESH-88: the edge lines rise only for a reason --
@pytest.mark.parametrize("rule", ["strict", "ranked"])
def test_an_edge_line_rises_only_for_a_reason(rule):
    names = sq.line_names(LIM_E)
    missed = np.zeros(len(names), dtype=bool)
    missed[names.index("quad aspect target")] = True              # this line's band is past its allowance
    edge = {"under shortest edge less tolerance": 1}
    # a solver limit or an outer limit mended: the edge may rise
    r, slack = _rise(Q4_TAPER=-1, **edge)
    assert _rule(r, slack, LIM_E, rule, missed=missed)[0]
    r, slack = _rise(**{"quad min_angle_quad outer": -1}, **edge)
    assert _rule(r, slack, LIM_E, rule, missed=missed)[0]
    # the band of a line past its allowance lowered: it may too
    r, slack = _rise(**{"quad aspect target": -1}, **edge)
    assert _rule(r, slack, LIM_E, rule, missed=missed)[0]
    # no reason - nothing lowered, or only the band of a line within its allowance: it may not
    r, slack = _rise(**edge)
    assert not _rule(r, slack, LIM_E, rule, missed=missed)[0]
    r, slack = _rise(**{"quad min_angle_quad target": -1}, **edge)
    assert not _rule(r, slack, LIM_E, rule, missed=missed)[0]
    # the reason lets only the edge lines through: a band rising past its room still refuses the change
    r, slack = _rise(Q4_TAPER=-1, **{"quad min_angle_quad target": 1}, **edge)
    assert not _rule(r, slack, LIM_E, rule, missed=missed)[0]


@pytest.mark.parametrize("rule", ["strict", "ranked"])
def test_a_band_element_near_its_outer_limit_taken_back_inside_is_a_reason_for_the_soft_edge_lines(rule):
    # SHMESH-88b, the real column's case: a ring quad at 149.9 degrees (135 target, 150 outer: within 1.5 of it) is
    # taken back inside 135 by a change that puts one element under the shortest edge, inside its tolerance band.
    # The line is within its allowance (not missed), and nothing is past an outer or solver limit
    names = sq.line_names(LIM_E)
    j = [f"{s} {k}" for s, k, *_ in LIM_E.aims].index("quad max_angle_quad")
    near = np.zeros((1, len(LIM_E.aims)), dtype=np.int64)
    near[0, j] = -1
    r, slack = _rise(**{"quad max_angle_quad target": -1, "under shortest edge": 1})
    assert _rule(r, slack, LIM_E, rule, near=near)[0]
    # the same change with the quad nowhere near the outer limit: no reason
    assert not _rule(r, slack, LIM_E, rule, near=np.zeros_like(near))[0]
    assert not _rule(r, slack, LIM_E, rule)[0]
    # the near quad only moved within its band (its band count did not fall): no reason
    r2, slack = _rise(**{"under shortest edge": 1})
    assert not _rule(r2, slack, LIM_E, rule, near=near)[0]
    # the edge past its tolerance too (under the shortest edge less its tolerance): this reason does not reach it
    r3, slack = _rise(**{"quad max_angle_quad target": -1, "under shortest edge": 1,
                         "under shortest edge less tolerance": 1})
    assert not _rule(r3, slack, LIM_E, rule, near=near)[0]
    assert names.index("under shortest edge") in sq.edge_cols(LIM_E).tolist()


def _rhombus(deg: float) -> np.ndarray:
    # a rhombus with corners of deg and 180 - deg
    a = np.radians(180.0 - deg)
    return np.array([[0, 0, 0], [1, 0, 0], [1 + np.cos(a), np.sin(a), 0], [np.cos(a), np.sin(a), 0]], float)


def test_near_the_outer_limit_is_within_a_tenth_of_the_band_inside_it():
    lim = sq.InForce(aims=(("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01),))
    q = np.array([[0, 1, 2, 3]], dtype=np.int64)
    got = {deg: int(sq.near_outer(_rhombus(deg), q, lim)[0, 0]) for deg in (149.9, 148.6, 148.4, 140.0, 150.1)}
    assert got == {149.9: 1, 148.6: 1, 148.4: 0, 140.0: 0, 150.1: 0}


def test_the_allowance_does_not_cover_an_element_near_the_outer_limit():
    # SHMESH-86c: one quad in a band of 100 elements that allows 1%: the line is within its allowance, so the band
    # asks nothing of the mend - but for its outer tenth: a quad at 149.9 degrees still costs, one at 140 does not
    lim = sq.InForce(aims=(("quad", "max_angle_quad", "<=", 135.0, 150.0, 0.01),))
    for deg, cost in ((149.9, sq.AIM_BAND), (140.0, 0)):
        xyz = _rhombus(deg)
        q = np.array([[0, 1, 2, 3]], dtype=np.int64)
        t = np.zeros((99, 3), dtype=np.int64) + np.array([0, 1, 3])         # (99 sound triangles: n = 100)
        got = sq.within_allowance(xyz, q, t, lim)
        assert got.aims[0][3] == pytest.approx(148.5)
        assert int(sq._quad_aim(xyz, q, got)[0]) == cost


def test_the_edge_lines_and_the_missed_lines_are_counted_as_line_matrix_counts_them():
    xyz, q = _grid(MEND_CASE)
    m = sq.line_matrix(xyz, q, LIM_E)
    assert np.array_equal(sq.edge_lines(xyz, q, LIM_E), m[:, sq.edge_cols(LIM_E)].astype(np.int64))
    got = sq.line_missed(xyz, q, np.zeros((0, 3), np.int64), LIM_E)
    names = sq.line_names(LIM_E)
    meas = sq.quad_measures(xyz, q, extra=True)
    for shape, key, op, target, outer, share in LIM_E.aims:
        v = meas[key]
        band = int((((v < target) & (v >= outer)) if op == ">=" else ((v > target) & (v <= outer))).sum())
        assert got[names.index(f"{shape} {key} target")] == (band > int(share * len(q)))
    assert not got[[i for i, n in enumerate(names) if not n.endswith("target")]].any()


# ------------------------------------------------------------------ SHMESH-17b: the pairing holds each line ---------
LIM_P = sq.InForce(aims=(("quad", "aspect", "<=", 1.5, 6.0, 0.01), ("tria", "min_angle_tria", ">=", 50.0, 20.0, 0.01)))


def test_a_pair_never_trades_one_band_for_another_past_its_allowance(monkeypatch):
    # a 2 x 1 rectangle cut on its diagonal: both triangles in the triangle-angle band (26.6 degrees), the quad they
    # make in the aspect band (2): summed (SHMESH-25) the quad scores no worse than the two triangles and is made
    xyz = np.array([[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]], dtype=float)
    t = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    q0 = np.zeros((0, 4), dtype=np.int64)
    monkeypatch.setattr(sq, "PAIR_LINES", False)
    new_q, _kept, _came = sq.pair_trias(xyz, q0, t, [0, 0], LIM_P)
    assert len(new_q) == 1
    monkeypatch.setattr(sq, "PAIR_LINES", True)
    new_q, kept, _came = sq.pair_trias(xyz, q0, t, [0, 0], LIM_P)
    assert len(new_q) == 0 and len(kept) == 2


# ------------------------------------------------------------------ SHMESH-89: the order before every allowance ------
@pytest.mark.parametrize("rule", ["strict", "ranked"])
def test_a_change_out_past_an_outer_or_solver_limit_is_kept_whatever_else_rises(rule):
    names = sq.line_names(LIM_E)
    worse = {"quad max_angle_quad target": 4, "quad min_angle_quad target": 2, "under shortest edge less tolerance": 1,
             "triangles": 1}                                        # (a band past its room, an edge, a triangle)
    for down in ({"Q4_TAPER": -1}, {"quad min_angle_quad outer": -1}, {"not valid": -1}):
        r, slack = _rise(**down, **worse)
        assert not _rule(r, slack, LIM_E, rule)[0]
        assert sq.line_rule_ok(r, slack, LIM_E, rule)[0]
        assert bool(sq.severe_down(r, LIM_E)[0])
    # never where it puts another element past an outer or solver limit
    r, slack = _rise(Q4_TAPER=-2, **{"quad min_angle_quad outer": 1})
    assert not sq.line_rule_ok(r, slack, LIM_E, rule)[0] and not bool(sq.severe_down(r, LIM_E)[0])
    r, slack = _rise(**{"quad min_angle_quad outer": -1, "Q4_IAMAX": 1})
    assert not sq.line_rule_ok(r, slack, LIM_E, rule)[0]
    # with nothing past one lowered, the rule is as it was
    r, slack = _rise(**{"quad max_angle_quad target": 4, "quad min_angle_quad target": -1})
    assert not sq.line_rule_ok(r, slack, LIM_E, rule)[0]
    assert names.index("triangles") == len(names) - 1


#: a 3 x 3 grid with one quad past a Nastran limit: every move that mends it puts an element into a band of the
#: user's, and with no allowance there is no slack (found by a seeded search)
SEVERE_CASE = [[0.3811, -0.11], [1.0445, -0.4803], [2.5764, 0.1551], [2.9845, -0.2658], [-0.0315, 1.3873],
               [0.8458, 0.9037], [2.1551, 0.8412], [3.1019, 1.0264], [-0.3014, 2.1439], [0.6765, 2.108],
               [1.8521, 2.1455], [2.7491, 2.0491], [0.2969, 3.5182], [1.0383, 3.405], [2.1577, 3.1096],
               [2.9984, 2.9983]]


def test_the_relaxing_takes_an_element_out_past_a_solver_limit_into_a_band(monkeypatch):
    # SHMESH-89: the per-line rule alone refuses the move that mends the solver limit; the order keeps it
    xyz, q = _grid(SEVERE_CASE)
    mov = np.zeros(len(xyz), dtype=bool)
    mov[INNER] = True
    monkeypatch.setattr(sq, "RELAX_PER_LINE", True)
    monkeypatch.setattr(sq, "LINE_RULE", "strict")
    monkeypatch.setattr(sq, "RELAX_ALLOWANCE", False)               # (no allowance: no slack on any line)
    past0 = int(sq._quad_past(sq.quad_measures(xyz, q)).sum())
    got = {}
    for on in (False, True):
        monkeypatch.setattr(sq, "SEVERE_FIRST", on)
        x, n = sq.relax_past(xyz, q, np.zeros((0, 3), np.int64), mov, rounds=1, parallelogram=True, limits=LIM)
        got[on] = (int(sq._quad_past(sq.quad_measures(x, q)).sum()), n)
    assert got[False][0] == past0 and got[True][0] < past0


def test_the_relaxing_sweeps_the_moves_out_past_a_limit_first(monkeypatch):
    # SHMESH-89b: the first sweep of a round keeps only moves that lower a count past an outer or solver limit and
    # raise none; on SEVERE_CASE it mends the solver limit
    xyz, q = _grid(SEVERE_CASE)
    mov = np.zeros(len(xyz), dtype=bool)
    mov[INNER] = True
    monkeypatch.setattr(sq, "RELAX_PER_LINE", True)
    monkeypatch.setattr(sq, "LINE_RULE", "strict")
    seen = []
    orig = sq.severe_down

    def spy(rise, limits):
        got = orig(rise, limits)
        seen.append(got.copy())
        return got
    monkeypatch.setattr(sq, "severe_down", spy)
    x, n = sq.relax_past(xyz, q, np.zeros((0, 3), np.int64), mov, rounds=1, parallelogram=True, limits=LIM)
    assert seen and n >= 1
    assert int(sq._quad_past(sq.quad_measures(x, q)).sum()) < int(sq._quad_past(sq.quad_measures(xyz, q)).sum())


def test_the_whole_mesh_counts_read_measures_given_as_made():
    xyz, q = _grid(MEND_CASE)
    t = np.zeros((0, 3), np.int64)
    m = sq._whole_measures(xyz, q, t)
    assert np.array_equal(sq.line_slack(xyz, q, t, LIM_E), sq.line_slack(xyz, q, t, LIM_E, m=m))
    assert np.array_equal(sq.line_missed(xyz, q, t, LIM_E), sq.line_missed(xyz, q, t, LIM_E, m=m))
    assert sq.within_allowance(xyz, q, t, LIM_E) == sq.within_allowance(xyz, q, t, LIM_E, m=m)


#: a 3 x 3 grid (spacing 1.2) whose quads are all inside every target but one at 148.5..150 degrees (135 target, 150
#: outer), every edge over 0.9 (found by a seeded search)
NEAR_CASE = [[0.0806, 0.0233], [1.121, -0.0493], [2.4485, -0.3056], [3.6452, 0.0083], [-0.0959, 1.1875],
             [1.0175, 1.3272], [1.9967, 0.6679], [3.5664, 1.1186], [-0.0156, 2.3485], [0.9877, 2.5303],
             [2.3048, 2.4658], [3.5405, 2.3849], [0.0665, 3.7324], [1.3, 3.8889], [2.2537, 3.4756],
             [3.5646, 3.6199]]


def test_the_mend_takes_a_quad_near_its_outer_limit_back_inside_within_the_allowance(monkeypatch):
    # SHMESH-86c on the mend itself: 100 far sound triangles make the 1% allowance cover the one quad in its band, so
    # the line asks nothing of its band - but for its outer tenth: NEAR_MEND moves a node and the quad comes back
    # inside 148.5; off, the mend leaves it
    xyz2, q = _grid(NEAR_CASE)
    pts, tr = [], []
    for k in range(100):
        b = 16 + 3 * k
        pts += [[100.0 + 3.0 * k, 100.0, 0.0], [101.0 + 3.0 * k, 100.0, 0.0], [100.5 + 3.0 * k, 100.8660254, 0.0]]
        tr.append([b, b + 1, b + 2])
    xyz = np.vstack([xyz2, np.array(pts)])
    t = np.array(tr, dtype=np.int64)
    lim = sq.with_edges(LIM, (0.9, 0.72, None, None))
    m = sq.quad_measures(xyz, q, extra=True)
    assert int(((m["max_angle_quad"] > 148.5) & (m["max_angle_quad"] < 150.0)).sum()) == 1
    kind = np.full(len(xyz), sq.AT_VERTEX, dtype=np.int8)
    kind[INNER] = sq.INSIDE
    mov = np.zeros(len(xyz), dtype=bool)
    mov[INNER] = True
    got = {}
    for on in (False, True):
        monkeypatch.setattr(sq, "NEAR_MEND", on)
        X, Q2, _T2, _qf, _tf, done = sq.mend(xyz, q, t, [0] * 9, [0] * 100, kind, np.full(len(xyz), -1), {},
                                             limits=lim, movable=mov, rounds=1, edges=(0.9, None))
        got[on] = (sum(done.values()), float(sq.quad_measures(X, Q2)["max_angle_quad"].max()))
    assert got[False][0] == 0 and got[False][1] > 148.5
    assert got[True][0] >= 1 and got[True][1] <= 148.5


def _rough_plate(seed: int, n: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[int], list[int]]:
    # an n x n grid of quads, its nodes shaken (some quads past Nastran's limits and the user's), every fifth quad cut
    # into two triangles, two faces side by side
    rng = np.random.default_rng(seed)
    pts = [[i + rng.uniform(-0.33, 0.33), j + rng.uniform(-0.33, 0.33), rng.uniform(-0.05, 0.05)]
           for j in range(n + 1) for i in range(n + 1)]
    xyz = np.round(np.array(pts, dtype=float), 4)
    quads, trias, qf, tf = [], [], [], []
    for j in range(n):
        for i in range(n):
            a, b, c, d = j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i
            f = int(i >= n // 2)
            if (i + j * n) % 5 == 0:
                trias += [[a, b, c], [a, c, d]]
                tf += [f, f]
            else:
                quads.append([a, b, c, d])
                qf.append(f)
    return xyz, np.array(quads, dtype=np.int64), np.array(trias, dtype=np.int64), qf, tf


@pytest.mark.parametrize("seed", range(12))
def test_the_batched_mend_makes_the_same_mesh_as_the_loop(monkeypatch, seed):
    # SHMESH-28c: judged in one batch, the mend makes every choice the element-by-element loop made, bit for bit
    xyz, q, t, qf, tf = _rough_plate(seed)
    n = 8
    kind = np.full(len(xyz), sq.INSIDE, dtype=np.int8)
    rim = [k for k in range(len(xyz)) if k % n in (0, n - 1) or k // n in (0, n - 1)]
    kind[rim] = sq.AT_VERTEX
    mov = kind == sq.INSIDE
    lim = sq.with_protect(sq.with_edges(LIM_P, (0.6, 0.5, 1.6, 1.8)), [1] if seed % 2 else [])
    lim = sq.InForce(hard=sq.IN_FORCE.hard, aims=LIM.aims + lim.aims, edges=lim.edges, protect=lim.protect,
                     protect_nodes=lim.protect_nodes)
    got = []
    real, used = sq._judge_batch, []

    def spy(*a, **k):
        out = real(*a, **k)
        used.append(out is not None)
        return out
    monkeypatch.setattr(sq, "_judge_batch", spy)
    for batch in (False, True):
        monkeypatch.setattr(sq, "MEND_BATCH", batch)
        got.append(sq.mend(xyz, q, t, qf, tf, kind, np.full(len(xyz), -1), {}, limits=lim, movable=mov,
                           edges=(0.6, 1.6)))
    (xa, qa, ta, qfa, tfa, da), (xb, qb, tb, qfb, tfb, db) = got
    assert sum(da.values()) >= 1 and used and all(used)        # (the batch judged every round)
    assert np.array_equal(xa, xb) and np.array_equal(qa, qb) and np.array_equal(ta, tb)
    assert qfa == qfb and tfa == tfb and da == db
