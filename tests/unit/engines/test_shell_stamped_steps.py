# Responsibility: Verify the stamped strips' better step (SHMESH-90): a strip whose long sides take two counts is
# meshed in quads only - rows of the coarse count and a transition row of two-to-one steps (two coarse segments
# meeting four, six quads) or three-to-one steps (one meeting three, four quads), placed where the two sides' nodes
# stay in step; the quads meet edge to edge and close on the strip's own sides; the best candidate is kept, the step
# refused when every candidate is past a limit (or, with STEP_OWN_LOW, makes an edge under the user's shortest edge).
# Boundaries: pure functions on flat strips built here (no gmsh, no CAD, no solver).
from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from meshpipeline.engines import shell_quality as SQ
from meshpipeline.engines.gmsh import shell_adapter as SA


def _strip(Lc: float, Lf: float, w: float, n: int, f: int, m: int):
    """A flat trapezoid strip: coarse side Lc mm (n segments) at y = 0, fine side Lf mm (f segments) at y = w, its
    ends m segments; nodes evenly spaced. Returns (bottom, top, left, right, xyz)."""
    off = 0.5 * (Lf - Lc)
    B = [np.array([off + Lc * i / n, 0.0, 0.0]) for i in range(n + 1)]
    T = [np.array([Lf * j / f, w, 0.0]) for j in range(f + 1)]
    L = [B[0] + (T[0] - B[0]) * j / m for j in range(1, m)]
    R = [B[-1] + (T[-1] - B[-1]) * j / m for j in range(1, m)]
    xyz = np.array(B + T + L + R)
    bottom = list(range(n + 1))
    top = list(range(n + 1, n + f + 2))
    o = n + f + 2
    left = [bottom[0]] + list(range(o, o + m - 1)) + [top[0]]
    right = [bottom[-1]] + list(range(o + m - 1, o + 2 * (m - 1))) + [top[-1]]
    return bottom, top, left, right, xyz


def _flat(p: np.ndarray) -> np.ndarray:
    return p


def _sides(bottom, top, left, right) -> set:
    return {(min(p[q], p[q + 1]), max(p[q], p[q + 1])) for p in (bottom, top, left, right) for q in range(len(p) - 1)}


def _edges(Q: np.ndarray) -> Counter:
    return Counter((min(int(q[e]), int(q[(e + 1) % 4])), max(int(q[e]), int(q[(e + 1) % 4]))) for q in Q for e in range(4))


def _area(X: np.ndarray, Q: np.ndarray) -> float:
    p = X[Q]
    return float(0.5 * np.abs(np.cross(p[:, 2] - p[:, 0], p[:, 3] - p[:, 1])[:, 2]).sum())


def _fractions(n: int) -> np.ndarray:
    return np.linspace(0.0, 1.0, n + 1)


# ------------------------------------------------------------------------------------------- where the steps go
def test_a_strip_twice_as_fine_on_one_side_steps_two_to_one_all_along():
    assert SA._step_tokens(_fractions(4), _fractions(8), 4, 8, "gentle") == ["T2a", "T2b", "T2a", "T2b"]


def test_a_two_count_step_in_the_middle_of_a_long_strip_is_one_two_to_one_step():
    got = SA._step_tokens(_fractions(8), _fractions(10), 8, 10, "gentle")
    assert got.count("T2a") == 1 and got.count("T3") == 0 and got.count("P") == 6
    i = got.index("T2a")
    assert 2 <= i <= 4            # in the middle, where the sides' nodes drift apart most


def test_three_to_one_steps_only_where_two_to_one_ones_cannot_make_the_count():
    # 4 -> 10: three steps on four coarse segments: at most one two-to-one step (it takes two of them)
    got = SA._step_tokens(_fractions(4), _fractions(10), 4, 10, "gentle")
    assert got.count("T2a") == 1 and got.count("T3") == 2
    assert SA._step_tokens(_fractions(4), _fractions(10), 4, 10, "t3").count("T3") == 3


