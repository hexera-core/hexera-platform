# Responsibility: Verify the engine-owned lumen staging - declared ports bound to measured openings,
#                 one-source seeding, sizing, the staged pype, and the builder-facing merge.
from __future__ import annotations

import json

import numpy as np
import pytest
import pyvista as pv
import vtk

from meshpipeline.engines.vmtk import lumen_staging as LS
from meshpipeline.engines.vmtk import vmtk_runner

# ports as stage_lumen records them (metres)
_PORTS = [
    {"name": "inlet", "role": "inlet", "engine_key": "inlet", "centroid": [0.0, 0.0, 0.0],
     "size_m": 0.30, "area_m2": 0.0707},
    {"name": "outlet_1", "role": "outlet", "engine_key": "outlet_2", "centroid": [1.0, 0.0, 0.3],
     "size_m": 0.10, "area_m2": 0.00785},
    {"name": "outlet_2", "role": "outlet", "engine_key": "outlet_1", "centroid": [1.5, 0.0, 0.4],
     "size_m": 0.15, "area_m2": 0.01767},
]


def test_bind_ports_uses_the_declared_location_and_falls_back_to_area():
    measured = [
        {"key": "inlet", "centroid": [0.0, 0.0, 0.0], "area_m2": 0.0707, "size_m": 0.30},
        {"key": "outlet_1", "centroid": [1.5, 0.0, 0.4], "area_m2": 0.01767, "size_m": 0.15},
        {"key": "outlet_2", "centroid": [1.0, 0.0, 0.3], "area_m2": 0.00785, "size_m": 0.10},
    ]
    targets = [
        {"name": "inlet", "near_m": (0.0, 0.0, 0.0), "area_m2": 0.0707},
        {"name": "small", "near_m": None, "area_m2": 0.0078},        # by area only
        {"name": "side", "near_m": (1.49, 0.0, 0.41), "area_m2": 0.0177},
    ]
    roles = {"inlet": "inlet", "small": "outlet", "side": "outlet"}
    bound = LS.bind_ports(measured, targets, roles)
    assert [p["name"] for p in bound] == ["inlet", "side", "small"]
    by_name = {p["name"]: p for p in bound}
    assert by_name["side"]["engine_key"] == "outlet_1"       # the engine's size-ordered key
    assert by_name["small"]["engine_key"] == "outlet_2"
    assert by_name["small"]["role"] == "outlet" and by_name["inlet"]["role"] == "inlet"


def test_bind_ports_never_uses_a_declared_port_twice():
    measured = [{"key": "a", "centroid": [0, 0, 0], "area_m2": 1.0, "size_m": 1.0},
                {"key": "b", "centroid": [0.1, 0, 0], "area_m2": 1.0, "size_m": 1.0}]
    targets = [{"name": "only", "near_m": (0.0, 0.0, 0.0), "area_m2": 1.0}]
    bound = LS.bind_ports(measured, targets, {"only": "inlet"})
    assert [p["name"] for p in bound] == ["only"]


def test_seed_points_are_one_source_the_largest_port_and_every_other_port_a_target():
    src, tgt = LS.seed_points(_PORTS)
    assert src == [0.0, 0.0, 0.0]                        # the 0.30 m inlet
    assert tgt == [1.5, 0.0, 0.4, 1.0, 0.0, 0.3]        # the rest, largest first
    assert LS.seed_points(_PORTS[:1]) == ([], [])       # one opening seeds nothing


def test_sizing_is_tied_to_the_smallest_and_largest_port():
    s = LS.sizing(_PORTS)
    assert s["edge_bound"] == pytest.approx(0.10 / LS.RIM_DIVISIONS)
    assert s["min_edge_length"] == pytest.approx(0.10 * LS.MIN_EDGE_FRACTION)
    assert s["max_edge_length"] == pytest.approx(0.30 * LS.MAX_EDGE_FRACTION)
    assert s["min_edge_length"] < s["edge_bound"] < s["max_edge_length"]
    assert s["radius_lo"] == pytest.approx(0.10 * LS.RADIUS_LO_FRACTION)
    assert s["radius_hi"] == pytest.approx(0.30 * LS.RADIUS_HI_FRACTION)


def _tube(radius=0.05, length=0.4, n_around=24, n_along=8):
    # an open cylinder (no caps): points on rings, quads split into triangles
    th = np.linspace(0, 2 * np.pi, n_around, endpoint=False)
    xs = np.linspace(0, length, n_along + 1)
    pts = np.array([[x, radius * np.cos(t), radius * np.sin(t)] for x in xs for t in th])
    tri = []
    for i in range(n_along):
        for j in range(n_around):
            a = i * n_around + j
            b = i * n_around + (j + 1) % n_around
            c = a + n_around
            d = b + n_around
            tri += [[a, b, d], [a, d, c]]
    return pts, np.asarray(tri, dtype=np.int64)


