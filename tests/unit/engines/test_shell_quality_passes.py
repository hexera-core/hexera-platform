"""SHMESH-100: the extra quality passes - with time left under the run's limit, a mesh made with a figure past its
limit in force is worked on at the elements past it; a mesh inside its limits is delivered exactly as made."""
from __future__ import annotations

import math
import time

import numpy as np
import pytest
from tests.unit.engines.test_shell_requirements import criteria

from meshpipeline.engines import shell_quality as SQ
from meshpipeline.engines import shell_run
from meshpipeline.engines.gmsh import shell_adapter as sa
from meshpipeline.engines.gmsh import shell_quality_passes as QP
from meshpipeline.engines.shell_mesh import ShellMesh


def _grid(n: int = 4, h: float = 10.0, push: tuple[float, float] = (0.0, 0.0)) -> ShellMesh:
    """An n x n plate of h mm quads on z = 0, its node (1, 1) pushed by `push` mm."""
    xs = np.arange(n + 1) * h
    xyz = np.array([[x, y, 0.0] for y in xs for x in xs])
    xyz[(n + 1) + 1, :2] += push
    quads = np.array([[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
                      for j in range(n) for i in range(n)], dtype=np.int64)
    m = ShellMesh(xyz=xyz, quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(n * n, 2.0),
                  mid=["Mid_1"] * (n * n), part=["Part_1"] * (n * n),
                  groups={"Part_1": np.arange(n * n)})
    m.face_of = np.zeros(n * n, dtype=np.int64)  # type: ignore[attr-defined]
    m.held = np.zeros(len(xyz), dtype=bool)  # type: ignore[attr-defined]
    return m


def _applied(mesh: ShellMesh, req=None) -> dict:
    from meshpipeline.engines import shell_requirements as SR

    out = {"quality": SQ.measure(mesh), "sizing": {"low_mm": 5.0, "high_mm": 15.0} if req else {}}
    out["quality"]["passes"] = ["made"]
    out["quality"]["passes_s"] = [1.0]
    if req:
        out["requirements"] = SR.report(mesh, req, out["sizing"])
    return out


@pytest.fixture(autouse=True)
def _no_clock():
    # (shell_adapter's clock is module state: a test reads only what it set)
    saved = dict(sa._CLOCK)
    sa._CLOCK.update(t0=None, allowed=None)
    yield
    sa._CLOCK.clear()
    sa._CLOCK.update(saved)


def test_the_rule_is_cited_by_its_id():
    assert QP.RULE == "SHMESH-100"
    assert sa.QUALITY_PASSES_RULE == "SHMESH-100"


def test_a_jacobian_past_our_target_is_improved_by_moving_free_nodes_only():
    mesh = _grid(push=(4.0, 3.0))
    before = SQ.measure(mesh)["figures"]["jacobian_ratio"]["worst"]
    assert before < 0.6
    got, applied, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec is not None and rec["kept"] >= 1
    after = SQ.measure(got)["figures"]["jacobian_ratio"]["worst"]
    assert after > before
    # every node on the plate's edge stays on it, its corners where they stood; the elements are the same elements
    edge_x = np.flatnonzero(mesh.xyz[:, 0] % 40 == 0)
    edge_y = np.flatnonzero(mesh.xyz[:, 1] % 40 == 0)
    assert np.array_equal(got.xyz[edge_x, 0], mesh.xyz[edge_x, 0])
    assert np.array_equal(got.xyz[edge_y, 1], mesh.xyz[edge_y, 1])
    corners = [0, 4, 20, 24]
    assert np.array_equal(got.xyz[corners], mesh.xyz[corners])
    assert np.array_equal(got.quads, mesh.quads) and len(got.trias) == 0
    assert np.allclose(got.xyz[:, 2], 0.0)
    # the record and the one line say it, and the plan carries the mesh's own figures
    line = next(x for x in rec["lines"] if x["id"] == "jacobian_ratio")
    assert line["improved"] and line["before"] == pytest.approx(before) and line["after"] == pytest.approx(after)
    assert rec["words"].startswith("Improved ") and "the worst element shape (Jacobian) from " in rec["words"]
    assert applied["quality"]["figures"]["jacobian_ratio"]["worst"] == pytest.approx(after)
    assert SQ.measure(got)["valid"]


def test_a_mesh_inside_its_limits_is_left_exactly_as_made():
    mesh = _grid()
    applied = _applied(mesh)
    got, applied2, rec = QP.run(mesh, applied, None, deadline=None)
    assert rec is None and got is mesh and applied2 == applied
    sa._CLOCK.update(t0=time.monotonic(), allowed=900.0)
    out = (mesh, applied)
    assert sa._quality_passes(out, None) is out


def test_no_pass_runs_without_time_left():
    mesh = _grid(push=(4.0, 3.0))
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=time.monotonic() - 1.0)
    assert rec is None and got is mesh
    sa._CLOCK.update(t0=time.monotonic() - 1000.0, allowed=900.0)
    out = (mesh, _applied(mesh))
    assert sa._quality_passes(out, None) is out