def test_counts_that_cannot_step_give_no_steps():
    assert SA._step_tokens(_fractions(4), _fractions(7), 4, 7, "gentle") is None     # odd difference
    assert SA._step_tokens(_fractions(2), _fractions(10), 2, 10, "gentle") is None   # more steps than segments
    assert SA._step_tokens(_fractions(4), _fractions(4), 4, 4, "gentle") is None     # no step


def test_the_spread_mode_is_shmesh73s_own():
    assert SA._step_tokens(_fractions(6), _fractions(10), 6, 10, "spread") == ["P", "T3", "P", "P", "T3", "P"]


# ------------------------------------------------------------------------------------------- the quads
@pytest.mark.parametrize("n,f,m,mode", [(4, 8, 1, "gentle"), (6, 10, 1, "gentle"), (4, 10, 1, "gentle"),
                                        (4, 10, 1, "t3"), (5, 7, 2, "gentle"), (5, 9, 3, "even")])
def test_the_steps_quads_meet_edge_to_edge_and_close_on_the_strips_sides(n, f, m, mode):
    bottom, top, left, right, xyz = _strip(20.0, 28.0, 4.0 * m, n, f, m)
    tok = SA._step_tokens(SA.ST._along(xyz[bottom]), SA.ST._along(xyz[top]), n, f, mode)
    P, Q = SA._step_build(bottom, top, left, right, xyz, tok, _flat)
    X = np.vstack([xyz, P]) if len(P) else xyz
    seen = _edges(Q)
    outer = {e for e, k in seen.items() if k == 1}
    assert all(k in (1, 2) for k in seen.values())
    assert outer == _sides(bottom, top, left, right)
    assert _area(X, Q) == pytest.approx(0.5 * (20.0 + 28.0) * 4.0 * m, rel=1e-9)
    assert (SQ.quad_measures(X, Q)["min_jacobian"] > 0).all()


def test_a_two_to_one_step_is_six_quads_and_three_new_nodes():
    bottom, top, left, right, xyz = _strip(8.0, 8.0, 4.0, 2, 4, 1)
    P, Q = SA._step_build(bottom, top, left, right, xyz, ["T2a", "T2b"], _flat)
    assert len(Q) == 6 and len(P) == 3


def test_a_taller_transition_row_holds_its_nodes_further_from_the_sides():
    bottom, top, left, right, xyz = _strip(24.0, 30.0, 7.0, 6, 8, 2)
    tok = SA._step_tokens(SA.ST._along(xyz[bottom]), SA.ST._along(xyz[top]), 6, 8, "gentle")
    shortest = []
    for tall in (0.0, 2.0 / 3.0):
        P, Q = SA._step_build(bottom, top, left, right, xyz, tok, _flat, 0, tall)
        X = np.vstack([xyz, P])
        own = [e for e in _edges(Q) if e not in _sides(bottom, top, left, right)]
        shortest.append(min(float(np.linalg.norm(X[u] - X[v])) for u, v in own))
    assert shortest[1] > shortest[0]
    assert shortest[1] >= 2.0        # every edge of its own over a 2 mm shortest edge


# ------------------------------------------------------------------------------------------- which is kept
def test_the_best_step_is_two_to_one_where_three_to_one_would_pass_the_solvers_limits():
    bottom, top, left, right, xyz = _strip(16.0, 24.0, 6.0, 4, 8, 1)
    P, Q, kept = SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, None, None)
    assert kept["t2"] == 2 and kept["t3"] == 0 and len(Q) == 12
    X = np.vstack([xyz, P])
    assert not SQ._quad_past(SQ.quad_measures(X, Q)).any()


def test_a_step_no_candidate_can_make_inside_the_limits_is_refused():
    # 2 -> 6 in a strip 1 mm across: every candidate's quads are slivers
    bottom, top, left, right, xyz = _strip(10.0, 30.0, 1.0, 2, 6, 1)
    with pytest.raises(ValueError):
        SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, None, None)


