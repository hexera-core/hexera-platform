# Responsibility: Verify the stamped sheet rules of the shell mesh under the user's shortest edge (SHMESH-60..63): a
# strip narrower than that edge (a fillet, a narrow flank) is closed to its middle line after meshing, chosen
# narrowest first; a bead flank is never cut into rows under the shortest edge; a short CAD edge is not graded to
# below it; and on a real beaded sheet the elements with an edge under the shortest edge drop from most of the bead to
# a few, with the shape kept.
# Boundaries: pure functions on plain data and a tiny mesh, then the real surface run on a small beaded sheet built
# here (a few seconds each). No solver.
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines.gmsh import shell_adapter as SA


# ------------------------------------------------------------------------------------------- which strips close
def test_two_touching_narrow_fillets_both_close_when_together_they_are_under_the_shortest_edge():
    thin = {1: ((10, 11), (20, 21), 0.8), 2: ((11, 12), (22, 23), 0.8)}
    assert SA._narrow_choose(None, thin, 2.0) == [1, 2]


def test_of_two_wider_touching_strips_one_closes_and_the_other_keeps_a_row_over_the_shortest_edge():
    thin = {1: ((10, 11), (20, 21), 1.5), 2: ((11, 12), (22, 23), 1.6)}
    assert SA._narrow_choose(None, thin, 2.0) == [1]       # 2 then holds 1.6 + 0.75 = 2.35 mm


def test_a_flank_between_two_closed_fillets_keeps_its_row():
    # fillet (1.0) - flank (1.9) - fillet (1.0): both fillets close (narrowest first), the flank's row is then
    # 1.9 + 0.5 + 0.5 = 2.9 mm and stays
    thin = {1: ((10, 11), (20, 21), 1.0), 2: ((11, 12), (22, 23), 1.9), 3: ((12, 13), (24, 25), 1.0)}
    assert SA._narrow_choose(None, thin, 2.0) == [1, 3]


def test_strips_that_share_no_side_close_on_their_own():
    thin = {1: ((10, 11), (20, 21), 1.9), 2: ((30, 31), (22, 23), 2.5)}
    assert SA._narrow_choose(None, thin, 2.0) == [1]


# ------------------------------------------------------------------------------------------- closing a strip
OCP = pytest.importorskip("OCP.BRepBuilderAPI")


def _flat_face():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Dir, gp_Pln, gp_Pnt

    return BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), -100, 100, -100, 100).Face()


def _three_rows():
    """Two rows of 4 x 4 quads with a 1 mm strip of quads between them (face 1), all in z = 0, x along."""
    ys = [0.0, 4.0, 5.0, 9.0]
    xyz = np.array([[x, y, 0.0] for y in ys for x in (0.0, 4.0, 8.0, 12.0)])
    Q, f = [], []
    for r in range(3):
        for c in range(3):
            a = 4 * r + c
            Q.append([a, a + 1, a + 5, a + 4])
            f.append(1 if r == 1 else 0)
    return xyz, np.array(Q), f


def test_a_closed_strip_leaves_its_neighbours_meeting_on_its_middle_line():
    xyz, Q, f = _three_rows()
    pairs = [(4 + i, 8 + i, 1) for i in range(4)]
    facts = [{"normal_face": _flat_face()}, {"normal_face": _flat_face()}]
    xyz2, Q2, T2, qf, tf, new, closed = SA._close_narrow(xyz, Q, np.zeros((0, 3), dtype=np.int64), f, [], pairs,
                                                         facts, 1.0)
    assert closed == [1] and len(T2) == 0 and len(Q2) == 6 and set(qf) == {0}
    assert sorted(new) == [4, 5, 6, 7]
    assert np.allclose(xyz2[new][:, 1], 4.5)                 # the middle of the 1 mm strip
    # every element left is a whole quad, none with an edge across the strip
    e = np.linalg.norm(xyz2[Q2] - xyz2[np.roll(Q2, -1, axis=1)], axis=2)
    assert e.min() >= 4.0 - 1e-9


