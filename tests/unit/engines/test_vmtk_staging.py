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
    # the floor guards against a zero-size target: half the cell at the radius floor
    assert s["min_edge_length"] == pytest.approx(0.15 * 0.10 * LS.RADIUS_LO_FRACTION
                                                 * LS.MIN_EDGE_OF_FLOOR)
    assert s["max_edge_length"] == pytest.approx(0.30 * LS.MAX_EDGE_FRACTION)
    assert s["min_edge_length"] < s["edge_bound"] < s["max_edge_length"]
    assert s["radius_lo"] == pytest.approx(0.10 * LS.RADIUS_LO_FRACTION)
    assert s["radius_hi"] == pytest.approx(0.30 * LS.RADIUS_HI_FRACTION)


def test_the_narrow_end_is_read_on_the_hydraulic_diameter():
    # an annulus (a 151 mm bore round a 125 mm rod) is as narrow as its 26 mm gap, a flat duct
    # as its thin side - the area-equivalent diameter (85 mm) put the radius floor above the
    # real half-gap and clipped the whole field up to it
    ring = {"name": "inlet", "size_m": 0.0854, "hydraulic_m": 0.0264, "area_m2": 0.00573}
    s = LS.sizing([ring, {**ring, "name": "outlet"}])
    assert s["radius_lo"] == pytest.approx(0.0264 * LS.RADIUS_LO_FRACTION)
    assert s["radius_lo"] < 0.5 * 0.0264                       # under the true half-gap
    assert s["edge_bound"] == pytest.approx(0.0264 / LS.RIM_DIVISIONS)
    assert s["max_edge_length"] == pytest.approx(0.0854 * LS.MAX_EDGE_FRACTION)


def test_an_opening_is_measured_with_its_hydraulic_diameter_and_loop_count():
    disk = pv.Disc(inner=0.0, outer=0.05, c_res=48, r_res=4).triangulate().clean()
    ring = pv.Disc(inner=0.04, outer=0.05, c_res=48, r_res=2).triangulate().clean()
    d = LS._measure_opening(disk)
    assert d["loops"] == 1 and d["hydraulic_m"] == pytest.approx(0.1, rel=0.02)
    a = LS._measure_opening(ring)
    assert a["loops"] == 2 and a["hydraulic_m"] == pytest.approx(0.02, rel=0.03)


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
    (tmp_path / "lumen_open.vtp").write_text("staged")   # what staging leaves
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
    (tmp_path / "lumen_open.vtp").write_text("staged")   # what staging leaves
    res = R._run_vmtk_local(tmp_path, timeout=1000)
    # the surface stage (700 s) and one generator step (700 s) exhaust the 1000 s budget: the
    # remaining ladder steps are not started, and the note says so
    assert len(calls) == 2
    assert "ladder step(s) not started" in res["repair_note"]
    assert "of the 1000 s budget left" in res["repair_note"]


def _polydata(pts, tri):
    return pv.PolyData(np.asarray(pts, dtype=float),
                       np.hstack([np.full((len(tri), 1), 3, dtype=np.int64), tri]).ravel())


def _folded(pts, tri):
    # the tube plus a fin on one of its edges (that edge now has three triangles) and a triangle
    # whose corners lie on one line (no area): what a remesh that folded the wall looks like
    a, b = tri[0][0], tri[0][1]
    n = len(pts)
    pts = np.vstack([pts, pts[a] + np.array([0.0, 0.0, 0.3]), pts[a], 0.5 * (pts[a] + pts[b])])
    return pts, np.vstack([tri, [[a, b, n], [a, n + 2, b]]])


def _staged_tube(ws):
    pts, tri = _tube()
    wall = _polydata(pts, tri)
    wall.point_data["LocalRadius"] = 1.0 + pts[:, 0]           # any per-point value
    wall.save(str(ws / "lumen_open.vtp"))
    return pts, tri


def test_wall_defects_finds_non_manifold_edges_and_zero_area_triangles(tmp_path):
    from meshpipeline.engines.vmtk import vmtk_runner as R
    pts, tri = _tube()
    _polydata(pts, tri).save(str(tmp_path / "clean.vtp"))
    assert R._wall_defects(tmp_path / "clean.vtp") == {}
    _polydata(*_folded(pts, tri)).save(str(tmp_path / "folded.vtp"))
    found = R._wall_defects(tmp_path / "folded.vtp")
    assert found["non-manifold edges"] >= 1 and found["zero-area triangles"] == 1
    (tmp_path / "junk.vtp").write_text("not a surface")
    assert R._wall_defects(tmp_path / "junk.vtp") == {}       # the generator will say so itself