def test_with_the_edge_targets_held_a_step_in_a_strip_under_twice_the_shortest_edge_is_refused(monkeypatch):
    bottom, top, left, right, xyz = _strip(20.0, 22.0, 3.0, 4, 8, 1)
    monkeypatch.setattr(SA, "STEP_OWN_LOW", True)
    with pytest.raises(ValueError, match="under the shortest edge"):
        SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, None, 2.0)
    monkeypatch.setattr(SA, "STEP_OWN_LOW", False)
    _P, Q, _k = SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, None, 2.0)
    assert len(Q) >= 10


def test_the_sides_own_segments_over_the_longest_edge_do_not_refuse_a_step():
    # the coarse side was counted at 4 over 20 mm (5 mm segments) against a 4.5 mm longest edge: the paving would
    # carry the same segments; only the step's own edges are held to it
    bottom, top, left, right, xyz = _strip(20.0, 22.0, 3.0, 4, 8, 1)
    P, Q, _kept = SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, 4.5, None)
    X = np.vstack([xyz, P])
    own = [e for e in _edges(Q) if e not in _sides(bottom, top, left, right)]
    assert len(Q) == 12 and max(float(np.linalg.norm(X[u] - X[v])) for u, v in own) <= 4.5
    with pytest.raises(ValueError):
        SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, 2.0, None)


def test_the_steps_are_kept_on_the_strips_where_they_cost_no_triangles(monkeypatch):
    # all on: strip face 1 and its neighbour 5 carry 6 triangles (1 without the steps), strip face 2 none (4 without):
    # the mesh with only strip 2 stepped is made, and kept when it stands better
    from types import SimpleNamespace

    calls = []

    def path(brep, record, **kw):
        excl = len(SA._STEP_EXCLUDE)
        calls.append((SA.TRANSITIONS, excl))
        if SA.TRANSITIONS and not excl:
            q = {"strip_steps": {"planned": 2, "made": 2},
                 "step_faces": {"1": {"near": [1, 5], "key": [[0.0, 0.0, 0.0], 1.0]},
                                "2": {"near": [2, 6], "key": [[1.0, 0.0, 0.0], 1.0]}},
                 "face_trias": {"1": 4, "5": 2}}
            n = 10
        elif SA.TRANSITIONS:
            q, n = {"strip_steps": {"planned": 1, "made": 1}, "face_trias": {"5": 1}}, 7
        else:
            q, n = {"face_trias": {"5": 1, "2": 3, "6": 1}}, 12
        return SimpleNamespace(trias=[0] * n, n_elements=100), {"quality": q, "requirements": {"rows": []}}, 0

    monkeypatch.setattr(SA, "_narrow_kept_path" if hasattr(SA, "_narrow_kept_path") else "_guarded_path", path)
    _mesh, applied, _p = SA._yield_both("x.brep", {}, requirements={})
    kept = applied["quality"]["strip_steps_kept"]
    assert kept["kept"] == "the steps kept where they pay (1 of 2 strips)"
    assert (True, 1) in calls and SA._STEP_EXCLUDE == []
    monkeypatch.setattr(SA, "STEP_PER_STRIP", False)
    calls.clear()
    _mesh, applied, _p = SA._yield_both("x.brep", {}, requirements={})
    assert (True, 1) not in calls and applied["quality"]["strip_steps_kept"]["kept"] == "all on"


# ------------------------------------------------------------------------------------------- per-face keep (SHMESH-91)
def _two_faces(order: list[int], quad_face: int):
    """Two unit squares side by side (face 0 left, face 1 right) sharing the edge x = 1: the face `quad_face` one
    quad, the other two triangles; nodes numbered in `order` (a permutation of the six grid nodes)."""
    from meshpipeline.engines.shell_mesh import ShellMesh

    grid = [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)]
    pos = [grid[i] for i in order]
    xyz = np.array([[x, y, 0.0] for x, y in pos])
    at = {g: order.index(i) for i, g in enumerate(grid)}

    def n(x, y):
        return at[(x, y)]
    quads, trias, fq, ft = [], [], [], []
    for f, x0 in ((0, 0), (1, 1)):
        a, b, c, d = n(x0, 0), n(x0 + 1, 0), n(x0 + 1, 1), n(x0, 1)
        if f == quad_face:
            quads.append([a, b, c, d])
            fq.append(f)
        else:
            trias += [[a, b, c], [a, c, d]]
            ft += [f, f]
    ne = len(quads) + len(trias)
    m = ShellMesh(xyz=xyz, quads=np.array(quads, dtype=np.int64).reshape(-1, 4),
                  trias=np.array(trias, dtype=np.int64).reshape(-1, 3), wall=np.full(ne, 1.0),
                  mid=["Mid_1"] * ne, part=["Part_1"] * ne, groups={"Part_1": np.arange(ne)})
    m.face_of = np.array(fq + ft, dtype=np.int64)
    return m


