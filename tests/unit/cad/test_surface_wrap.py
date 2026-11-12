"""The CFD wrap (cad/surface_wrap, LAD-cfd-6/7): a dirty surface becomes a closed, manifold skin that
follows the original, names every face after it, keeps every opening of a passage, and says how far
it lies from the original and where it bridged."""
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.cad import surface_wrap as sw

CUBES = 400_000          # small grids: the rules are the same at any size


def _box(lo, hi):
    from meshpipeline.cad.stl_io import _box_triangles
    return np.asarray(_box_triangles(lo, hi), dtype=np.float64)


def _subdivide(T, n=3):
    for _ in range(n):
        a, b, c = T[:, 0], T[:, 1], T[:, 2]
        ab, bc, ca = (a + b) / 2, (b + c) / 2, (c + a) / 2
        T = np.concatenate([np.stack(x, axis=1) for x in
                            ((a, ab, ca), (ab, b, bc), (ca, bc, c), (ab, bc, ca))])
    return T


def _pipe(r=0.02, length=0.3, n=48):
    """A closed cylinder along z: wall, inlet lid at z=-L/2, outlet lid at z=+L/2."""
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    ring = np.stack([r * np.cos(th), r * np.sin(th)], axis=1)
    z0, z1 = -length / 2, length / 2
    T, L = [], []
    for i in range(n):
        p, q = ring[i], ring[(i + 1) % n]
        a, b = np.r_[p, z0], np.r_[q, z0]
        c, d = np.r_[q, z1], np.r_[p, z1]
        T += [(a, b, c), (a, c, d)]
        L += ["wall", "wall"]
        T.append((np.r_[0, 0, z0], b, a))
        L.append("inlet")
        T.append((np.r_[0, 0, z1], d, c))
        L.append("outlet")
    return np.asarray(T, dtype=np.float64), np.asarray(L, dtype=object)


def test_a_dirty_body_wraps_into_a_closed_named_skin():
    # two halves with a 2 mm crack between them (a seam left open), a box that crosses one, and a
    # doubled stretch of skin
    left = _subdivide(_box((0, 0, 0), (0.499, 0.4, 0.3)))
    right = _subdivide(_box((0.501, 0, 0), (1.0, 0.4, 0.3)))
    other = _subdivide(_box((0.8, 0.1, 0.1), (1.2, 0.3, 0.4)))
    T = np.concatenate([left, right, other, left[:40] + 1e-6])
    labels = np.full(len(T), "body", dtype=object)
    res = sw.wrap_surface(T, labels, flow="external", cubes=CUBES)
    one, _two, many = sw.edge_counts(res.faces)
    assert one == 0 and many == 0
    assert set(res.labels.tolist()) == {"body"}
    rep = res.report
    assert rep["closed"] and rep["rule"] == "LAD-cfd-7" and rep["distance_m"] > 0
    # it lies on the original wherever the original was sound, to under 1.5 cubes
    assert rep["max_on_cad_m"] <= 1.5 * rep["cube_m"] + 1e-12
    # the body's own volume, out of the body (an STL body's convention)
    V, F = res.verts, res.faces
    vol = np.einsum("ij,ij->i", V[F[:, 0]], np.cross(V[F[:, 1]], V[F[:, 2]])).sum() / 6.0
    union = 1.0 * 0.4 * 0.3 + 0.4 * 0.2 * 0.3 - 0.2 * 0.2 * 0.2
    assert vol > 0 and abs(vol - union) / union < 0.08
    # the crack is bridged, and said
    assert rep["bridged_spots"] >= 1 or rep["max_m"] <= 1.5 * rep["cube_m"]
    assert "meshed on a wrapped surface: within" in sw.summary_words(rep)


def test_a_pipe_keeps_its_openings_and_the_names_follow_the_original():
    T, L = _pipe()
    res = sw.wrap_surface(T, L, flow="internal", seed=(0.0, 0.0, 0.0), ports=("inlet", "outlet"),
                          cubes=CUBES)
    assert set(res.labels.tolist()) == {"wall", "inlet", "outlet"}
    assert res.seed_in_fluid
    one, _two, many = sw.edge_counts(res.faces)
    assert one == 0 and many == 0
    # out of the fluid: the enclosed volume is positive and about the pipe's
    V, F = res.verts, res.faces
    vol = np.einsum("ij,ij->i", V[F[:, 0]], np.cross(V[F[:, 1]], V[F[:, 2]])).sum() / 6.0
    assert 0.9 < vol / (np.pi * 0.02 ** 2 * 0.3 * (np.sin(2 * np.pi / 48) * 48 / (2 * np.pi))) < 1.05
    # the narrowest way is the pipe's own half-width: the wrap stays well inside it (LAD-cfd-6)
    assert res.report["distance_m"] <= sw.WRAP_PASSAGE_SHARE * 0.02 * 1.01 + 2.5 * res.report["cube_m"]


def test_a_passage_narrower_than_the_wrap_stops_the_rung():
    T, L = _pipe(r=0.002, length=0.3)
    with pytest.raises(sw.WrapError):
        sw.wrap_surface(T, L, flow="internal", seed=(0.0, 0.0, 0.0), ports=("inlet", "outlet"),
                        distance=0.004, cubes=CUBES)