def test_a_strip_whose_closing_would_turn_an_element_over_is_left():
    xyz, Q, f = _three_rows()
    # a neighbour's node pulled across the strip: the merge would fold its quad, so the strip stays
    xyz[1, 1] = 4.8
    pairs = [(4 + i, 8 + i, 1) for i in range(4)]
    facts = [{"normal_face": _flat_face()}, {"normal_face": _flat_face()}]
    _x, Q2, _t, _qf, _tf, new, closed = SA._close_narrow(xyz, Q, np.zeros((0, 3), dtype=np.int64), f, [], pairs,
                                                         facts, 1.0)
    assert closed == [] and new == [] and len(Q2) == len(Q)


# ------------------------------------------------------------------------------------------- flank and short edges
def test_a_flank_is_never_cut_into_rows_under_the_shortest_edge():
    from meshpipeline.engines import shell_plan as plan_rules

    # 3.4 mm flank, 4 mm size: two across would be 1.7 mm; with a 2 mm shortest edge it keeps one
    assert plan_rules.flank_size(3.4, size=4.0, wall=1.0) == pytest.approx(1.7)
    across = min(plan_rules.FLANK_ACROSS, int(math.floor(3.4 / 2.0 + 1e-9)))
    assert across == 1


# ------------------------------------------------------------------------------------------- a real beaded sheet
gmsh = pytest.importorskip("gmsh")