def test_local_radius_reads_the_tube_radius_from_the_inward_chord():
    pts, tri = _tube(radius=0.05)
    r = LS.local_radius(pts, tri, interior_point=(0.2, 0.0, 0.0), r_lo=0.01, r_hi=0.2)
    assert r.shape == (len(pts),)
    assert np.allclose(r, 0.05, atol=0.002)     # the chord across a 24-gon is 2R within 1%


def test_local_radius_is_clipped_and_orientation_agnostic():
    pts, tri = _tube(radius=0.05)
    flipped = tri[:, [0, 2, 1]]                   # the same tube wound the other way
    r = LS.local_radius(pts, flipped, interior_point=(0.2, 0.0, 0.0), r_lo=0.01, r_hi=0.2)
    assert np.allclose(r, 0.05, atol=0.002)
    hi = LS.local_radius(pts, tri, interior_point=(0.2, 0.0, 0.0), r_lo=0.01, r_hi=0.03)
    assert np.allclose(hi, 0.03)
    lo = LS.local_radius(pts, tri, interior_point=(0.2, 0.0, 0.0), r_lo=0.08, r_hi=0.2)
    assert np.allclose(lo, 0.08)


def test_the_tessellation_angle_puts_one_remesh_edge_per_rim_chord():
    # chord on the smallest port = D * theta / 2 = D / RIM_DIVISIONS = the remesh edge length
    assert LS.ANGULAR_DEFLECTION == pytest.approx(2.0 / LS.RIM_DIVISIONS)


def test_merge_staged_fills_only_what_the_builder_left_out():
    staged = {"source_points": [0, 0, 0], "target_points": [1, 0, 0], "sizing_array": "LocalRadius",
              "min_edge_length": 0.005, "max_edge_length": 0.05}
    # nothing given -> everything staged
    m = LS.merge_staged({"edge_length_factor": 0.3}, staged)
    assert m["source_points"] == [0, 0, 0] and m["target_points"] == [1, 0, 0]
    assert m["sizing_array"] == "LocalRadius" and m["max_edge_length"] == 0.05
    # the builder's own seeds (either form) win, ids are not mixed with staged points
    m = LS.merge_staged({"source_ids": [0], "target_ids": [1]}, staged)
    assert "source_points" not in m and m["source_ids"] == [0]
    m = LS.merge_staged({"source_points": [9, 9, 9], "target_points": [8, 8, 8]}, staged)
    assert m["source_points"] == [9, 9, 9]
    # an explicit length is kept
    m = LS.merge_staged({"max_edge_length": 0.02}, staged)
    assert m["max_edge_length"] == 0.02 and m["min_edge_length"] == 0.005
    assert LS.merge_staged({"a": 1}, None) == {"a": 1}


def test_pype_with_a_staged_lumen_remeshes_projects_and_generates_from_the_radius_field():
    argv = vmtk_runner.build_pype({"sizing_array": "LocalRadius", "min_edge_length": 0.005,
                                   "max_edge_length": 0.05, "edge_length_factor": 0.25,
                                   "boundary_layers": 3, "cap_openings": False})
    joined = " ".join(argv)
    assert ("vmtksurfaceremeshing -ifile lumen_open.vtp -elementsizemode edgelengtharray "
            "-edgelengtharray LocalRadius -edgelengthfactor 0.25 -preserveboundary 0 "
            "-iterations 10") in joined
    assert "--pipe vmtksurfaceconnectivity -method largest" in joined
    assert "--pipe vmtksurfaceprojection -rfile lumen_open.vtp -ofile lumen.vtp" in joined
    assert ("--pipe vmtkmeshgenerator -ifile lumen.vtp -elementsizemode edgelengtharray "
            "-edgelengtharray LocalRadius -edgelengthfactor 0.25 -minedgelength 0.005 "
            "-maxedgelength 0.05 -skipcapping 0 -skipremeshing 0") in joined
    assert "-boundarylayer 1 -sublayers 3" in joined and "-boundarylayeroncaps 0" in joined
    assert argv[-2:] == ["-ofile", "mesh.vtu"]
    assert "vmtkcenterlines" not in joined and "-seedselector" not in joined
    stages = [joined.index(x) for x in ("vmtksurfaceremeshing", "vmtksurfaceconnectivity",
                                        "vmtksurfaceprojection", "vmtkmeshgenerator")]
    assert stages == sorted(stages)


def test_pype_without_staging_is_unchanged():
    joined = " ".join(vmtk_runner.build_pype({"source_ids": [0], "target_ids": [1]}))
    assert "vmtksurfaceremeshing" not in joined and "lumen_open" not in joined
    assert "vmtkcenterlines -ifile lumen.vtp" in joined
    assert "-minedgelength" not in joined and "-edgelengtharray DistanceToCenterlines" in joined