def test_with_no_clock_the_run_is_unchanged():
    mesh = _grid(push=(4.0, 3.0))
    out = (mesh, _applied(mesh))
    assert sa._quality_passes(out, None) is out


def test_a_bad_element_with_no_free_node_is_told_and_the_mesh_kept_as_made():
    # one kite-shaped quad: every node is on the face's edge
    xyz = np.array([[0, 0, 0], [10, 0, 0], [25, 25, 0], [0, 10, 0]], dtype=float)
    mesh = ShellMesh(xyz=xyz, quads=np.array([[0, 1, 2, 3]]), trias=np.zeros((0, 3), dtype=np.int64),
                     wall=np.array([2.0]), mid=["Mid_1"], part=["Part_1"])
    mesh.face_of = np.zeros(1, dtype=np.int64)  # type: ignore[attr-defined]
    applied = _applied(mesh)
    xyz0 = mesh.xyz.copy()
    got, applied2, rec = QP.run(mesh, applied, None, deadline=None)
    if rec is None:
        pytest.skip("the kite is inside every limit")
    assert rec["kept"] == 0 and got is mesh and np.array_equal(got.xyz, xyz0)
    assert "did not improve" in rec["words"] and applied2["quality"] == applied["quality"]


def test_a_node_on_a_straight_edge_slides_along_it_and_a_corner_never_moves():
    mesh = _grid()
    k = 2                                   # node (2, 0): on the plate's straight edge y = 0
    mesh.xyz[k, 0] += 8.0
    before = SQ.measure(mesh)["figures"]["aspect"]["worst"]
    assert before > 5.0
    xyz0 = mesh.xyz.copy()
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec is not None and rec["kept"] >= 1
    assert got.xyz[k, 1] == 0.0 and got.xyz[k, 2] == 0.0 and got.xyz[k, 0] < xyz0[k, 0]
    corners = [0, 4, 20, 24]
    assert np.array_equal(got.xyz[corners], xyz0[corners])
    assert SQ.measure(got)["figures"]["aspect"]["worst"] < before


def test_a_structures_nodes_never_move():
    mesh = _grid(push=(4.0, 3.0))
    mesh.held = np.ones(len(mesh.xyz), dtype=bool)  # type: ignore[attr-defined]
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec is not None and rec["kept"] == 0 and got is mesh
    # the adapter holds every node of a mesh that does not say which are a structure's
    bare = _grid()
    del bare.held
    assert sa._held_of(bare).all()


def test_two_slivers_have_their_diagonal_turned():
    xyz = np.array([[0, 0, 0], [5, -1, 0], [10, 0, 0], [5, 1, 0]], dtype=float)
    trias = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    mesh = ShellMesh(xyz=xyz, quads=np.zeros((0, 4), dtype=np.int64), trias=trias, wall=np.full(2, 1.0),
                     mid=["Mid_1"] * 2, part=["Part_1"] * 2)
    mesh.face_of = np.zeros(2, dtype=np.int64)  # type: ignore[attr-defined]
    before = SQ.measure(mesh)["figures"]["min_angle_tria"]["worst"]
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec is not None and rec["turned"] >= 1 and rec["kept"] == 1
    assert len(got.trias) == 2 and len(got.quads) == 0
    assert np.array_equal(got.xyz, mesh.xyz)
    assert SQ.measure(got)["figures"]["min_angle_tria"]["worst"] > before > 0
    assert SQ.measure(got)["valid"]


def test_the_users_lines_are_the_lines_while_they_set_any():
    req = criteria(1.0)
    mesh = _grid(push=(4.0, 3.0))
    applied = _applied(mesh, req)
    lines = QP.lines_in_force(applied, req)
    assert {ln["kind"] for ln in lines} == {"user", "solver"}
    jac = next(r for r in applied["requirements"]["rows"] if r["field"] == "limit:jacobian")
    assert jac["met"] is False
    got, applied2, rec = QP.run(mesh, applied, req, deadline=None)
    assert rec is not None and rec["kept"] >= 1
    jac2 = next(r for r in applied2["requirements"]["rows"] if r["field"] == "limit:jacobian")
    assert jac2["worst"] > jac["worst"]
    # no line of theirs met before is unmet after, none counts more elements outside it
    for r0 in applied["requirements"]["rows"]:
        r1 = next(r for r in applied2["requirements"]["rows"] if r["id"] == r0["id"])
        if r0.get("met") is True:
            assert r1.get("met") is True
        assert int(r1.get("outside") or 0) <= int(r0.get("outside") or 0)