def test_the_distance_comes_from_the_geometry():
    assert sw.wrap_distance(5.0) == pytest.approx(5.0 / sw.WRAP_DIAG_SHARE)
    assert sw.wrap_distance(5.0, bottleneck=0.01) == pytest.approx(sw.WRAP_PASSAGE_SHARE * 0.01)
    grid, r = sw.grid_for((0, 0, 0), (10, 10, 10), 0.001, cubes=100_000)
    assert grid.n <= 100_000 * 1.05 and r >= sw.WRAP_CUBES_PER_R * grid.h * 0.999


def test_nothing_is_wrapped_without_the_runs_request(tmp_path):
    from meshpipeline.cad.stl_io import write_stl_binary
    write_stl_binary(tmp_path / "input.stl", _box((0, 0, 0), (1, 1, 1)))
    before = (tmp_path / "input.stl").read_bytes()
    assert sw.wrap_body_if_asked(tmp_path) is None
    rec = {"stls": {"wall": str(tmp_path / "input.stl")}}
    out = tmp_path / "_internal_stls"
    out.mkdir()
    assert sw.wrap_staged_if_asked(rec, out) is rec
    assert (tmp_path / "input.stl").read_bytes() == before


def test_the_body_is_wrapped_in_place_on_the_rung(tmp_path):
    from meshpipeline.cad.stl_io import read_stl_triangles, write_stl_binary
    body = _subdivide(_box((0, 0, 0), (1.0, 0.5, 0.5)), 2)
    write_stl_binary(tmp_path / "input.stl", np.concatenate([body, body[:10]]))
    (tmp_path / sw.WRAP_REQUEST_FILE).write_text(json.dumps({"flow": "external"}))
    orig = sw.WRAP_GRID_CUBES
    try:
        sw.WRAP_GRID_CUBES = CUBES
        rep = sw.wrap_body_if_asked(tmp_path)
    finally:
        sw.WRAP_GRID_CUBES = orig
    assert rep is not None
    assert (tmp_path / ("input" + sw.UNWRAPPED_SUFFIX)).exists()
    saved = json.loads((tmp_path / sw.WRAP_REPORT_FILE).read_text())
    assert saved["words"].startswith("meshed on a wrapped surface")
    assert len(read_stl_triangles(tmp_path / "input.stl")) == rep["skin_triangles"]



def _tube(axis, length=0.3, r=0.02, n=40, port_lo="", port_hi=""):
    """A closed cylinder along z (axis 2) or x (axis 0), centred at the origin, its lids named."""
    T, L = _pipe(r=r, length=length, n=n)
    if axis == 0:
        T = T[:, :, [2, 1, 0]]
        T = T[:, ::-1]                          # the swap mirrors: keep every face turned out
    L = np.where(L == "inlet", port_lo or "wall", np.where(L == "outlet", port_hi or "wall", L))
    return T, np.asarray(L, dtype=object)


def test_overlapping_shells_merge_into_one_fluid():
    # two vessels drawn THROUGH each other (a cross): from the point inside the first, its own
    # walls hide the second vessel's openings - the pockets the boundary encloses are one fluid
    Ta, La = _tube(2, port_lo="inlet", port_hi="outlet_1")
    Tb, Lb = _tube(0, port_lo="outlet_2", port_hi="outlet_3")
    T, L = np.concatenate([Ta, Tb]), np.concatenate([La, Lb])
    res = sw.wrap_surface(T, L, flow="internal", seed=(0.0, 0.0, 0.1),
                          ports=("inlet", "outlet_1", "outlet_2", "outlet_3"), cubes=CUBES)
    assert {"inlet", "outlet_1", "outlet_2", "outlet_3"} <= set(res.labels.tolist())
    assert any(t.get("fluid") == "enclosed pockets" and t["reached"] for t in res.report["tries"])
    one, _two, many = sw.edge_counts(res.faces)
    assert one == 0 and many == 0


def test_an_opening_the_wrap_cannot_keep_is_placed_not_refused():
    # a hair-thin vessel (0.4 mm across, far below any grid the wrap can draw) beside the main pipe:
    # no wrap distance keeps it, so the wrap delivers the pipe with that opening placed on its wall
    # nearest where it is drawn, and says so - the run is never ended on it
    T, L = _pipe()
    Tb, Lb = _tube(2, length=0.1, r=0.0002, n=12, port_lo="wall", port_hi="outlet_2")
    Tb = Tb + np.array([0.03, 0.0, 0.0])
    T, L = np.concatenate([T, Tb]), np.concatenate([L, Lb])
    res = sw.wrap_surface(T, L, flow="internal", seed=(0.0, 0.0, 0.0),
                          ports=("inlet", "outlet", "outlet_2"), cubes=CUBES)
    assert {"inlet", "outlet", "outlet_2"} <= set(res.labels.tolist())
    moved = res.report.get("openings_moved") or []
    assert [m["opening"] for m in moved] == ["outlet_2"]
    assert moved[0]["moved_m"] < 0.03
    one, _two, many = sw.edge_counts(res.faces)
    assert one == 0 and many == 0
