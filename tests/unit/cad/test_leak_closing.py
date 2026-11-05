# Responsibility: Verify INS-8 (docs/RULES.md), leaks closed for the mesh up to a size the user
# confirms: a thin-wall tube with pinholes the map as drawn floods through is read with its leaks
# closed (its two real ends the openings, each leak listed with its width and place), the size the
# stage proposes covers every leak, the staging closes them and records it, the covers lie on the
# wall, a surface with no leak (a clean tube, a closed solid) reads exactly as before, and the size
# travels from the stage's answer through the stored confirmation to the run's workspace.
# Boundaries: numpy surfaces built here and temporary files. No model, no storage, no engine.
from __future__ import annotations

import math

import numpy as np
import pytest

from meshpipeline.cad import inside_space as ins
from meshpipeline.cad.open_ends import surface

R, L = 0.010, 0.040            # a 20 mm tube, 40 mm long, its wall a sheet of 1 mm triangles
CELLS = 1_500_000               # cubes of about 0.24 mm: a pinhole is two or three of them across
HOLES = ((0.012, 0.0, 0.8), (0.021, math.pi, 0.8), (0.028, 0.5 * math.pi, 0.8), (0.016, 1.5 * math.pi, 0.8))


def _tube(n_around: int = 64, n_along: int = 40, holes=()) -> np.ndarray:
    th = np.linspace(0.0, 2.0 * math.pi, n_around, endpoint=False)
    xs = np.linspace(0.0, L, n_along + 1)
    P = np.array([[x, R * math.cos(t), R * math.sin(t)] for x in xs for t in th])
    F = []
    for i in range(n_along):
        for j in range(n_around):
            a, b = i * n_around + j, i * n_around + (j + 1) % n_around
            c, d = (i + 1) * n_around + j, (i + 1) * n_around + (j + 1) % n_around
            F += [[a, b, d], [a, d, c]]
    T = P[np.asarray(F)]
    keep = np.ones(len(T), dtype=bool)
    C = T.mean(axis=1)
    ring = []
    for x, t, s in holes:
        # A PINHOLE: the triangle nearest the spot keeps only a band round its edge - the middle,
        # `s` of it across, is a hole in the wall
        at = np.array([x, R * math.cos(t), R * math.sin(t)])
        k = int(np.argmin(np.linalg.norm(C - at, axis=1) + np.where(keep, 0.0, np.inf)))
        keep[k] = False
        a, b, c = T[k]
        g = (a + b + c) / 3.0
        ia, ib, ic = (g + s * (v - g) for v in (a, b, c))
        for (o0, o1), (i0, i1) in (((a, b), (ia, ib)), ((b, c), (ib, ic)), ((c, a), (ic, ia))):
            ring += [[o0, o1, i1], [o0, i1, i0]]
    return np.concatenate([T[keep], np.asarray(ring).reshape(-1, 3, 3)]) if ring else T[keep]


@pytest.fixture(scope="module")
def leaky():
    tris = _tube(holes=HOLES)
    mesh = surface(tris)
    plain = ins.read_inside(mesh, cells=CELLS)
    return tris, mesh, plain, ins.read_leaks(mesh, cells=CELLS, plain=plain)


def test_the_map_as_drawn_floods_through_the_pinholes(leaky):
    _tris, _mesh, plain, _closed = leaky
    assert plain.loops_small >= 1               # holes too small for the grid: the map cannot cap them
    assert not plain.has_inside() and not plain.leaks and plain.closed is None


def test_with_the_leaks_closed_the_two_ends_are_the_mouths_and_every_leak_is_listed(leaky):
    _tris, _mesh, _plain, m = leaky
    assert m is not None and m.has_inside() and m.close in ins.LEAK_RADII
    [p] = m.passages()
    ends = sorted(round(float(m.mouth(i).centroid[0]), 4) for i in p.mouths)
    assert ends == [0.0, L]
    assert m.leaks
    hole_at = [np.array([x, R * math.cos(t), R * math.sin(t)]) for x, t, _ in HOLES]
    for s in m.leaks:
        assert 0.0 < s.width < 2.0e-3                               # about a pinhole across
        assert min(float(np.linalg.norm(s.point - h)) for h in hole_at) < 1.5e-3
        assert s.width < 2.0 * p.clearance                          # narrower than the passage
    widths = [s.width for s in m.leaks]
    assert widths == sorted(widths, reverse=True)                   # the widest first
    facts = ins.leak_facts(m)
    assert facts["rule"] == "INS-8" and facts["found"] == len(m.leaks)
    assert facts["close_up_to_mm"] >= max(widths) * 1000.0           # the proposal closes every one
    assert [x["width_mm"] for x in facts["leaks"]] == [round(w * 1000.0, 3) for w in widths]