def test_a_shuffled_wall_is_the_same_wall_with_its_radius_field(tmp_path):
    from meshpipeline.engines.vmtk import vmtk_runner as R
    _staged_tube(tmp_path)
    wall = pv.read(str(tmp_path / "lumen_open.vtp"))
    s = R._shuffled(wall, 1)
    assert not np.array_equal(np.asarray(s.points), np.asarray(wall.points))
    assert np.array_equal(np.unique(np.asarray(s.points), axis=0),
                          np.unique(np.asarray(wall.points), axis=0))
    assert np.allclose(np.asarray(s.point_data["LocalRadius"]), 1.0 + np.asarray(s.points)[:, 0])
    # the same triangles, each wound the same way: the same set of (centroid, normal)
    def faces(m):
        p = np.asarray(m.points)
        t = np.asarray(m.faces).reshape(-1, 4)[:, 1:]
        n = np.cross(p[t[:, 1]] - p[t[:, 0]], p[t[:, 2]] - p[t[:, 0]])
        return sorted(map(tuple, np.round(np.hstack([p[t].mean(axis=1), n]), 9)))
    assert faces(s) == faces(wall)
    assert np.array_equal(np.asarray(R._shuffled(wall, 1).points), np.asarray(s.points))  # fixed


def _surface_faker(tmp_path, pts, tri, folds):
    """A run_guarded stand-in: surface stage number k (from 0) writes a folded wall when
    folds(k, argv) says so, a clean one otherwise; the generator always fills."""
    import subprocess as sp
    calls: list[list[str]] = []
    seen: list[np.ndarray] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[1] == "vmtksurfaceremeshing":
            seen.append(np.asarray(pv.read(str(tmp_path / "lumen_open.vtp")).points).copy())
            k = sum(c[1] == "vmtksurfaceremeshing" for c in calls) - 1
            out = _polydata(*_folded(pts, tri)) if folds(k, argv) else _polydata(pts, tri)
            out.save(str(tmp_path / "lumen.vtp"))
            return sp.CompletedProcess(argv, 0, stdout="Done executing vmtksurfaceprojection.",
                                       stderr="")
        if argv[1] == "vmtkmeshgenerator":
            (tmp_path / "mesh.vtu").write_text("filled")
            return sp.CompletedProcess(argv, 0, stdout="Done executing vmtkmeshgenerator.",
                                       stderr="")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")    # the OpenFOAM export
    return fake_run, calls, seen


def test_a_clean_remesh_runs_once_as_before(tmp_path, monkeypatch):
    from meshpipeline.engines.vmtk import vmtk_runner as R
    pts, tri = _staged_tube(tmp_path)
    fake_run, calls, _ = _surface_faker(tmp_path, pts, tri, lambda k, argv: False)
    monkeypatch.setattr(R, "run_guarded", fake_run)
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=100)
    assert [c[1] for c in calls][:2] == ["vmtksurfaceremeshing", "vmtkmeshgenerator"]
    assert "-collapseangle" not in " ".join(calls[0])
    assert "repair_note" not in res


def test_a_folded_remesh_is_run_again_with_gentler_collapses(tmp_path, monkeypatch):
    from meshpipeline.engines.vmtk import vmtk_runner as R
    pts, tri = _staged_tube(tmp_path)
    # vmtk's default collapse folds this wall; the gentler one does not
    fake_run, calls, seen = _surface_faker(tmp_path, pts, tri,
                                           lambda k, argv: "-collapseangle" not in argv)
    monkeypatch.setattr(R, "run_guarded", fake_run)
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=100)
    assert [c[1] for c in calls][:3] == ["vmtksurfaceremeshing", "vmtksurfaceremeshing",
                                         "vmtkmeshgenerator"]
    assert "-collapseangle 0.1 --pipe" in " ".join(calls[1])
    assert np.array_equal(seen[0], seen[1])                 # the same wall, in the same order
    assert "came back folded (1 non-manifold edges, 1 zero-area triangles)" in res["repair_note"]
    assert "surface remesh 2: the wall came back clean" in res["repair_note"]
    assert res["rc"] == 0 and (tmp_path / "mesh.vtu").read_text() == "filled"