def test_the_adapter_records_the_passes_and_the_run_says_them():
    mesh = _grid(push=(4.0, 3.0))
    sa._CLOCK.update(t0=time.monotonic(), allowed=900.0)
    got, applied = sa._quality_passes((mesh, _applied(mesh)), None)
    rec = applied["quality"]["quality_passes"]
    assert rec["rule"] == "SHMESH-100" and rec["kept"] >= 1
    assert applied["quality"]["passes"][-1] == "extra quality passes (SHMESH-100)"
    assert len(applied["quality"]["passes"]) == len(applied["quality"]["passes_s"])
    said = shell_run.words({"made": True, "elements": got.n_elements, "quads": len(got.quads), "trias": 0,
                            "quality": applied["quality"]})
    assert rec["words"] in said


def test_off_the_passes_change_nothing(monkeypatch):
    monkeypatch.setattr(sa, "QUALITY_PASSES", False)
    mesh = _grid(push=(4.0, 3.0))
    sa._CLOCK.update(t0=time.monotonic(), allowed=900.0)
    out = (mesh, _applied(mesh))
    assert sa._quality_passes(out, None) is out


def test_passes_go_on_past_six_while_each_is_kept(monkeypatch):
    # (each pass moves one node a little and is kept; the tenth moves nothing: the passes stop there, not at 6)
    mesh = _grid(push=(4.0, 3.0))
    calls = {"n": 0}

    def sweep(self, past_cols, deadline, clock):
        calls["n"] += 1
        if calls["n"] < 10:
            self.X[6, 0] += 1e-6
            self.moved += 1
        return True

    monkeypatch.setattr(QP._Sweep, "sweep", sweep)
    monkeypatch.setattr(QP, "kept", lambda old, new, lines: True)
    monkeypatch.setattr(QP, "GAIN_SHARE", None)      # (the stop on small gains is its own test below)
    _got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec["kept"] == 9 and rec["tried"] == 10 and rec["stopped"] == "nothing more to improve"


def test_passes_stop_once_they_no_longer_help(monkeypatch):
    # (round 13: each pass is kept, but from the third on it gains under GAIN_SHARE: the passes stop after GAIN_RUN)
    mesh = _grid(push=(4.0, 3.0))
    calls = {"n": 0}

    def sweep(self, past_cols, deadline, clock):
        calls["n"] += 1
        self.X[6, 0] += 1e-6
        self.moved += 1
        return True

    monkeypatch.setattr(QP._Sweep, "sweep", sweep)
    monkeypatch.setattr(QP, "kept", lambda old, new, lines: True)
    monkeypatch.setattr(QP, "_pass_helps", lambda first, prev, now, lines: calls["n"] < 3)
    _got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec["tried"] == 2 + QP.GAIN_RUN and rec["kept"] == rec["tried"]
    assert rec["stopped"].startswith(f"gains under {QP.GAIN_SHARE:.0%}") and rec["gain_stop"] == rec["tried"]


def _line(op: str = ">=", bound: float = 30.0) -> dict:
    return {"op": op, "bound": bound}


def _s(n: int, worst: float | None, past: bool | None = None) -> dict:
    return {"past": n > 0 if past is None else past, "n": n, "worst": worst, "beyond": n}


@pytest.mark.parametrize("of", ["gained", "left"])
def test_a_pass_helps_by_a_tenth_or_by_bringing_a_line_inside(monkeypatch, of):
    monkeypatch.setattr(QP, "GAIN_OF", of)
    monkeypatch.setattr(QP, "GAIN_SHARE", 0.1)
    ln = [_line()]
    first = [_s(100, 10.0)]
    # (brought inside: always helps)
    assert QP._pass_helps(first, [_s(2, 25.0)], [_s(0, 31.0, past=False)], ln)
    # (100 -> 50 past: half of what is left and of all gained so far)
    assert QP._pass_helps(first, first, [_s(50, 10.0)], ln)
    # (60 -> 59, worst the same: 1 of 41 gained, 1 of 60 left - under a tenth either way)
    assert not QP._pass_helps(first, [_s(60, 10.0)], [_s(59, 10.0)], ln)
    # (no gain at all)
    assert not QP._pass_helps(first, [_s(60, 10.0)], [_s(60, 10.0)], ln)
    # (the worst figure 10 -> 15 degrees of a 30 bound: a quarter of the gap left, and all that was gained on it)
    assert QP._pass_helps(first, [_s(60, 10.0)], [_s(60, 15.0)], ln)
    # (a line not past as the pass began is not judged)
    assert not QP._pass_helps([_s(0, 31.0)], [_s(0, 31.0)], [_s(0, 32.0)], ln)


