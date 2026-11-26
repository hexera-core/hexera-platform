# Responsibility: Verify the FEA heatmap packers colour each drawn element by the product's own FEA figures - the tetrahedron behind a solid's surface triangle, a shell's own plate element - against the result's own limits and counts.
# Boundaries: small meshes built by hand (and one real gmsh box through the viewer hook); no worker, no browser.
from __future__ import annotations

import base64
import json

import numpy as np
import pytest

from meshpipeline.engines import fea_view_quality as FV
from meshpipeline.engines.fea_quality import element_measures
from meshpipeline.engines.shell_quality import quad_measures, tria_measures


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


# two tetrahedra on one face (0, 1, 2): a good one above it, a flat sliver below it
POINTS = np.array([[0, 0, 0], [1, 0, 0], [0.5, 0.87, 0], [0.5, 0.29, 0.82], [0.5, 0.29, -0.01]], dtype=float)
TETS = np.array([[0, 1, 2, 3], [0, 2, 1, 4]])
GOOD_SIDE = [[0, 1, 3], [1, 2, 3], [2, 0, 3]]
BAD_SIDE = [[0, 1, 4], [1, 2, 4], [2, 0, 4]]


def _solid(quality=None, shared=False):
    patches = [("Face_1", GOOD_SIDE), ("Face_2", BAD_SIDE)] + ([("Face_3", [[0, 1, 2]])] if shared else [])
    return FV.solid_fields(POINTS, TETS, patches, quality or {})


def test_each_surface_triangle_carries_the_figures_of_the_tetrahedron_behind_it():
    f = _solid()
    m = element_measures(POINTS[TETS])
    good, bad = f["patches"]["Face_1"], f["patches"]["Face_2"]
    assert good["count"] == 3 and bad["count"] == 3
    np.testing.assert_allclose(_f32(good["aspect_ratio_b64"]), m["aspect"][0], rtol=1e-6)
    np.testing.assert_allclose(_f32(bad["aspect_ratio_b64"]), m["aspect"][1], rtol=1e-6)
    np.testing.assert_allclose(_f32(bad["scaled_jacobian_b64"]), m["scaled_jacobian"][1], rtol=1e-5)
    np.testing.assert_allclose(_f32(bad["min_dihedral_b64"]), m["min_dihedral"][1], rtol=1e-5)
    # the sliver is the reddest: past every limit, and the good one inside them
    md = f["metrics"]
    assert _f32(bad["aspect_ratio_b64"]).min() > md["aspect_ratio"]["limit"] > _f32(good["aspect_ratio_b64"]).max()
    assert _f32(bad["scaled_jacobian_b64"]).max() < md["scaled_jacobian"]["limit"] < _f32(good["scaled_jacobian_b64"]).min()
    assert md["scaled_jacobian"]["low"] and md["scaled_jacobian"]["best"] == 1.0
    assert md["min_dihedral"]["low"] and md["min_dihedral"]["unit"] == "°"
    assert f["noun"] == "element"
    # the interior pass names the sliver as a hot spot of each measure it misses
    assert {h["metric"] for h in f["hotspots"]} == {"aspect_ratio", "scaled_jacobian", "min_dihedral"}


def test_a_face_both_tetrahedra_share_shows_the_worse_of_the_two():
    f = _solid(shared=True)
    m = element_measures(POINTS[TETS])
    cut = f["patches"]["Face_3"]
    assert _f32(cut["aspect_ratio_b64"])[0] == pytest.approx(max(m["aspect"]), rel=1e-6)
    assert _f32(cut["scaled_jacobian_b64"])[0] == pytest.approx(min(m["scaled_jacobian"]), rel=1e-5)


