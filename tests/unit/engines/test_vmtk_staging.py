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


def _flat_duct(width=0.5, gap=0.1, length=0.6, n=10):
    # an open rectangular duct (no end caps): four walls of a width x gap section
    xs = np.linspace(0, length, n + 1)
    ring = []                                   # section polygon, counter-clockwise
    for t in np.linspace(0, width, 6)[:-1]:
        ring.append((t - width / 2, -gap / 2))
    for t in np.linspace(0, gap, 3)[:-1]:
        ring.append((width / 2, t - gap / 2))
    for t in np.linspace(0, width, 6)[:-1]:
        ring.append((width / 2 - t, gap / 2))
    for t in np.linspace(0, gap, 3)[:-1]:
        ring.append((-width / 2, gap / 2 - t))
    m = len(ring)
    pts = np.array([[x, y, z] for x in xs for (y, z) in ring])
    tri = []
    for i in range(n):
        for j in range(m):
            a = i * m + j
            b = i * m + (j + 1) % m
            c = a + m
            d = b + m
            tri += [[a, b, d], [a, d, c]]
    return pts, np.asarray(tri, dtype=np.int64)


def test_local_radius_lets_a_narrow_gap_govern_the_walls_beside_it():
    # 500 x 100 mm duct: the wide walls see the 100 mm gap; the 100 mm-tall side walls look across
    # 500 mm. Without the ball-min the side walls would size 2.5x coarser than the gap allows.
    pts, tri = _flat_duct()
    r = LS.local_radius(pts, tri, interior_point=(0.3, 0.0, 0.0), r_lo=0.01, r_hi=0.3)
    side = np.abs(np.abs(pts[:, 1]) - 0.25) < 1e-9      # points on the two narrow side walls
    assert side.any()
    assert r[side].max() <= 0.05 + 0.02                 # the 100 mm gap, within a ring of smoothing
    assert r[~side].max() <= 0.05 + 0.02


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


def test_repair_ladder_varies_the_generator_remesh_then_the_layers_for_staged_runs_only():
    steps = vmtk_runner.repair_ladder({"sizing_array": "LocalRadius", "boundary_layers": 3,
                                       "boundary_layer_thickness_factor": 0.2})
    assert [(s["boundary_layers"], s["boundary_layer_thickness_factor"], s["generator_remesh"])
            for s in steps] == [(3, 0.2, True), (3, 0.2, False), (3, 0.1, False),
                                (0, 0.2, True), (0, 0.2, False)]
    bare = vmtk_runner.repair_ladder({"sizing_array": "LocalRadius", "boundary_layers": 0})
    assert [s["generator_remesh"] for s in bare] == [True, False]
    assert len(vmtk_runner.repair_ladder({"source_ids": [0], "target_ids": [1]})) == 1


def test_staged_stages_split_the_surface_from_the_generator():
    surface, generate = vmtk_runner.build_staged_stages(
        {"sizing_array": "LocalRadius", "generator_remesh": False, "boundary_layers": 0})
    assert surface[1] == "vmtksurfaceremeshing" and surface[-2:] == ["-ofile", "lumen.vtp"]
    assert generate[1] == "vmtkmeshgenerator" and "-skipremeshing 1" in " ".join(generate)
    assert "-boundarylayer 0" in " ".join(generate) and "-sublayers" not in " ".join(generate)


def test_run_walks_the_ladder_when_tetgen_gives_up(tmp_path, monkeypatch):
    import subprocess as sp

    from meshpipeline.engines.vmtk import vmtk_runner as R
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        joined = " ".join(argv)
        if "vmtksurfaceremeshing" in joined:
            (tmp_path / "lumen.vtp").write_text("remeshed")
            return sp.CompletedProcess(argv, 0, stdout="Done executing vmtksurfaceprojection.",
                                       stderr="")
        if "-skipremeshing 0" in joined:            # the generator's own remesh: TetGen gives up
            (tmp_path / "mesh.vtu").write_text("layers only")
            return sp.CompletedProcess(argv, 0, stdout="Generating volume mesh\nTetGen quit "
                                       "with an exception.", stderr="")
        (tmp_path / "mesh.vtu").write_text("filled")
        return sp.CompletedProcess(argv, 0, stdout="Done executing vmtkmeshgenerator.", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=10)
    # the surface stage once, then two generator attempts
    assert res["rc"] == 0 and len(calls) == 3
    assert calls[0][1] == "vmtksurfaceremeshing"
    assert calls[1][1] == "vmtkmeshgenerator" and calls[2][1] == "vmtkmeshgenerator"
    assert "-skipremeshing 1" in " ".join(calls[2]) and "-sublayers 3" in " ".join(calls[2])
    assert "generator remesh off" in res["repair_note"]
    assert "last generator stage: Generating volume mesh" in res["repair_note"]
    shipped = json.loads((tmp_path / "vmtk_spec.json").read_text())
    assert shipped["generator_remesh"] is False and shipped["boundary_layers"] == 3
    assert "repair_note" in shipped
    assert (tmp_path / "mesh.vtu").read_text() == "filled"
    assert "next:" in (tmp_path / "log.vmtk").read_text()