def _beaded_sheet(path: Path, r: float = 1.0) -> Path:
    """A 120 x 80 sheet with one drafted bead 80 x 12 x 4 mm, every edge of the bead rounded `r` (as stamped), its
    top skin written as a surface STEP."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        box = occ.addBox(-60, -40, -10, 120, 80, 10)
        h, w, ln, below = 4.0, 12.0, 80.0, 2.0
        d = (h + below) / math.tan(math.radians(60))
        r1, r0, a = w / 2, w / 2 + d, ln / 2 - w / 2

        def loop(pts):
            tg = [occ.addPoint(*p) for p in pts]
            return occ.addPlaneSurface([occ.addCurveLoop([occ.addLine(tg[i], tg[(i + 1) % 4]) for i in range(4)])])

        s = loop([(-a, -r0, -below), (-a, r0, -below), (-a, r1, h), (-a, -r1, h)])
        bead = [t for t in occ.extrude([(2, s)], 2 * a, 0, 0) if t[0] == 3]
        for sx in (-1, 1):
            hs = loop([(sx * a, 0, -below), (sx * a, r0, -below), (sx * a, r1, h), (sx * a, 0, h)])
            rv = [t for t in occ.revolve([(2, hs)], sx * a, 0, 0, 0, 0, 1, math.pi if sx < 0 else -math.pi)
                  if t[0] == 3]
            bead = [t for t in occ.fuse(bead, rv)[0] if t[0] == 3]
        vol = [t for t in occ.fuse([(3, box)], bead)[0] if t[0] == 3]
        occ.synchronize()
        inner = []
        for _d, f in gmsh.model.getBoundary(vol, oriented=False):
            for _d1, c in gmsh.model.getBoundary([(2, abs(f))], oriented=False):
                b = gmsh.model.getBoundingBox(1, abs(c))
                if abs(b[0]) < 59 and abs(b[3]) < 59 and abs(b[1]) < 39 and abs(b[4]) < 39 and b[2] > -1:
                    inner.append(abs(c))
        # the bead's own smooth seams (flank to end cone) are not rounded: only its sharp edges
        sharp = []
        for c in sorted(set(inner)):
            up = list(gmsh.model.getAdjacencies(1, c)[0])
            lo, hi = gmsh.model.getParametrizationBounds(1, c)
            x = gmsh.model.getValue(1, c, [0.5 * (lo[0] + hi[0])])
            ns = []
            for f in up:
                _p, uv = gmsh.model.getClosestPoint(2, f, x)
                n = np.array(gmsh.model.getNormal(f, uv))
                ns.append(n / np.linalg.norm(n))
            if len(ns) == 2 and abs(float(ns[0] @ ns[1])) < math.cos(math.radians(8)):
                sharp.append(c)
        vol = [t for t in occ.fillet([v[1] for v in vol], sharp, [r]) if t[0] == 3]
        occ.synchronize()
        keep, drop = [], []
        for _d, f in gmsh.model.getBoundary(vol, oriented=False):
            b = gmsh.model.getBoundingBox(2, abs(f))
            outer = (abs(b[0] - b[3]) < 1e-6 and abs(abs(b[0]) - 60) < 1e-6) or \
                    (abs(b[1] - b[4]) < 1e-6 and abs(abs(b[1]) - 40) < 1e-6) or abs(b[5] + 10) < 1e-6
            (drop if outer else keep).append(abs(f))
        occ.remove(vol)
        occ.remove([(2, f) for f in drop], recursive=True)
        occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()
    return path


@pytest.fixture(scope="module")
def sheet(tmp_path_factory) -> Path:
    return _beaded_sheet(tmp_path_factory.mktemp("bead") / "bead.step")


def _run(ws: Path, step: Path, monkeypatch=None, **switches) -> tuple[dict, np.ndarray]:
    from tests.unit.engines.test_surface_requirements import asked

    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines import surface_run

    if monkeypatch is not None:
        for k, v in switches.items():
            monkeypatch.setattr(SA, k, v)
    ws.mkdir(parents=True, exist_ok=True)
    rec = surface_run.run(ws, step, mm_per_unit=1.0, thickness_mm=1.0, said_shell=True, source="bead.step",
                          requirements=asked(4.0))
    back = sw.read_bdf(ws / "shell.bdf")
    xyz = {n: 1000.0 * np.array(p) for n, p in back.nodes.items()}
    shortest = np.array([min(np.linalg.norm(xyz[c[i]] - xyz[c[(i + 1) % len(c)]]) for i in range(len(c)))
                         for c in back.elements.values()])
    return rec, shortest


def test_a_beaded_sheets_fillets_close_and_its_elements_keep_over_the_shortest_edge(tmp_path, sheet, monkeypatch):
    rec, shortest = _run(tmp_path / "on", sheet)
    assert rec["made"] and rec["quality"]["valid"] is True
    applied = json.loads((tmp_path / "on" / "shell_quality.json").read_text())
    assert applied["quality"].get("narrow_closed", 0) >= 4          # the bead's 1 mm fillets
    assert (shortest < 2.0).mean() < 0.02, (shortest < 2.0).mean()
    # the shape is kept: the bead still stands 4 mm (its crown nodes on it; a closed fillet line within 0.05 mm)
    from meshpipeline.engines import shell_writers as sw

    z = 1000.0 * np.array([p[2] for p in sw.read_bdf(tmp_path / "on" / "shell.bdf").nodes.values()])
    assert z.max() == pytest.approx(4.0, abs=0.05)
    # without the rules a fillet keeps a row of 1 mm elements along the whole bead
    _rec, before = _run(tmp_path / "off", sheet, monkeypatch, NARROW_COLLAPSE=False, FLANK_LOW=False,
                        SHORT_SUPPRESS=False, STRIP_LOW=False)
    assert (before < 2.0).mean() > 5 * max((shortest < 2.0).mean(), 0.01)


@pytest.mark.parametrize("source", ["surface", "extracted"])
def test_the_stamped_wrapper_passes_every_argument_of_the_mesh_on(monkeypatch, source):
    # SHMESH-60..69: _mesh_once (stamped steps on for surface files, off for extracted mid-surfaces) hands every
    # parameter of _mesh_once_core on, whatever another step adds to it: once a named list dropped the bolt rings'
    # ul_state on every extracted part
    import inspect

    from meshpipeline.engines.gmsh import shell_adapter as SA

    params = list(inspect.signature(SA._mesh_once_core).parameters.values())
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    keywords = [p for p in params if p.kind == p.KEYWORD_ONLY]
    seen = {}

    def core(*a, **k):
        seen.update(dict(zip([p.name for p in positional], a, strict=False)), **k)
        return "mesh", {}

    monkeypatch.setattr(SA, "_mesh_once_core", core)
    args = [{"source": source} if p.name == "record" else object() for p in positional]
    kwargs = {p.name: object() for p in keywords}
    assert SA._mesh_once(*args, **kwargs) == ("mesh", {})
    assert set(seen) == {p.name for p in params}
    assert all(seen[p.name] is v for p, v in zip(positional, args, strict=True))
    assert all(seen[k] is v for k, v in kwargs.items())