def test_the_leaks_are_read_the_same_twice(leaky):
    _tris, mesh, plain, m = leaky
    again = ins.read_leaks(mesh, cells=CELLS, plain=plain)
    assert again is not None and again.close == m.close
    assert [(s.width, tuple(s.point), len(s.cubes)) for s in again.leaks] == \
        [(s.width, tuple(s.point), len(s.cubes)) for s in m.leaks]


def test_a_tube_with_no_leak_and_a_closed_solid_read_as_drawn():
    clean = surface(_tube())
    plain = ins.read_inside(clean, cells=CELLS)
    assert plain.has_inside() and plain.closed is None and not plain.leaks
    assert ins.read_leaks(clean, cells=CELLS, plain=plain) is None   # the map as drawn finds the way through
    # a closed surface in one piece keeps its inside and outside apart: it cannot leak, however
    # narrow its channels
    from tests.unit.cad.test_open_ends import _tube as thick_tube
    solid = surface(np.asarray(thick_tube(0.05, 0.04), dtype=float))
    assert not ins.surface_is_open(solid)
    assert ins.read_leaks(solid, cells=CELLS) is None


def test_a_holed_body_that_is_the_fluid_gets_its_inside_back():
    # the tube closed at both ends by flat discs: a body that is the fluid, its skin pinholed
    def disc(x, sign):
        th = np.linspace(0.0, 2.0 * math.pi, 64, endpoint=False)
        rim = np.array([[x, R * math.cos(t), R * math.sin(t)] for t in th])
        c = np.array([x, 0.0, 0.0])
        T = [[c, rim[(j + 1) % 64], rim[j]] if sign > 0 else [c, rim[j], rim[(j + 1) % 64]] for j in range(64)]
        return np.asarray(T)
    body = np.concatenate([_tube(holes=HOLES), disc(0.0, -1), disc(L, 1)])
    mesh = surface(body)
    plain = ins.read_inside(mesh, cells=CELLS)
    assert plain.enclosed_cells == 0                    # flooded through the pinholes
    m = ins.read_leaks(mesh, cells=CELLS, plain=plain)
    assert m is not None and m.enclosed_cells > 0 and not m.passages()
    assert m.enclosed_volume == pytest.approx(math.pi * R * R * L, rel=0.15)
    assert len(m.leaks) >= 2 and all(s.width < 2.0e-3 for s in m.leaks)


def test_the_covers_lie_on_the_wall_and_face_out_of_the_passage(leaky):
    tris, mesh, _plain, m = leaky
    T = ins.leak_covers(m, mesh.verts, mesh.faces)
    assert len(T)
    P = T.reshape(-1, 3)
    radial = np.linalg.norm(P[:, 1:], axis=1)
    assert np.all(np.abs(radial - R) <= 3.0 * m.h)                  # across the hole, at the wall
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    out = np.c_[np.zeros(len(T)), T.mean(axis=1)[:, 1:]]
    assert float(np.mean(np.einsum("ij,ij->i", n, out) > 0)) >= 0.5   # mostly out of the passage
    # the rim is laid on the surface: no gap is left between a cover and the wall
    from scipy.spatial import cKDTree

    from meshpipeline.cad.inside_space import _surface_distance
    W = mesh.verts[mesh.faces]
    key = np.round(P / (1e-3 * m.h)).astype(np.int64)
    _u, inv = np.unique(key, axis=0, return_inverse=True)
    tri = inv.reshape(-1, 3)
    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), axis=1)
    ue, cnt = np.unique(e, axis=0, return_counts=True)
    rim = np.unique(ue[cnt == 1].ravel())
    assert len(rim)
    first = np.zeros(len(_u), dtype=np.int64)
    first[inv] = np.arange(len(P))
    d = _surface_distance(P[first[rim]], W, cKDTree(W.mean(axis=1)))
    assert float(np.median(d)) <= 1e-6 * R