def test_a_small_last_step_helps_when_it_is_a_tenth_of_what_was_gained(monkeypatch):
    # (the two measures differ: 10 -> 9 past after 100 -> 10 is a tenth of what is left, not of all gained)
    monkeypatch.setattr(QP, "GAIN_SHARE", 0.1)
    ln = [_line()]
    first, prev, now = [_s(100, 10.0)], [_s(10, 10.0)], [_s(9, 10.0)]
    monkeypatch.setattr(QP, "GAIN_OF", "left")
    assert QP._pass_helps(first, prev, now, ln)
    monkeypatch.setattr(QP, "GAIN_OF", "gained")
    assert not QP._pass_helps(first, prev, now, ln)


def test_passes_stop_at_their_share_of_the_time_left():
    mesh = _grid(push=(4.0, 3.0))
    t = {"now": 0.0}

    def clock():
        t["now"] += 1.0
        return t["now"]

    _got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=1000.0, clock=clock)
    # (the clock moves one second a read: the passes' own stop is PASS_SHARE of the 999 s left when they began)
    assert 0.0 < QP.PASS_SHARE < 1.0
    assert rec["seconds"] <= QP.PASS_SHARE * 999.0 + 50.0


R_CYL, N_CYL = 50.0, 12


class _Cylinder:
    """The CAD of a cylinder r = R_CYL: its surface, and its two rims z = 0 and z = top."""

    def __init__(self, top: float):
        self.top = top

    def face(self, k, nodes, tol):
        return bool(np.abs(np.hypot(nodes[:, 0], nodes[:, 1]) - R_CYL).max() <= tol)

    def surface(self, k, pts):
        out = np.array(pts, dtype=float)
        out[:, :2] *= (R_CYL / np.hypot(out[:, 0], out[:, 1]))[:, None]
        return out

    def curve(self, pts, tol):
        if not self.face(0, pts, tol):
            return None
        for c, z in ((1, 0.0), (2, self.top)):
            if np.abs(pts[:, 2] - z).max() <= tol:
                return c
        return None

    def on_curve(self, c, pts):
        out = self.surface(0, pts)
        out[:, 2] = 0.0 if c == 1 else self.top
        return out


def _cylinder_patch(turn: tuple[int, int] = (2, 2), by: float = 0.0) -> tuple[ShellMesh, _Cylinder]:
    """A quarter of a cylinder r = R_CYL in N_CYL x N_CYL quads; node `turn` (i round, j up) turned by `by` of a
    step round the axis (it stays on the surface)."""
    n = N_CYL
    dth = 0.5 * math.pi / n
    h = R_CYL * dth
    pts = []
    for j in range(n + 1):
        for i in range(n + 1):
            th = (i + (by if (i, j) == turn else 0.0)) * dth
            pts.append([R_CYL * math.cos(th), R_CYL * math.sin(th), j * h])
    quads = np.array([[j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i]
                      for j in range(n) for i in range(n)], dtype=np.int64)
    m = ShellMesh(xyz=np.array(pts), quads=quads, trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(n * n, 2.0),
                  mid=["Mid_1"] * (n * n), part=["Part_1"] * (n * n), groups={"Part_1": np.arange(n * n)})
    m.face_of = np.zeros(n * n, dtype=np.int64)  # type: ignore[attr-defined]
    m.held = np.zeros(len(pts), dtype=bool)  # type: ignore[attr-defined]
    return m, _Cylinder(n * h)


def test_a_node_of_a_curved_face_moves_on_its_cad_surface():
    mesh, cyl = _cylinder_patch((5, 5), 0.85)
    before = SQ.measure(mesh)["figures"]["aspect"]["worst"]
    assert before > 5.0
    # without the CAD nothing of a curved face moves
    got0, _a, rec0 = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec0["kept"] == 0 and np.array_equal(got0.xyz, mesh.xyz)
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None, project=cyl)
    assert rec["kept"] >= 1 and rec["on_cad"]
    assert SQ.measure(got)["figures"]["aspect"]["worst"] < before
    assert np.allclose(np.hypot(got.xyz[:, 0], got.xyz[:, 1]), R_CYL, rtol=0, atol=1e-9)
    assert np.array_equal(got.quads, mesh.quads) and SQ.measure(got)["valid"]


def test_a_node_on_a_curved_rim_slides_on_its_cad_curve_and_corners_never_move():
    mesh, cyl = _cylinder_patch((5, 0), 0.85)
    k = 5
    before = SQ.measure(mesh)["figures"]["aspect"]["worst"]
    assert before > 5.0
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None, project=cyl)
    assert rec["kept"] >= 1
    assert got.xyz[k, 2] == pytest.approx(0.0, abs=1e-12)
    assert math.hypot(got.xyz[k, 0], got.xyz[k, 1]) == pytest.approx(R_CYL, abs=1e-9)
    assert math.atan2(got.xyz[k, 1], got.xyz[k, 0]) < math.atan2(mesh.xyz[k, 1], mesh.xyz[k, 0])
    n = N_CYL
    corners = [0, n, n * (n + 1), (n + 1) ** 2 - 1]
    assert np.array_equal(got.xyz[corners], mesh.xyz[corners])
    assert SQ.measure(got)["figures"]["aspect"]["worst"] < before


