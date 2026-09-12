# Responsibility: Verify cfMesh's passage sizing caps, the passage measure, and the resolution floor, judging no real mesh.
# The HEX-6 pilot delivered a 173 mm reducer at 8.9 cells across (4.6 at the narrowest wall)
# because the wall band defaulted to L/20 of a 1.26 m part. Sizes now come from the local radius.
from __future__ import annotations

import numpy as np

import meshpipeline.engines.cfmesh.cfmesh_runner as R
import meshpipeline.engines.passage as P
from meshpipeline.engines.cfmesh import flow_gates as G
from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.snappy import flow_gates as SG


def _cylinder(radius=0.05, length=1.0, n_around=24, n_along=40):
    th = np.linspace(0, 2 * np.pi, n_around, endpoint=False)
    xs = np.linspace(0, length, n_along)
    ring = np.stack([np.zeros_like(th), radius * np.cos(th), radius * np.sin(th)], axis=1)
    pts = np.concatenate([ring + [x, 0, 0] for x in xs])
    faces = []
    for i in range(n_along - 1):
        for j in range(n_around):
            a, b = i * n_around + j, i * n_around + (j + 1) % n_around
            c, d = a + n_around, b + n_around
            faces += [[a, b, d], [a, d, c]]
    c0, c1 = len(pts), len(pts) + 1
    pts = np.concatenate([pts, [[0, 0, 0], [length, 0, 0]]])
    for j in range(n_around):
        faces.append([c0, (j + 1) % n_around, j])
        base = (n_along - 1) * n_around
        faces.append([c1, base + j, base + (j + 1) % n_around])
    return pts, np.asarray(faces, dtype=np.int64)


def test_the_measure_counts_cells_across_a_tube():
    pts, faces = _cylinder()
    m = P.measure_passage(pts, faces, np.full(len(pts), 0.05))
    assert 2.0 < m["p05"] <= m["median"] < 9.0 and m["points"] == len(pts)
    assert P.measure_passage(pts, faces, np.zeros(len(pts))) == {}


def test_the_radius_is_read_from_a_closed_tube_with_a_point_found_inside():
    pts, faces = _cylinder(radius=0.05, n_around=48, n_along=80)
    out = P.passage_of_surface(pts, faces)
    r = out["passage_radius"]
    assert 0.04 < r["median"] < 0.06, r          # half the 100 mm bore
    assert out["passage_cells_across_local"]["points"] == len(pts)