def test_the_ladder_shares_one_time_budget(tmp_path, monkeypatch):
    import subprocess as sp

    from meshpipeline.engines.vmtk import vmtk_runner as R
    clock = {"t": 0.0}
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        clock["t"] += 700.0                       # every stage takes 700 s
        if "vmtksurfaceremeshing" in " ".join(argv):
            (tmp_path / "lumen.vtp").write_text("remeshed")
            return sp.CompletedProcess(argv, 0, stdout="Done executing vmtksurfaceprojection.",
                                       stderr="")
        return sp.CompletedProcess(argv, 0, stdout="TetGen quit with an exception.", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    monkeypatch.setattr(R, "_now", lambda: clock["t"])
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=1000)
    # the surface stage (700 s) and one generator step (700 s) exhaust the 1000 s budget: the
    # remaining ladder steps are not started, and the note says so
    assert len(calls) == 2
    assert "ladder step(s) not started" in res["repair_note"]
    assert "of the 1000 s budget left" in res["repair_note"]


def test_an_unchanged_retry_keeps_its_pass_identity(tmp_path, monkeypatch):
    from meshpipeline.contracts import mesh_execution as ME
    from meshpipeline.engines.vmtk import vmtk_runner as R
    monkeypatch.setattr(ME, "run_mesh", lambda ws, **kw: {"rc": 0})
    (tmp_path / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.3}))
    R.run_cartesian_mesh(tmp_path, timeout=10)
    R.run_cartesian_mesh(tmp_path, timeout=10)              # an unchanged retry
    assert ME.read_native_pass(tmp_path) == 1
    (tmp_path / "vmtk_spec.json").write_text(json.dumps({"edge_length_factor": 0.25}))
    R.run_cartesian_mesh(tmp_path, timeout=10)              # a revised spec: its own pass
    assert ME.read_native_pass(tmp_path) == 2


def test_a_completed_fill_is_also_written_as_an_openfoam_case(tmp_path, monkeypatch):
    import subprocess as sp

    from meshpipeline.engines.vmtk import vmtk_runner as R
    exported: list = []

    def fake_run(argv, **kw):
        if "vmtksurfaceremeshing" in " ".join(argv):
            (tmp_path / "lumen.vtp").write_text("remeshed")
        else:
            (tmp_path / "mesh.vtu").write_text("filled")
        return sp.CompletedProcess(argv, 0, stdout="Done executing.", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    monkeypatch.setattr(R, "export_openfoam_case", lambda ws, **kw: exported.append(kw) or "openfoam_case")
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=900)
    assert res["rc"] == 0 and res["openfoam_case"] == "openfoam_case"
    assert len(exported) == 1 and 0 < exported[0]["timeout"] <= 900


def test_the_gmsh_volume_file_names_every_patch(tmp_path):
    from meshpipeline.engines.vmtk.vmtk_runner import _write_gmsh_volume
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    faces = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]
    cells = np.hstack([[4, 0, 1, 2, 3]] + [[3, *f] for f in faces]).astype(np.int64)
    ctypes = np.array([vtk.VTK_TETRA] + [vtk.VTK_TRIANGLE] * 4, dtype=np.uint8)
    g = pv.UnstructuredGrid(cells, ctypes, pts)
    g.cell_data["CellEntityIds"] = np.array([0, 1, 1, 1, 2], dtype=np.int32)
    facts = _write_gmsh_volume(g, {1: "wall", 2: "inlet"}, tmp_path / "mesh_volume.msh")
    text = (tmp_path / "mesh_volume.msh").read_text()
    assert facts == {"nodes": 4, "tets": 1, "triangles": 4, "patches": {1: "wall", 2: "inlet"}}
    assert text.startswith("$MeshFormat\n2.2 0 8")
    assert '2 1 "wall"' in text and '2 2 "inlet"' in text and '3 100 "fluid"' in text
    assert "$Nodes\n4\n" in text and "$Elements\n5\n" in text
    # the cap triangle carries tag 2, the tet the fluid tag, both 1-based node ids
    lines = text.split("$Elements\n5\n", 1)[1].splitlines()
    assert lines[3].split()[3:5] == ["2", "2"] and lines[4].split()[1:5] == ["4", "2", "100", "100"]