def _open_lumen(ws):
    # a single triangle: an open surface with one boundary loop
    tri = pv.PolyData(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float), np.array([3, 0, 1, 2]))
    tri.save(str(ws / "lumen.vtp"))


def _staging(ws):
    rec = {"ports": _PORTS, "source_points": [0.0, 0.0, 0.0],
           "target_points": [1.5, 0.0, 0.4, 1.0, 0.0, 0.3], **LS.sizing(_PORTS),
           "sizing_array": LS.SIZING_ARRAY, "radius_m": {"min": 0.05, "median": 0.1, "max": 0.15},
           "n_open_loops": 3}
    (ws / LS.STAGING_FACT).write_text(json.dumps(rec))
    return rec


def test_configure_mesh_takes_the_staged_seeds_and_sizing(tmp_path):
    _open_lumen(tmp_path)
    rec = _staging(tmp_path)
    out = vmtk_runner.configure_mesh(tmp_path, strategy={"edge_length_factor": 0.3})
    assert "error" not in out
    spec = out["spec"]
    assert spec["sizing_array"] == LS.SIZING_ARRAY
    assert spec["max_edge_length"] == pytest.approx(rec["max_edge_length"])
    assert "vmtksurfaceremeshing -ifile lumen_open.vtp" in out["pype"]
    assert "vmtkmeshgenerator -ifile lumen.vtp" in out["pype"]
    assert json.loads((tmp_path / "vmtk_spec.json").read_text())["min_edge_length"] > 0


def test_configure_mesh_refuses_when_nothing_seeds_the_centerline(tmp_path):
    _open_lumen(tmp_path)
    out = vmtk_runner.configure_mesh(tmp_path, strategy={"edge_length_factor": 0.3})
    assert out["code"] == "vmtk_seeds_required"
    assert not (tmp_path / "vmtk_spec.json").exists()


def test_geometry_report_lists_the_staged_ports(tmp_path):
    _open_lumen(tmp_path)
    _staging(tmp_path)
    rep = vmtk_runner.inspect_stl(tmp_path)
    assert [p["name"] for p in rep["staged_ports"]] == ["inlet", "outlet_1", "outlet_2"]
    assert rep["staged_ports"][0]["role"] == "inlet"
    assert rep["sizing_staged"] is True and rep["n_open_profiles"] == 1
    assert rep["local_radius_m"]["median"] == 0.1
    assert "local radius" in rep["note"].lower()


def test_stage_lumen_does_not_apply_to_surfaces_or_undeclared_runs(tmp_path):
    assert LS.stage_lumen(tmp_path, tmp_path / "lumen.stl", prepared=None,
                          intake_patches=[{"name": "inlet", "type": "inlet"}]) is None
    assert LS.stage_lumen(tmp_path, tmp_path / "body.step", prepared=None,
                          intake_patches=[]) is None
    assert not (tmp_path / LS.STAGING_FACT).exists()


def test_the_builder_hook_is_inert_without_geometry_or_hook(tmp_path):
    from meshpipeline.agents.builder import attempt
    attempt._stage_declared(tmp_path, None, {"intake_patches": []}, "vmtk")
    attempt._stage_declared(tmp_path, None, {}, "snappy")
    assert not (tmp_path / LS.STAGING_FACT).exists()
    assert not (tmp_path / LS.LUMEN_OPEN).exists()


def _mesh_with_caps(ws):
    # one tet; its four faces as boundary triangles: three on the "wall" (id 1), one cap (id 2)
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    cells = np.hstack([[4, 0, 1, 2, 3], [3, 0, 2, 1], [3, 0, 1, 3], [3, 1, 2, 3], [3, 0, 3, 2]])
    ctypes = np.array([vtk.VTK_TETRA] + [vtk.VTK_TRIANGLE] * 4, dtype=np.uint8)
    g = pv.UnstructuredGrid(cells.astype(np.int64), ctypes, pts)
    g.cell_data["CellEntityIds"] = np.array([0, 1, 1, 1, 2], dtype=np.int32)
    g.save(str(ws / "mesh.vtu"))
    return pts[[0, 3, 2]].mean(axis=0)           # centroid of the cap face


def test_viewer_names_caps_after_the_staged_ports_but_the_gate_form_keeps_ids(tmp_path):
    from meshpipeline.engines.vmtk.viewer_surface import surface_patches
    cap_c = _mesh_with_caps(tmp_path)
    (tmp_path / LS.STAGING_FACT).write_text(json.dumps({"ports": [
        {"name": "inlet", "role": "inlet", "centroid": cap_c.tolist(), "size_m": 1.0}]}))
    # (cap_0 is the tets' own outer faces, entity 0 - present in every real vmtk output too)
    named = surface_patches(tmp_path, named=True)
    assert {"wall", "inlet"} <= set(named) and "cap_2" not in named
    plain = surface_patches(tmp_path)
    assert {"wall", "cap_2"} <= set(plain) and "inlet" not in plain