def test_the_cad_projector_finds_a_face_and_its_rim_on_the_brep(tmp_path):
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import shell_cad_projector as CP

    brep = tmp_path / "cyl.brep"
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("cyl")
        gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 10, 5)      # (cm: mm = 10)
        gmsh.model.occ.synchronize()
        gmsh.write(str(brep))
    finally:
        gmsh.finalize()
    th = np.linspace(0.1, 1.2, 7)
    on = np.c_[50 * np.cos(th), 50 * np.sin(th), np.linspace(10, 90, 7)]
    rim = np.c_[50 * np.cos(th[:3]), 50 * np.sin(th[:3]), np.zeros(3)]
    with CP.opened(brep, 10.0) as cad:
        assert cad.face(7, on, 1e-6)
        assert not cad.face(8, on * 1.01, 1e-6)          # (a face not on the CAD: never moved)
        put = cad.surface(7, on * 1.05)
        assert np.allclose(np.hypot(put[:, 0], put[:, 1]), 50.0, atol=1e-6)
        c = cad.curve(rim, 1e-6)
        assert c is not None and cad.curve(on[:3], 1e-6) is None
        slid = cad.on_curve(c, rim + [0.0, 0.0, 3.0])
        assert np.allclose(slid[:, 2], 0.0, atol=1e-6)


def test_the_line_shows_as_many_digits_as_the_change_needs():
    rec = {"kept": 1, "seconds": 2.0, "lines": [{"kind": "ours", "id": "max_angle_tria", "deg": True, "improved": True,
                                                "name": "largest triangle corner angle", "before": 179.862,
                                                "after": 179.742, "n_before": 3, "n_after": 3}]}
    assert QP._words(rec) == ("Improved the largest triangle corner angle from 179.9 to 179.7 degrees in 1 extra "
                              "pass (2 s of the time left).")


def _two_bad(n: int = 8, h: float = 10.0) -> tuple[ShellMesh, int, int]:
    """An n x n plate with two nodes pushed: `good` (the worse) and `bad` (a smaller push, far from it)."""
    m = _grid(n, h)
    good, bad = 2 * (n + 1) + 2, 5 * (n + 1) + 5
    m.xyz[good, :2] += (4.5, 3.5)
    m.xyz[bad, :2] += (-3.5, -3.0)
    return m, good, bad


def test_one_bad_patch_is_set_aside_and_the_good_patches_of_its_pass_kept(monkeypatch):
    # (the whole-mesh measure finds a row worse whenever node `bad` moves - something the local guard cannot see:
    # the pass is kept up to that patch, the patch set aside, and the passes go on without it)
    mesh, good, bad = _two_bad()
    xyz0 = mesh.xyz.copy()
    real = QP._measure

    def measure(m, applied, requirements):
        st = real(m, applied, requirements)
        if not np.array_equal(m.xyz[bad], xyz0[bad]):
            st["q"] = dict(st["q"], invalid=int(st["q"].get("invalid") or 0) + 1)
        return st

    monkeypatch.setattr(QP, "_measure", measure)
    before = SQ.measure(mesh)["figures"]["jacobian_ratio"]["worst"]
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec["kept"] >= 1 and rec["set_aside"] >= 1
    assert np.array_equal(got.xyz[bad], xyz0[bad])
    assert not np.array_equal(got.xyz[good], xyz0[good])
    assert SQ.measure(got)["figures"]["jacobian_ratio"]["worst"] > before
    assert np.array_equal(got.quads, mesh.quads) and SQ.measure(got)["valid"]


def test_passes_with_no_gain_go_on_a_few_then_the_mesh_as_made_is_handed_back(monkeypatch):
    # (no pass is better, none worse: they go on STALL_MAX in a row, then the final gate hands back the mesh as made)
    mesh = _grid(push=(4.0, 3.0))
    xyz0 = mesh.xyz.copy()

    def sweep(self, past_cols, deadline, clock):
        self.X[6, 0] += 1e-6
        self.moved += 1
        return True

    monkeypatch.setattr(QP._Sweep, "sweep", sweep)
    monkeypatch.setattr(QP, "kept", lambda old, new, lines: False)
    monkeypatch.setattr(QP, "_not_worse", lambda old, new, lines: True)
    got, applied, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec["tried"] == QP.STALL_MAX and rec["kept"] == 0 and rec["moved"] == 0
    assert got is mesh and np.array_equal(got.xyz, xyz0) and "did not improve" in rec["words"]


class _Counted(_Cylinder):
    """The cylinder's CAD, counting the points put on it; `face` says every face is on it."""

    def __init__(self, top: float):
        super().__init__(top)
        self.sizes: list[int] = []

    def face(self, k, nodes, tol):
        return True

    def surface(self, k, pts):
        self.sizes.append(len(pts))
        return super().surface(k, pts)