def test_the_per_face_keep_stitches_each_faces_best_mesh_from_two_candidates():
    a = _two_faces([0, 1, 2, 3, 4, 5], quad_face=0)        # face 0 quads, face 1 triangles
    b = _two_faces([5, 3, 1, 4, 0, 2], quad_face=1)        # the other way round, its nodes numbered otherwise
    rec = {"quality": {"face_trias": {}}, "requirements": {"rows": []}}
    got = SA._face_keep(("all on", a, rec, 0), [("all on", a, rec, 0), ("without the steps", b, rec, 0)], None)
    assert got is not None
    mesh = got[1]
    assert len(mesh.quads) == 2 and len(mesh.trias) == 0 and len(mesh.xyz) == 6
    # every node used, no two at one place, the shared edge's two nodes used by both quads
    pos = {tuple(p) for p in mesh.xyz.tolist()}
    assert len(pos) == 6 and set(mesh.quads.ravel().tolist()) == set(range(6))
    shared = [i for i, p in enumerate(mesh.xyz.tolist()) if p[0] == 1.0]
    assert all(all(s in q for s in shared) for q in mesh.quads.tolist())
    assert (SA.shell_quality.quad_measures(mesh.xyz, mesh.quads)["min_jacobian"] > 0).all()
    assert sorted(mesh.face_of.tolist()) == [0, 1] and len(mesh.groups["Part_1"]) == 2
    assert got[2]["quality"]["face_keep"]["faces"] == 1


def test_the_per_face_keep_takes_nothing_where_the_boundary_nodes_do_not_agree():
    a = _two_faces([0, 1, 2, 3, 4, 5], quad_face=0)
    b = _two_faces([0, 1, 2, 3, 4, 5], quad_face=1)
    b.xyz = b.xyz.copy()
    b.xyz[:, 0] *= 1.01                                    # every node of b a hair off a's
    rec = {"quality": {}, "requirements": {"rows": []}}
    assert SA._face_keep(("all on", a, rec, 0), [("all on", a, rec, 0), ("b", b, rec, 0)], None) is None


def test_a_curve_shrunk_to_a_point_is_degenerate_and_a_closed_circle_is_not():
    # SHMESH-79: a cone's side has a curve of no length at its apex (no end vertices) and its base circle (none
    # either, being closed): only the first is a point of the loop
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.addCone(0, 0, 0, 0, 0, 10, 5, 0)
        gmsh.model.occ.synchronize()
        side = next(t for _d, t in gmsh.model.getEntities(2) if gmsh.model.getType(2, t) == "Cone")
        got = {round(gmsh.model.occ.getMass(1, abs(c)), 3): SA._degenerate(gmsh, abs(c))
               for _d, c in gmsh.model.getBoundary([(2, side)], oriented=False)}
        assert got == {0.0: True, round(10 * np.pi, 3): False}
    finally:
        gmsh.finalize()


def test_with_the_better_step_off_the_step_is_shmesh73s_spread_three_to_one(monkeypatch):
    monkeypatch.setattr(SA, "STEP_PATTERNS", False)
    bottom, top, left, right, xyz = _strip(30.0, 34.0, 8.0, 8, 10, 2)
    _P, Q, kept = SA._step_best(bottom, top, left, right, xyz, _flat, SQ.IN_FORCE, None, None)
    assert kept["mode"] == "spread" and kept["t3"] == 1 and kept["smooth"] == 0