def test_export_openfoam_case_runs_gmshtofoam_in_a_fresh_case(tmp_path, monkeypatch):
    import subprocess as sp

    from meshpipeline.engines.vmtk import vmtk_runner as R
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    faces = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]
    cells = np.hstack([[4, 0, 1, 2, 3]] + [[3, *f] for f in faces]).astype(np.int64)
    ctypes = np.array([vtk.VTK_TETRA] + [vtk.VTK_TRIANGLE] * 4, dtype=np.uint8)
    g = pv.UnstructuredGrid(cells, ctypes, pts)
    g.cell_data["CellEntityIds"] = np.array([0, 1, 1, 1, 2], dtype=np.int32)
    g.save(str(tmp_path / "mesh.vtu"))
    seen: list = []

    def fake_run(argv, **kw):
        import pathlib
        seen.append((argv, kw))
        (pathlib.Path(kw["cwd"]) / "constant" / "polyMesh").mkdir(parents=True)
        (pathlib.Path(kw["cwd"]) / "constant" / "polyMesh" / "owner").write_text("faces")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    assert R.export_openfoam_case(tmp_path, timeout=300) == "openfoam_case"
    argv, kw = seen[0]
    assert "gmshToFoam ../mesh_volume.msh" in argv[-1] and kw["cwd"].endswith("openfoam_case")
    assert (tmp_path / "openfoam_case" / "system" / "controlDict").exists()
    assert (tmp_path / "mesh_volume.msh").exists()
    # a failed conversion is a None, never an exception
    monkeypatch.setattr(R, "run_guarded",
                        lambda argv, **kw: sp.CompletedProcess(argv, 1, stdout="boom", stderr=""))
    assert R.export_openfoam_case(tmp_path, timeout=300) is None


def test_run_makes_one_attempt_when_nothing_was_staged(tmp_path, monkeypatch):
    import subprocess as sp

    from meshpipeline.engines.vmtk import vmtk_runner as R
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return sp.CompletedProcess(argv, 0, stdout="TetGen quit with an exception.", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    (tmp_path / "vmtk_spec.json").write_text(json.dumps({"source_ids": [0], "target_ids": [1],
                                                        "boundary_layers": 3}))
    res = R._run_vmtk_local(tmp_path, timeout=10)
    assert len(calls) == 1 and "repair_note" not in res


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


def test_finalize_writes_the_review_surface_under_the_declared_names(tmp_path):
    pytest.importorskip("gmsh")
    cap_c = _mesh_with_caps(tmp_path)
    (tmp_path / LS.STAGING_FACT).write_text(json.dumps({"ports": [
        {"name": "inlet", "role": "inlet", "centroid": cap_c.tolist(), "size_m": 1.0}]}))
    intake = [{"name": "inlet", "type": "inlet"}, {"name": "wall", "type": "wall"}]
    out = vmtk_runner.finalize(str(tmp_path), intake, "vmtk", "internal flow", True, {}, "")
    assert out["success"] is True
    msh = tmp_path / "mesh.msh"
    assert msh.exists() and msh.read_bytes().startswith(b"$MeshFormat")
    text = msh.read_text(errors="replace")
    assert '"inlet"' in text and '"wall"' in text and "cap_0" not in text
    man = json.loads((tmp_path / "mesh_manifest.json").read_text())
    assert man["patches"]["inlet"] and man["patches"]["wall"]     # the msh entities
    assert man["mesh_paths"]["surface"].endswith("mesh.msh")


def test_finalize_fails_the_delivery_when_the_review_surface_is_not_written(tmp_path, monkeypatch):
    pytest.importorskip("gmsh")
    from meshpipeline.render import review_artifacts as RA
    cap_c = _mesh_with_caps(tmp_path)
    (tmp_path / LS.STAGING_FACT).write_text(json.dumps({"ports": [
        {"name": "inlet", "role": "inlet", "centroid": cap_c.tolist(), "size_m": 1.0}]}))

    def boom(*a, **kw):
        raise RuntimeError("gmsh refused")

    monkeypatch.setattr(RA, "build_review_msh", boom)
    intake = [{"name": "inlet", "type": "inlet"}, {"name": "wall", "type": "wall"}]
    out = vmtk_runner.finalize(str(tmp_path), intake, "vmtk", "internal flow", True, {}, "")
    assert out["success"] is False
    assert "mesh.msh" in out["output"]
    man = json.loads((tmp_path / "mesh_manifest.json").read_text())
    assert any("mesh.msh" in f for f in man["quality"]["fatal"])


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