def test_on_the_cad_only_the_best_few_candidates_are_put_on_it():
    mesh, cyl = _cylinder_patch((5, 5), 0.85)
    cad = _Counted(cyl.top)
    before = SQ.measure(mesh)["figures"]["aspect"]["worst"]
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None, project=cad)
    assert rec["kept"] >= 1 and SQ.measure(got)["figures"]["aspect"]["worst"] < before
    assert cad.sizes and max(cad.sizes) <= QP.PROJECT_MAX
    assert np.allclose(np.hypot(got.xyz[:, 0], got.xyz[:, 1]), R_CYL, rtol=0, atol=1e-9)
    assert rec["cad_s"] >= 0.0


def test_a_node_off_its_cad_surface_never_moves_though_its_face_is_known():
    mesh, _cyl = _cylinder_patch((5, 5), 0.85)
    k = 5 * (N_CYL + 1) + 5
    mesh.xyz[k, :2] *= 1.02                       # (off the surface: a face made again round it)
    cad = _Counted(_cyl.top)
    got, _a, rec = QP.run(mesh, _applied(mesh), None, deadline=None, project=cad)
    assert np.array_equal(got.xyz[k], mesh.xyz[k])


def test_the_cad_projector_reads_a_sample_of_a_faces_nodes(tmp_path):
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import shell_cad_projector as CP

    brep = tmp_path / "cyl.brep"
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("cyl")
        gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 10, 5)
        gmsh.model.occ.synchronize()
        gmsh.write(str(brep))
    finally:
        gmsh.finalize()
    n = 8 * CP.SAMPLE
    th = np.linspace(0.1, 1.2, n)
    on = np.c_[50 * np.cos(th), 50 * np.sin(th), np.linspace(10, 90, n)]
    picked = set(np.unique(np.linspace(0, n - 1, CP.SAMPLE).astype(int)).tolist())
    off = next(i for i in range(n) if i not in picked)
    on[off, :2] *= 1.01
    with CP.opened(brep, 10.0) as cad:
        assert cad.face(7, on, 1e-6)              # (the sample is on it; the node off it is checked when it moves)
        put = cad.surface(7, on[[off]])
        assert float(np.linalg.norm(put[0] - on[off])) > 1e-3


def _edge_pushed() -> ShellMesh:
    """A 4 x 4 plate whose bottom-edge node (20, 0) is pushed along the edge to (27, 0) (its right quad's Jacobian far
    under 0.6), every node inside the plate held: only the edge's nodes may slide."""
    mesh = _grid()
    mesh.xyz[2, 0] = 27.0
    inner = (mesh.xyz[:, 0] % 40 != 0) & (mesh.xyz[:, 1] % 40 != 0)
    mesh.held = inner.copy()  # type: ignore[attr-defined]
    return mesh


def test_a_node_no_usual_step_helps_is_tried_at_finer_steps(monkeypatch):
    monkeypatch.setattr(QP, "STEPS", (5.0,))           # (every usual step folds an element round it)
    monkeypatch.setattr(QP, "PASS_MAX", 20)
    mesh = _edge_pushed()
    before = SQ.measure(mesh)["figures"]["jacobian_ratio"]["worst"]
    got, _applied2, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec is not None and rec["kept"] >= 1
    assert SQ.measure(got)["figures"]["jacobian_ratio"]["worst"] > before
    held = np.flatnonzero(mesh.held)
    assert np.array_equal(got.xyz[held], mesh.xyz[held])
    assert np.array_equal(got.xyz[[0, 4, 20, 24]], mesh.xyz[[0, 4, 20, 24]])
    assert np.allclose(got.xyz[:5, 1], 0.0)                  # (the edge's nodes stay on it)


def test_two_neighbouring_nodes_move_together_where_no_single_node_can(monkeypatch):
    monkeypatch.setattr(QP, "STEPS", (5.0,))
    monkeypatch.setattr(QP, "FINE_STEPS", (5.0,), raising=False)
    monkeypatch.setattr(QP, "PASS_MAX", 20)
    mesh = _edge_pushed()
    before = SQ.measure(mesh)["figures"]["jacobian_ratio"]["worst"]
    got, _applied2, rec = QP.run(mesh, _applied(mesh), None, deadline=None)
    assert rec is not None and rec["kept"] >= 1 and rec["paired"] >= 1
    assert SQ.measure(got)["figures"]["jacobian_ratio"]["worst"] > before
    held = np.flatnonzero(mesh.held)
    assert np.array_equal(got.xyz[held], mesh.xyz[held])
    assert np.array_equal(got.xyz[[0, 4, 20, 24]], mesh.xyz[[0, 4, 20, 24]])
    assert np.allclose(got.xyz[:5, 1], 0.0)
    assert SQ.measure(got)["valid"]