def test_the_staging_closes_the_leaks_up_to_the_size_and_records_it(leaky):
    tris, _mesh, _plain, m = leaky
    from meshpipeline.cad.internal_surface import stage_triangles
    patches = [{"name": "inlet", "type": "inlet", "near_mm": [0.0, 0.0, 0.0], "area_mm2": math.pi * 100.0},
               {"name": "outlet", "type": "outlet", "near_mm": [L * 1000.0, 0.0, 0.0], "area_mm2": math.pi * 100.0},
               {"name": "wall", "type": "wall"}]
    size = ins.proposed_leak_size(m)
    st = stage_triangles(tris, intake_patches=patches, close_leaks=size)
    done = st.facts["leaks_closed"]
    assert done["rule"] == "INS-8" and done["found"] >= 1
    assert done["sealed"] + done["covered"] == done["found"]
    assert st.facts["leak_test"].get("sealed") is True
    assert st.facts["volume_m3"] == pytest.approx(math.pi * R * R * L, rel=0.03)   # the tube's own fluid
    rows = [d for d in st.facts["decisions"] if d.get("rule") == "INS-8"]
    assert rows and "unchanged" in rows[0]["words"]
    # switched off (or never proposed): the staging is the one it always was
    plain = stage_triangles(tris, intake_patches=patches)
    assert "leaks_closed" not in plain.facts
    assert not [d for d in plain.facts["decisions"] if d.get("rule") == "INS-8"]


def test_the_closing_radius_follows_the_size_and_the_grid():
    assert ins.closing_radius(1.0e-3, 0.25e-3) == 2
    assert ins.closing_radius(1.1e-3, 0.25e-3) == 3
    assert ins.closing_radius(1.0e-4, 0.25e-3) == 1                 # never less than one cube
    assert ins.closing_radius(1.0, 0.25e-3) == ins.LEAK_MAX_RADIUS   # never past the cap


def test_the_stage_answer_takes_the_proposal_a_size_or_off():
    from meshpipeline.application.geometry_confirmation import leak_closing_answer, leak_closing_of
    leaks = {"rule": "INS-8", "found": 2, "close_up_to_mm": 0.9,
             "leaks": [{"width_mm": 0.71, "at_mm": [12, 0, 10]}, {"width_mm": 0.4, "at_mm": [21, 0, -10]}]}
    assert leak_closing_answer(None, None) == (None, "")
    assert leak_closing_answer({"found": 0}, 2.0) == (None, "")
    size, said = leak_closing_answer(leaks, None)
    assert size == 0.9 and "every leak up to 0.9 mm is closed for the mesh" in said and "never in the file" in said
    size, said = leak_closing_answer(leaks, 0)
    assert size == 0.0 and "left open" in said
    size, said = leak_closing_answer(leaks, 0.5)
    assert size == 0.5 and "0.71 mm, is wider and stays open" in said
    assert leak_closing_of({"flow": "internal", "close_leaks_mm": 0.9}) == {"close_up_to_m": 0.0009, "rule": "INS-8"}
    assert leak_closing_of({"flow": "internal", "close_leaks_mm": 0}) is None
    assert leak_closing_of({"flow": "external", "close_leaks_mm": 0.9}) is None
    assert leak_closing_of({"flow": "internal"}) is None


def test_the_size_travels_to_the_runs_workspace(tmp_path):
    from meshpipeline.agents.builder.workspace import write_leak_closing
    from meshpipeline.engines.workspace_facts import read_leak_closing
    assert read_leak_closing(tmp_path) == 0.0
    write_leak_closing(tmp_path, {"close_up_to_m": 0.0009, "rule": "INS-8"})
    assert read_leak_closing(tmp_path) == pytest.approx(0.0009)
    write_leak_closing(tmp_path, None)                  # switched off: a carried-forward one goes
    assert read_leak_closing(tmp_path) == 0.0


def test_the_scout_proposes_the_real_ends_and_lists_the_leaks(tmp_path, leaky):
    tris, _mesh, _plain, _m = leaky
    from meshpipeline.cad.scout_mesh import scout_mesh
    from meshpipeline.cad.stl_io import write_stl_binary
    p = tmp_path / "tube.stl"
    write_stl_binary(p, [tuple(t.tolist()) for t in tris])
    sc = scout_mesh(p, scale_to_m=1.0)
    lk = sc.extra["leaks"]
    assert lk["rule"] == "INS-8" and lk["found"] >= 1
    assert sc.extra["inside"]["has_inside"] and sc.extra["inside"]["leaks_closed"]
    ports = [o for o in sc.as_dict()["openings"] if o.get("role") in ("inlet", "outlet")]
    xs = sorted(round(float(o["centroid_mm"][0])) for o in ports)
    assert xs[0] == 0 and xs[-1] == 40                   # the tube's two ends
    assert all(float(o.get("diameter_mm") or 0) > 15.0 for o in ports)   # no pinhole proposed as a port
