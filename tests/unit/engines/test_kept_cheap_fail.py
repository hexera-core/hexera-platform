# Responsibility: Verify MIXED-21..25, A FAILING KEPT-SOLID ATTEMPT IS CHEAP AND A KEPT ONE NEVER COSTS NASTRAN'S
# LIMITS: the split's distances read box first give the same answers (MIXED-21); a wall slab OCC calls invalid stops
# the split before its cut (MIXED-24); a thick region's growth past its bound stops the attempt, said (MIXED-25); an
# overlap quad past Nastran's limits is cut into two triangles (MIXED-22); a kept mesh still past them is held for the
# all-shell mesh to be weighed against (MIXED-23).
# Boundaries: synthetic OCC boxes and small numpy meshes; one forked child per guard test; no mesher.
from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox  # noqa: E402
from OCP.gp import gp_Pnt  # noqa: E402

from meshpipeline.cad import midsurface as ms  # noqa: E402
from meshpipeline.cad import mixed_split as mx  # noqa: E402
from meshpipeline.engines import mixed_mesh as mm  # noqa: E402
from meshpipeline.engines import mixed_run as mr  # noqa: E402
from meshpipeline.engines import shell_quality as sq  # noqa: E402
from meshpipeline.engines import shell_run as sr  # noqa: E402
from meshpipeline.engines.shell_mesh import ShellMesh  # noqa: E402


def _faces():
    out = []
    for k in range(6):
        b = BRepPrimAPI_MakeBox(gp_Pnt(10.0 * k, 0.3 * k, 0.0), 4.0, 2.0 + k, 1.0).Shape()
        out.append(ms._faces(b)[k % 6])
    return out


def test_box_first_distances_give_the_brute_force_answers():
    faces = _faces()
    boxes = [ms._box(f) for f in faces]
    for p in ([0.5, 0.5, 0.2], [24.0, 1.0, 3.0], [55.0, -2.0, 0.5], [-30.0, 0.0, 0.0]):
        v = ms._vertex(p)
        brute = min(range(len(faces)), key=lambda i: (ms._dist(v, faces[i]), i))
        assert mx._nearest(p, faces, boxes) == brute
    other = faces[2]
    for r in (0.0, 1.0, 9.0, 40.0):
        reach = [r] * len(faces)
        brute = [k for k, f in enumerate(faces) if ms._dist(f, other) <= r]
        assert mx._within(faces, boxes, other, reach, ms._box(other)) == brute
    box = ms._box(other)
    p = np.array([500.0, 0.0, 0.0])
    assert mx._near(other, p, 1.0, box) is False and mx._near(other, p, 1.0) is False


def test_a_wall_slab_occ_calls_invalid_stops_the_split_before_its_cut(monkeypatch):
    body = BRepPrimAPI_MakeBox(10.0, 10.0, 2.0).Shape()
    kept = [(ms._faces(body)[0], 2.0, 0.0)]
    assert mx._invalid([body, mx.slab(kept[0][0], 1.0)]) == 0
    monkeypatch.setattr(mx, "_invalid", lambda shapes: 1)
    called = []
    monkeypatch.setattr(mx, "_bop", lambda *a, **k: called.append(a) or None)
    got = mx.split_body(mx._solids(body)[0], kept, [], scale=1.0)
    assert got.ok is False and called == []
    assert got.why == mx.SLAB_INVALID.format(n=1, m=1)


def _burn(cpu_s: float) -> None:
    """Work for `cpu_s` CPU seconds (MIXED-27: the bounds count work, not wall seconds)."""
    stop = time.process_time() + cpu_s
    while time.process_time() < stop:
        pass


def test_a_growth_past_its_bound_stops_the_attempt_and_says_so(tmp_path):
    def slow():
        mx._step(mx.GROW_STEP)
        _burn(5.0)
        return 1

    t0 = time.time()
    with pytest.raises(mr._Stop) as got:
        sr._guarded(slow, 30.0, tmp_path, grow_s=0.5)
    assert time.time() - t0 < 4.0
    assert str(got.value) == sr.KEPT_GROW.format(s=0.5)