def _fan(n: int = 16, r: float = 10.0) -> ShellMesh:
    """A flat n-gon face of n triangles round one middle node: their middle angle (360/n degrees) no node move or
    diagonal turn can mend; only meshing the patch again can."""
    th = 2.0 * np.pi * np.arange(n) / n
    xyz = np.vstack([[0.0, 0.0, 0.0], np.c_[r * np.cos(th), r * np.sin(th), np.zeros(n)]])
    trias = np.array([[0, 1 + i, 1 + (i + 1) % n] for i in range(n)], dtype=np.int64)
    m = ShellMesh(xyz=xyz, quads=np.zeros((0, 4), dtype=np.int64), trias=trias, wall=np.full(n, 2.0),
                  mid=["Mid_1"] * n, part=["Part_1"] * n, groups={"Part_1": np.arange(n)})
    m.face_of = np.zeros(n, dtype=np.int64)  # type: ignore[attr-defined]
    m.held = np.zeros(len(xyz), dtype=bool)  # type: ignore[attr-defined]
    return m


class _Stub:
    """A remesher that offers one given way, and counts its calls."""

    def __init__(self, way):
        self.way, self.calls = way, 0

    def patch(self, ring, normal, size):
        self.calls += 1
        return [self.way(np.asarray(ring))] if self.way else []


def test_a_patch_no_node_move_can_mend_is_meshed_again_with_its_outline_untouched():
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import shell_cad_projector as CP

    mesh = _fan()
    before = SQ.measure(mesh)["figures"]["min_angle_tria"]["worst"]
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("on_cad")
        got, applied, rec = QP.run(mesh, _applied(mesh), None, deadline=None, remesher=CP.PatchMesher(gmsh))
        assert gmsh.model.getCurrent() == "on_cad"
    finally:
        gmsh.finalize()
    assert rec is not None and rec["remeshed"] == 1 and rec["kept"] >= 1
    q = SQ.measure(got)
    assert q["valid"] and q["figures"]["min_angle_tria"]["worst"] > before + 10.0
    # the outline's nodes are all there, where they stood; every new node inside it, on the face's plane
    for p in mesh.xyz[1:]:
        assert np.linalg.norm(got.xyz - p, axis=1).min() == 0.0
    assert np.allclose(got.xyz[:, 2], 0.0) and np.hypot(got.xyz[:, 0], got.xyz[:, 1]).max() <= 10.0 + 1e-9
    assert got.n_elements != mesh.n_elements and len(got.face_of) == got.n_elements
    assert len(got.held) == len(got.xyz) and np.array_equal(got.groups["Part_1"], np.arange(got.n_elements))
    assert "re-making 1 small patch of elements" in rec["words"]
    assert applied["quality"]["figures"]["min_angle_tria"]["worst"] == pytest.approx(q["figures"]["min_angle_tria"]["worst"])


def test_a_patch_meshed_again_worse_is_left_as_made():
    # (the stub's way: the same fan round a middle node pushed near the rim - worse)
    stub = _Stub(lambda ring: (np.array([[8.0, 0.0, 0.0]]), [[len(ring), i, (i + 1) % len(ring)]
                                                            for i in range(len(ring))]))
    mesh = _fan()
    got, _applied2, rec = QP.run(mesh, _applied(mesh), None, deadline=None, remesher=stub)
    assert stub.calls >= 1 and rec is not None and rec["remeshed"] == 0
    assert got is mesh and np.array_equal(got.trias, mesh.trias)


def test_a_patch_round_a_held_node_is_never_meshed_again():
    stub = _Stub(None)
    mesh = _fan()
    mesh.held[0] = True
    got, _applied2, rec = QP.run(mesh, _applied(mesh), None, deadline=None, remesher=stub)
    assert stub.calls == 0 and rec is not None and rec["remeshed"] == 0 and got is mesh


def test_why_a_patch_and_a_node_move_were_refused_is_recorded():
    # (the fan's middle node: no move betters its worst triangle; a worse re-make; a held middle node)
    stub = _Stub(lambda ring: (np.array([[8.0, 0.0, 0.0]]), [[len(ring), i, (i + 1) % len(ring)]
                                                            for i in range(len(ring))]))
    _got, _a, rec = QP.run(_fan(), _applied(_fan()), None, deadline=None, remesher=stub)
    assert rec["refused"] and all(isinstance(v, int) and v > 0 for v in rec["refused"].values())
    assert set(rec["refused"]) <= {"no_gain", "would_fold", "another_figure_worse", "edge_past_typed",
                                   "pair_no_gain", "pair_would_fold", "pair_another_figure_worse", "set_aside"}
    assert rec["refused_patch"] and set(rec["refused_patch"]) & {"another_figure_worse", "not_better", "fold"}
    held = _fan()
    held.held[0] = True
    _got, _a, rec2 = QP.run(held, _applied(held), None, deadline=None, remesher=_Stub(None))
    assert set(rec2["refused_patch"]) == {"held_node_inside"}