def test_a_wall_that_still_folds_is_remeshed_in_other_orders_then_goes_on(tmp_path, monkeypatch):
    from meshpipeline.engines.vmtk import vmtk_runner as R
    pts, tri = _staged_tube(tmp_path)
    fake_run, calls, seen = _surface_faker(tmp_path, pts, tri, lambda k, argv: True)
    monkeypatch.setattr(R, "run_guarded", fake_run)
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=100)
    n = 1 + len(R._SURFACE_RETRIES)
    assert [c[1] for c in calls][:n + 1] == ["vmtksurfaceremeshing"] * n + ["vmtkmeshgenerator"]
    # the shuffled retries remesh the same wall stored in other orders, its radius field intact
    assert not np.array_equal(seen[0], seen[2]) and not np.array_equal(seen[2], seen[3])
    assert np.array_equal(np.unique(seen[0], axis=0), np.unique(seen[3], axis=0))
    shuffled = pv.read(str(tmp_path / "lumen_open.vtp"))
    assert np.allclose(np.asarray(shuffled.point_data["LocalRadius"]),
                       1.0 + np.asarray(shuffled.points)[:, 0])
    assert "stored in another order" in res["repair_note"]
    assert "still folded" in res["repair_note"] and "going on with it" in res["repair_note"]
    assert "still folded" in (tmp_path / "log.vmtk").read_text()


def test_the_surface_retries_respect_the_budget(tmp_path, monkeypatch):
    from meshpipeline.engines.vmtk import vmtk_runner as R
    pts, tri = _staged_tube(tmp_path)
    clock = {"t": 0.0}
    fake_run, calls, _ = _surface_faker(tmp_path, pts, tri, lambda k, argv: True)

    def slow(argv, **kw):
        clock["t"] += 700.0
        return fake_run(argv, **kw)
    monkeypatch.setattr(R, "run_guarded", slow)
    monkeypatch.setattr(R, "_now", lambda: clock["t"])
    (tmp_path / "vmtk_spec.json").write_text(json.dumps(R.resolve_strategy(
        {"sizing_array": "LocalRadius", "boundary_layers": 3})))
    res = R._run_vmtk_local(tmp_path, timeout=1000)
    # the first remesh (700 s) and one retry (700 s) use the budget up: no third remesh
    assert [c[1] for c in calls].count("vmtksurfaceremeshing") == 2
    assert "of the budget is left to remesh it again" in res["repair_note"]


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
    (tmp_path / "lumen_open.vtp").write_text("staged")   # what staging leaves
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
        pm = pathlib.Path(kw["cwd"]) / "constant" / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        for n in ("points", "faces", "owner", "neighbour", "boundary"):
            (pm / n).write_text(n)
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    def fake_run_partial(argv, **kw):
        import pathlib
        pm = pathlib.Path(kw["cwd"]) / "constant" / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)
        (pm / "owner").write_text("owner")
        return sp.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    assert R.export_openfoam_case(tmp_path, timeout=300) == "openfoam_case"
    argv, kw = seen[0]
    assert "gmshToFoam ../mesh_volume.msh" in argv[-1] and kw["cwd"].endswith("openfoam_case")
    assert (tmp_path / "openfoam_case" / "system" / "controlDict").exists()
    assert (tmp_path / "mesh_volume.msh").exists()
    # a partial polyMesh (owner only) is not a case: removed, and None
    monkeypatch.setattr(R, "run_guarded", fake_run_partial)
    assert R.export_openfoam_case(tmp_path, timeout=300) is None
    assert not (tmp_path / "openfoam_case" / "constant" / "polyMesh").exists()
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
    (tmp_path / "lumen_open.vtp").write_text("staged")   # what staging leaves
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


def test_stage_lumen_does_not_apply_without_a_staged_surface_or_a_declaration(tmp_path):
    assert LS.stage_lumen(tmp_path, tmp_path / "lumen.stl", prepared=None,
                          intake_patches=[{"name": "inlet", "type": "inlet"}]) is None
    assert LS.stage_lumen(tmp_path, tmp_path / "body.step", prepared=None,
                          intake_patches=[]) is None
    assert not (tmp_path / LS.STAGING_FACT).exists()


def _open_tube_stl(path, r=0.02, length=0.2, n=32):
    from meshpipeline.cad.stl_io import write_stl_binary
    tris = []
    for k in range(n):
        a0, a1 = 2 * np.pi * k / n, 2 * np.pi * ((k + 1) % n) / n     # the seam closes exactly
        for j in range(4):
            x0, x1 = length * j / 4, length * (j + 1) / 4
            p = [[x, r * np.cos(a), r * np.sin(a)] for x in (x0, x1) for a in (a0, a1)]
            tris += [(p[0], p[3], p[2]), (p[0], p[1], p[3])]
    write_stl_binary(path, tris)