def test_a_growth_inside_its_bound_leaves_the_attempt_to_finish(tmp_path):
    def quick():
        mx._step(mx.GROW_STEP)
        _burn(0.2)
        mx._step(None)
        _burn(0.8)                    # past the growth's bound, but no growth runs now
        return 7

    assert sr._guarded(quick, 30.0, tmp_path, grow_s=0.5) == 7


def test_a_growth_waiting_for_the_core_is_not_counted_against_its_bound(tmp_path):
    # MIXED-27: a growth that does no work (here: sleeps, as one waiting for a busy core does) is not stopped by
    # MIXED-25's bound; only MIXED-12's wall guard stops it
    import os

    if sr._cpu_group(os.getpid()) is None:
        pytest.skip("no /proc: the bounds count wall seconds here")

    def waiting():
        mx._step(mx.GROW_STEP)
        time.sleep(2.0)
        mx._step(None)
        return 7

    assert sr._guarded(waiting, 30.0, tmp_path, grow_s=0.5) == 7


def _mixed():
    # one wall quad and two overlap quads: a square and one opened to 167 degrees at a corner (past Q4_IAMAX)
    xyz = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                    [2, 0, 0], [2, 1, 0], [3, 0, 0], [2.4, 0.3, 0], [3, 1, 0], [0.5, 2, 0]], dtype=float)
    quads = np.array([[0, 1, 2, 3], [1, 4, 5, 2], [4, 6, 8, 7]], dtype=np.int64)
    trias = np.array([[3, 2, 9]], dtype=np.int64)
    shell = ShellMesh(xyz=xyz, quads=quads, trias=trias, wall=np.array([1.0, 2.0, 3.0, 4.0]),
                      mid=["Mid_1", "Mid_2", "Mid_3", "Mid_4"], part=["Part_1"] * 4,
                      groups={mm.OVERLAP_GROUP: np.array([1, 2]), "all": np.arange(4)})
    return mm.MixedMesh(shell=shell, solid=None, part="Part_1", ties=[], links=[], n_overlap=2)


def test_an_overlap_quad_past_nastran_is_cut_into_two_triangles_and_nothing_else_moves():
    mixed = _mixed()
    before = sq.nastran_past(mixed.shell)
    assert before[2] and not before[:2].any()
    assert mm.split_past_overlap(mixed) == 1
    sh = mixed.shell
    assert len(sh.quads) == 2 and len(sh.trias) == 3 and not sq.nastran_past(sh).any()
    assert sh.wall.tolist() == [1.0, 2.0, 4.0, 3.0, 3.0] and sh.mid == ["Mid_1", "Mid_2", "Mid_4", "Mid_3", "Mid_3"]
    assert sh.groups[mm.OVERLAP_GROUP].tolist() == [1, 3, 4] and mixed.n_overlap == 3
    assert sh.groups["all"].tolist() == [0, 1, 2, 3, 4]
    assert mm.split_past_overlap(mixed) == 0


def test_a_kept_mesh_past_nastran_is_held_for_the_all_shell_mesh(tmp_path, monkeypatch):
    sub = {"shell_quality": {"valid": True, "within_nastran": False}, "solid_quality": {"valid": True}}
    # MIXED-26: the all-shell mesh is made beside the attempt, so the weighing happens once the attempt is in
    monkeypatch.setattr(sr, "_wait", lambda *a, **k: ("joined", sub))
    rec = {"extract_s": 1.0, "made": True, "quality": {"valid": True, "within_nastran": True}}
    h = {"t0": time.time(), "budget": 300.0, "done_s": 5.0}
    assert sr._kept_finish(tmp_path, h, {}, rec, {}, plain_s=10.0, source="", requirements=None) is False
    assert rec["made"] is True and "kept_solid" not in rec       # the all-shell mesh stays delivered
    assert rec["kept_solid_why"] == sr.KEPT_NOT.format(why=sr.KEPT_PAST)
    assert sr._plain_inside({"made": True, "quality": {"valid": True, "within_nastran": True}})
    assert not sr._plain_inside({"made": True, "quality": {"valid": True, "within_nastran": False}})
    assert not sr._plain_inside({"made": False, "quality": {"within_nastran": True}})