def test_the_staged_wall_is_read_open_from_a_point_found_via_the_ports(tmp_path):
    import pyvista as pv
    pts, faces = _cylinder(radius=0.05, n_around=48, n_along=80)
    wall_faces = faces[: -(2 * 48)]                     # drop the two cap fans: an open tube
    tube = pv.PolyData(pts, np.hstack([np.full((len(wall_faces), 1), 3), wall_faces]).ravel())
    tube.save(str(tmp_path / "wall.stl"))
    cap = pv.PolyData(pts, np.hstack([np.full((2 * 48, 1), 3), faces[-(2 * 48):]]).ravel())
    cap.save(str(tmp_path / "caps.stl"))
    # the port centroids sit on the axis at the ends; the deep point lands on the axis
    deep = P.interior_from_ports(np.asarray(tube.points), np.asarray(cap.points),
                                 [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    assert np.linalg.norm(deep[1:]) < 0.005 and 0.05 < deep[0] < 0.95, deep
    r = P.passage_of_stls([tmp_path / "wall.stl"], cap_paths=[tmp_path / "caps.stl"],
                          port_centroids=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    assert 0.04 < r["p05"] <= r["median"] < 0.06, r  # half the 100 mm bore, end to end
    # a point inside the WALL MATERIAL (what the tessellation hands a hollow solid) must not
    # be what orients the chords: the ports win over it
    r2 = P.passage_of_stls([tmp_path / "wall.stl"], interior_point=[0.5, 0.0501, 0.0],
                           cap_paths=[tmp_path / "caps.stl"],
                           port_centroids=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    assert r2 == r
    assert P.passage_of_stls([tmp_path / "missing.stl"], interior_point=[0, 0, 0]) == {}


def test_a_wall_of_separately_wound_pieces_is_oriented_before_the_chords_are_cast(tmp_path):
    import pyvista as pv
    # three axial tubes with their own points (CAD faces do not share vertices), the middle
    # one wound inward
    pieces, all_pts, offset = [], [], 0
    for k in range(3):
        pts, faces = _cylinder(radius=0.05, length=1.0 / 3.0, n_around=48, n_along=27)
        wall = faces[: -(2 * 48)].copy()
        if k == 1:
            wall[:, [1, 2]] = wall[:, [2, 1]]
        pieces.append(wall + offset)
        all_pts.append(pts + [k / 3.0, 0.0, 0.0])
        offset += len(pts)
    P2 = np.concatenate(all_pts)
    F2 = np.concatenate(pieces)
    ports = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    P3, oriented = P.orient_wall_faces(P2, F2, ports)
    poly = pv.PolyData(P3, np.hstack([np.full((len(oriented), 1), 3), oriented]).ravel())
    cn = np.asarray(poly.compute_normals(cell_normals=True, point_normals=False,
                                         consistent_normals=False, auto_orient_normals=False)["Normals"])
    centres = P3[oriented].mean(axis=1)
    radial = centres[:, 1:] / np.linalg.norm(centres[:, 1:], axis=1, keepdims=True)
    assert (np.einsum("ij,ij->i", cn[:, 1:], radial) > 0.9).all(), "every piece must point out"
    pv.PolyData(P2, np.hstack([np.full((len(F2), 1), 3), F2]).ravel()).save(str(tmp_path / "wall.stl"))
    r = P.passage_of_stls([tmp_path / "wall.stl"], port_centroids=ports)
    assert 0.04 < r["p05"] <= r["median"] < 0.06, r


def test_a_flanged_wall_is_read_on_its_bore_skin_only(tmp_path):
    import pyvista as pv
    # bore skin (r 50 mm) plus an outer skin 9 mm away, both open tubes, plus the bore caps
    pts_in, faces_in = _cylinder(radius=0.05, n_around=48, n_along=80)
    pts_out, faces_out = _cylinder(radius=0.059, n_around=48, n_along=80)
    wall_in, wall_out = faces_in[: -(2 * 48)], faces_out[: -(2 * 48)]
    wall_pts = np.concatenate([pts_in, pts_out])
    wall_faces = np.concatenate([wall_in, wall_out + len(pts_in)])
    pv.PolyData(wall_pts, np.hstack([np.full((len(wall_faces), 1), 3), wall_faces]).ravel()).save(str(tmp_path / "wall.stl"))
    caps = pv.PolyData(pts_in, np.hstack([np.full((2 * 48, 1), 3), faces_in[-(2 * 48):]]).ravel())
    caps.save(str(tmp_path / "caps.stl"))
    ports = [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    r = P.passage_of_stls([tmp_path / "wall.stl"], cap_paths=[tmp_path / "caps.stl"], port_centroids=ports)
    assert 0.04 < r["p05"] <= r["median"] < 0.06, r    # half the bore, not the 9 mm skin gap
    skin_pts, skin_faces = P.cavity_skin(wall_pts, wall_faces, [caps])
    radii = np.linalg.norm(skin_pts[np.unique(skin_faces)][:, 1:], axis=1)
    assert radii.max() < 0.055, "the outer skin must not be part of the cavity skin"


def test_size_caps_put_thirteen_across_the_narrowest_and_typical_passage():
    caps = P.size_caps({"p05": 0.05, "median": 0.10})
    assert np.isclose(caps["wall_cell"], 2 * 0.05 / 13)
    assert np.isclose(caps["max_cell"], 2 * 0.10 / 13)
    assert np.isclose(caps["refinement_thickness"], 0.055)


def _render(tmp_path, **kw):
    ws = tmp_path
    return R.render_cfmesh_case(
        ws, surface_file="geom.fms", wall_patch="wall",
        patches=[{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                 {"name": "outlet", "type": "outlet"}],
        body_bbox=([0, 0, 0], [1.26, 0.2, 0.2]), L=1.26, domain_min=[0, 0, 0],
        domain_max=[1.26, 0.2, 0.2], strategy={}, cell_budget=8_000_000, **kw)


def test_the_passage_radius_caps_the_wall_band_and_the_background(tmp_path):
    base = _render(tmp_path)
    assert base["passage_caps"] is None
    capped = _render(tmp_path, passage_radius={"p05": 0.05, "median": 0.087})
    assert capped["wall_cell_size"] < base["wall_cell_size"]
    assert np.isclose(capped["wall_cell_size"], round(2 * 0.05 / 13, 6))
    assert capped["max_cell_size"] <= base["max_cell_size"]
    dic = (tmp_path / "system" / "meshDict").read_text()
    assert "refinementThickness 0.055" in dic and f"cellSize {2 * 0.05 / 13:.6g}" in dic


def _ctx(quality):
    c = GateCtx(workspace=None, engine="cfmesh", domain="", intake_patches=[], engine_params={})
    c.manifest_or_load = lambda: {"quality": quality}  # type: ignore[method-assign]
    return c


def test_the_narrowest_wall_gates_the_fill():
    ok, fb = G._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 8.9, "p05": 4.6}}))
    assert not ok and "narrowest wall" in fb and "4.6" in fb
    assert G._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 18.7, "p05": 17.5}}))[0]
    assert G._gate_resolution_floor(_ctx({}))[0], "an older mesh without the measure is not judged"
    # snappy holds the same bar with the same measure
    assert not SG._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 9, "p05": 5}}))[0]
    assert SG._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 20, "p05": 13}}))[0]