_TUBE_PORTS = [{"name": "inlet", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40},
               {"name": "outlet", "type": "outlet", "near_mm": [200, 0, 0], "diameter_mm": 40},
               {"name": "wall", "type": "wall"}]


def test_an_undeclared_surface_upload_is_still_staged_as_the_lumen_vmtk_inspects(tmp_path):
    """No ports declared yet: the upload itself becomes lumen.vtp, so geometry_report lists its
    open profiles instead of saying the workspace was never staged."""
    from meshpipeline.engines.vmtk.vmtk_runner import inspect_stl
    _open_tube_stl(tmp_path / "input.stl")
    assert LS.stage_lumen(tmp_path, tmp_path / "scan.stl", prepared=None, intake_patches=[]) is None
    rep = inspect_stl(tmp_path)
    assert not rep.get("error") and rep["n_open_profiles"] == 2


def test_a_step_whose_brep_cannot_be_opened_is_staged_from_its_own_surface(tmp_path, monkeypatch):
    """A faceted STEP (a shell of triangles, not a solid) fails the B-rep path; its staged
    surface is closed at the confirmed openings instead of the lumen never being written."""
    from meshpipeline.cad import cad_tessellate
    _open_tube_stl(tmp_path / "input.stl")

    def no_solid(*_a, **_k):
        raise RuntimeError("internal-flow input is not a watertight SOLID")
    monkeypatch.setattr(cad_tessellate, "tessellate_internal", no_solid)
    rec = LS.stage_lumen(tmp_path, tmp_path / "faceted.stp", prepared=None, intake_patches=_TUBE_PORTS)
    assert rec is not None and {p["name"] for p in rec["ports"]} == {"inlet", "outlet"}
    assert (tmp_path / LS.LUMEN_OPEN).exists()


def test_a_staging_failure_reaches_the_engines_report_with_its_true_reason(tmp_path, monkeypatch):
    from meshpipeline.agents.builder import attempt
    from meshpipeline.engines.vmtk import vmtk_runner

    def broken(*_a, **_k):
        raise ValueError("the surface holds too few triangles to bound a fluid")
    monkeypatch.setattr(vmtk_runner, "stage_declared", broken)
    monkeypatch.setattr("meshpipeline.cad.staging.staged_surface",
                        lambda g, p: type("S", (), {"consumed": None})())
    geometry = type("G", (), {"path": str(tmp_path / "scan.stl")})()
    attempt._stage_declared(tmp_path, geometry, {"intake_patches": _TUBE_PORTS}, "vmtk")
    rep = vmtk_runner.inspect_stl(tmp_path)
    assert "too few triangles" in rep["error"], rep


def test_stage_lumen_opens_a_surface_upload_at_its_confirmed_openings(tmp_path):
    """A capped vessel surface (STL, metres, staged as input.stl): the shared internal-flow staging
    finds each confirmed opening's capped face, and the lumen vmtk reads is the wall with those
    faces taken out - real holes, one per declared port, under the declared names."""
    from meshpipeline.cad.stl_io import write_stl_binary

    r, length, n = 0.02, 0.2, 32
    tris = []
    for k in range(n):
        a0, a1 = 2 * np.pi * k / n, 2 * np.pi * (k + 1) / n
        for j in range(4):
            x0, x1 = length * j / 4, length * (j + 1) / 4
            p = [[x, r * np.cos(a), r * np.sin(a)] for x in (x0, x1) for a in (a0, a1)]
            tris += [(p[0], p[3], p[2]), (p[0], p[1], p[3])]
        for x, s in ((0.0, -1), (length, 1)):
            q0, q1 = [x, r * np.cos(a0), r * np.sin(a0)], [x, r * np.cos(a1), r * np.sin(a1)]
            tris.append(([x, 0, 0], q1, q0) if s < 0 else ([x, 0, 0], q0, q1))
    write_stl_binary(tmp_path / "input.stl", tris)
    patches = [{"name": "aortic_root", "type": "inlet", "near_mm": [0, 0, 0], "diameter_mm": 40},
               {"name": "descending", "type": "outlet", "near_mm": [200, 0, 0], "diameter_mm": 40},
               {"name": "vessel", "type": "wall"}]
    rec = LS.stage_lumen(tmp_path, tmp_path / "upload.stl", prepared=None, intake_patches=patches,
                         input_kind="fluid-domain")
    assert rec is not None
    assert {p["name"] for p in rec["ports"]} == {"aortic_root", "descending"}
    assert rec["n_open_loops"] == 2, "the lumen must be open at exactly the two declared ports"
    assert (tmp_path / LS.LUMEN_OPEN).exists() and (tmp_path / "lumen.vtp").exists()


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