def test_the_census_names_a_cad_corner_no_element_can_meet_and_a_sliver_face(monkeypatch):
    # face 0: one triangle in a 6-degree CAD corner; face 1: a strip of 1 mm x 10 mm quads beside a 10 mm plate
    t = math.radians(6.0)
    xyz = [[0, 0, 0], [100, 0, 0], [100 * math.cos(t), 100 * math.sin(t), 0]]
    quads = []
    base = len(xyz)
    for i in range(6):
        xyz += [[200 + 10 * i, 0, 0], [200 + 10 * i, 1, 0]]
    for i in range(5):
        a = base + 2 * i
        quads.append([a, a + 2, a + 3, a + 1])
    mesh = ShellMesh(xyz=np.array(xyz, dtype=float), quads=np.array(quads, dtype=np.int64),
                     trias=np.array([[0, 1, 2]], dtype=np.int64), wall=np.full(6, 2.0), mid=["Mid_1"] * 6,
                     part=["Part_1"] * 6, groups={"Part_1": np.arange(6)})
    mesh.face_of = np.array([1] * 5 + [0], dtype=np.int64)  # type: ignore[attr-defined]
    mesh.held = np.zeros(len(xyz), dtype=bool)  # type: ignore[attr-defined]
    applied = _applied(mesh)
    lines = QP.lines_in_force(applied, None)
    got = QP.census(mesh, applied, lines, None)
    assert got["elements_past"] >= 6 and not got["cut"]
    assert got["by_cause"].get("b_cad_corner") == 1
    assert got["measures"]["b_cad_corner"]["min"] == pytest.approx(6.0, abs=0.2)
    assert got["by_cause"].get("c_sliver_face") == 5 and got["any_label"].get("a_strip", 0) >= 3
    assert sum(sum(r.values()) for r in got["by_row"].values()) >= got["elements_past"]


def test_the_census_names_the_structure_that_holds_an_element():
    # (the sliver strip of the test above, its nodes held by a mapped strip: the census says which structure)
    from meshpipeline.engines.gmsh import shell_adapter as SA
    xyz = []
    for i in range(6):
        xyz += [[10 * i, 0, 0], [10 * i, 1, 0]]
    quads = [[2 * i, 2 * i + 2, 2 * i + 3, 2 * i + 1] for i in range(5)]
    mesh = ShellMesh(xyz=np.array(xyz, dtype=float), quads=np.array(quads, dtype=np.int64),
                     trias=np.zeros((0, 3), dtype=np.int64), wall=np.full(5, 2.0), mid=["Mid_1"] * 5,
                     part=["Part_1"] * 5, groups={"Part_1": np.arange(5)})
    mesh.face_of = np.array([1] * 5, dtype=np.int64)  # type: ignore[attr-defined]
    pinned = np.ones(len(xyz), dtype=bool)
    mesh.held = pinned  # type: ignore[attr-defined]
    mesh.held_kind = SA._held_kinds(len(xyz), mesh.quads, [1] * 5, len(xyz), [], set(), {1}, pinned)  # type: ignore[attr-defined]
    applied = _applied(mesh)
    got = QP.census(mesh, applied, QP.lines_in_force(applied, None), None)
    assert got["by_cause"].get("f_held") == got["elements_past"] > 0
    assert got["held_by"] == {"mapped strip": got["elements_past"]}
    # (a ring's own nodes, added after the mesher's: the elements touching them are the ring's)
    kinds = SA._held_kinds(4, np.array([[0, 1, 2, 3]]), [0], 3, [(3, 4, SA.HELD_KINDS.index("ring"))], set(), set(),
                           np.array([True, True, True, True]))
    assert [SA.HELD_KINDS[k] for k in kinds.tolist()] == ["ring"] * 4


def test_a_mesh_carrying_held_kinds_is_still_meshed_again():
    # (round 10: every mesh the mesher makes carries held_kind since the census; the re-mesh refused such a mesh as
    # "mesh data not carried", so no patch was re-made on any part)
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import shell_cad_projector as CP

    mesh = _fan()
    mesh.held_kind = np.zeros(len(mesh.xyz), dtype=np.int8)  # type: ignore[attr-defined]
    gmsh.initialize(["-noenv"], readConfigFiles=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("on_cad")
        got, _applied2, rec = QP.run(mesh, _applied(mesh), None, deadline=None, remesher=CP.PatchMesher(gmsh))
    finally:
        gmsh.finalize()
    assert rec is not None and rec["remeshed"] == 1
    assert "mesh_data_not_carried" not in (rec.get("refused_patch") or {})
    assert len(got.held_kind) == len(got.xyz)