def test_the_counts_are_the_results_own_and_a_typed_limit_is_the_line():
    # the verdict's own count, and the user's aspect-ratio row (longest edge over shortest, their limit 1.2)
    q = {"fea": {"quality": {"over_limit": {"fea_max_aspect_ratio": 137, "fea_min_scaled_jacobian": 2,
                                             "fea_min_dihedral_deg": 1}}}}
    md = _solid(q)["metrics"]
    assert md["aspect_ratio"]["n_over"] == 137 and md["aspect_ratio"]["limit"] == 20.0
    assert md["aspect_ratio"]["from"] == FV.FROM_RESULT
    q["requirements"] = {"rows": [{"id": "r1", "field": "limit:aspect_ratio", "applied": True,
                                   "limit_target": 1.2, "outside": 1}]}
    f = _solid(q)
    a = f["metrics"]["aspect_ratio"]
    assert (a["limit"], a["n_over"], a["from"]) == (1.2, 1, FV.FROM_YOU)
    edge = element_measures(POINTS[TETS])["edge_ratio"]
    np.testing.assert_allclose(_f32(f["patches"]["Face_2"]["aspect_ratio_b64"]), edge[1], rtol=1e-6)


def test_quadratic_tetrahedra_read_their_curved_figures_and_the_curved_limit():
    mids = [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]
    pts = list(POINTS)
    t10 = []
    for t in TETS:
        row = list(t)
        for a, b in mids:
            pts.append((POINTS[t[a]] + POINTS[t[b]]) / 2)
            row.append(len(pts) - 1)
        t10.append(row)
    P, T = np.array(pts), np.array(t10)
    f = FV.solid_fields(P, T, [("Face_1", GOOD_SIDE), ("Face_2", BAD_SIDE)], {})
    assert f["metrics"]["scaled_jacobian"]["limit"] == 0.01
    np.testing.assert_allclose(_f32(f["patches"]["Face_1"]["scaled_jacobian_b64"]),
                               element_measures(P[T])["scaled_jacobian"][0], rtol=1e-5)


# a shell: a flat square quad, a warped quad beside it and one triangle
SHELL_P = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0], [2, 1, 0.6], [3, 0, 0]], dtype=float)
QUADS = [[0, 1, 2, 3], [1, 4, 5, 2]]
TRIA = [[4, 6, 5]]


def test_a_shell_element_carries_its_own_figures_and_the_warped_quad_is_the_reddest():
    sq = {"past_limits": {"warp_deg": 1, "aspect": 0}}
    f = FV.shell_fields(SHELL_P, [("Face_1", QUADS), ("Face_2", TRIA)], {"shell_quality": sq})
    qm = quad_measures(SHELL_P, np.array(QUADS))
    tm = tria_measures(SHELL_P, np.array(TRIA))
    warp = _f32(f["patches"]["Face_1"]["warp_b64"])
    np.testing.assert_allclose(warp, qm["warp_deg"], rtol=1e-5, atol=1e-6)
    assert warp[1] == warp.max() and warp[1] > f["metrics"]["warp"]["limit"] > warp[0]
    # a triangle has no warp: no figure, left calm
    assert np.isnan(_f32(f["patches"]["Face_2"]["warp_b64"])).all()
    np.testing.assert_allclose(_f32(f["patches"]["Face_2"]["aspect_ratio_b64"]), tm["aspect"], rtol=1e-5)
    np.testing.assert_allclose(_f32(f["patches"]["Face_1"]["jacobian_b64"]), qm["jacobian_ratio"], rtol=1e-5)
    md = f["metrics"]
    assert set(md) == {"aspect_ratio", "warp", "skew", "min_angle", "max_angle", "jacobian", "taper"}
    assert md["warp"]["n_over"] == 1 and md["aspect_ratio"]["n_over"] == 0
    # the corner angles read the quads' limits; a triangle's corner angles are its own rows
    assert md["min_angle"]["limit"] == 45.0 and md["max_angle"]["limit"] == 135.0
    assert np.isnan(_f32(f["patches"]["Face_2"]["min_angle_b64"])).all()
    assert md["skew"]["low"] and md["skew"]["best"] == 90.0 and md["jacobian"]["low"]


def test_a_shell_limit_the_user_typed_is_the_line_for_both_shapes():
    q = {"requirements": {"rows": [{"id": "r1", "field": "limit:min_angle", "op": ">=", "applied": True,
                                    "limit_target": 50.0, "outside": 2}]}}
    f = FV.shell_fields(SHELL_P, [("Face_1", QUADS), ("Face_2", TRIA)], q)
    a = f["metrics"]["min_angle"]
    assert (a["limit"], a["n_over"], a["from"]) == (50.0, 2, FV.FROM_YOU)
    np.testing.assert_allclose(_f32(f["patches"]["Face_2"]["min_angle_b64"]),
                               tria_measures(SHELL_P, np.array(TRIA))["min_angle_tria"], rtol=1e-5)


def test_nothing_that_goes_wrong_costs_the_view(tmp_path):
    resp = {"kind": "stl", "patches": [{"name": "Face_1"}]}
    FV.attach(resp, {"points": None, "patches": [("Face_1", [[0, 1, 2]])], "tets": [np.zeros((1, 3))]},
              tmp_path, shell=False)
    assert "quality_fields" not in resp


gmsh = pytest.importorskip("gmsh")


def test_the_fea_viewer_hook_serves_the_heatmap_aligned_with_the_drawn_triangles(tmp_path):
    from meshpipeline.engines.gmsh.spec import _viewer_surface
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("box")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        for _d, tag in gmsh.model.getEntities(2):
            gmsh.model.addPhysicalGroup(2, [tag])
        gmsh.model.addPhysicalGroup(3, [1])
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.4)
        gmsh.option.setNumber("Mesh.ElementOrder", 2)
        gmsh.model.mesh.generate(3)
        gmsh.write(str(tmp_path / "mesh.msh"))
    finally:
        gmsh.finalize()
    (tmp_path / "fea_plan.json").write_text("{}")
    (tmp_path / "quality.json").write_text(json.dumps({}))
    resp = _viewer_surface()(tmp_path, roles={}, units="m")
    qf = resp["quality_fields"]
    assert set(qf["metrics"]) == {"aspect_ratio", "scaled_jacobian", "min_dihedral"}
    for p in resp["patches"]:
        got = qf["patches"][p["name"]]
        assert got["count"] == p["tri_count"]
        sj = _f32(got["scaled_jacobian_b64"])
        assert len(sj) == p["tri_count"] and np.isfinite(sj).all() and (sj > 0).all()


def test_a_mesh_kept_in_the_bucket_keeps_its_heatmap_on_the_thinned_view(tmp_path, monkeypatch):
    # the stand-in is written while the volume is here; thinned, every drawn triangle takes the worst near it
    from meshpipeline.engines.gmsh import viewer_surface as VS
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber('General.Terminal', 0)
        gmsh.model.add('box')
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.model.addPhysicalGroup(2, [t for _d, t in gmsh.model.getEntities(2)])
        gmsh.model.addPhysicalGroup(3, [1])
        gmsh.option.setNumber('Mesh.MeshSizeMax', 0.15)
        gmsh.model.mesh.generate(3)
        gmsh.write(str(tmp_path / 'mesh.msh'))
    finally:
        gmsh.finalize()
    mesh = VS.read_msh(tmp_path, by_face=True)
    fields = FV.fields_for(mesh, tmp_path, shell=False)
    monkeypatch.setattr(VS, 'VIEW_MAX_TRIANGLES', 200)
    VS.save_viewer_mesh(tmp_path / 'stand_in.npz', mesh, fields)
    monkeypatch.setattr('meshpipeline.engines.bucket_files.VIEWER_MESH_FILE', 'stand_in.npz')
    shown = VS.load_viewer_mesh(tmp_path)
    got = shown['quality_fields']
    assert got['thinned'] and got['metrics'] == fields['metrics']
    for name, polys in shown['patches']:
        full = _f32(fields['patches'][name]['aspect_ratio_b64'])
        thin = _f32(got['patches'][name]['aspect_ratio_b64'])
        assert len(thin) == len(polys) < len(full)
        assert np.nanmax(thin) == pytest.approx(full.max())


def test_a_flat_tetrahedron_ships_as_the_worst_there_is_never_as_an_infinity_json_refuses():
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], dtype=float)      # all four in one plane
    f = FV.solid_fields(pts, np.array([[0, 1, 2, 3]]), [('Face_1', [[0, 1, 2], [0, 1, 3]])], {})
    json.dumps(f, allow_nan=False)
    assert max(h['value'] for h in f['hotspots'] if h['metric'] == 'aspect_ratio') == FV.ENDLESS
