# Responsibility: Verify VMTK's cell budget - the cost a staged lumen predicts, the order in which a
#                 fill is sized into its budget, and the run's re-check on the remeshed wall.
from __future__ import annotations

import json
import subprocess as sp

import numpy as np
import pytest
import pyvista as pv

from meshpipeline.engines.vmtk import budget as B
from meshpipeline.engines.vmtk import lumen_staging as LS
from meshpipeline.engines.vmtk import vmtk_runner as R

# a model whose default fill (edge factor 0.15, five layers) is ~2.5 M cells, two thirds of it
# in the layers - about what the human aorta measured (wall 42k, interior 281k)
_MODEL = {"elf_ref": 0.15, "wall": 10_000.0, "interior": 35_000.0, "volume_m3": 1e-4}


def _cells(model=_MODEL, **kw):
    return B.predict(model, **kw)["cells"]


def test_the_layer_stack_is_three_tets_per_wall_triangle_per_sublayer():
    p = B.predict(_MODEL, edge_length_factor=0.15, boundary_layers=5)
    assert p["wall_triangles"] == round(B.WALL_TRIANGLES_PER_AREA * 10_000)
    assert p["layer_tets"] == 3 * 5 * p["wall_triangles"]
    # a known remeshed wall replaces the model's count, exactly
    q = B.predict(_MODEL, edge_length_factor=0.15, boundary_layers=5, wall_triangles=20_000)
    assert q["layer_tets"] == 3 * 5 * 20_000


def test_the_interior_scales_with_the_cube_of_the_volume_factor_and_the_edge():
    base = B.predict(_MODEL, edge_length_factor=0.15)["interior_tets"]
    assert B.predict(_MODEL, edge_length_factor=0.15, volume_factor=1.6)["interior_tets"] \
        == pytest.approx(base / 8, rel=1e-4)
    assert B.predict(_MODEL, edge_length_factor=0.3)["interior_tets"] \
        == pytest.approx(base / 8, rel=1e-4)


def test_a_fill_inside_its_budget_is_left_alone():
    s = {"edge_length_factor": 0.15, "boundary_layers": 5, "volume_element_factor": 0.8}
    out, plan = B.fit(s, _MODEL, requested=8_000_000, hard_limit=8_000_000, min_cells_across=12)
    assert out == s and plan["steps"] == [] and plan["within"] == "request"


def test_the_interior_coarsens_first_and_the_wall_and_layers_stay():
    s = {"edge_length_factor": 0.15, "boundary_layers": 5, "volume_element_factor": 0.8}
    # a budget the interior alone can make room for
    want = int(_cells(edge_length_factor=0.15, boundary_layers=5, volume_factor=1.2) / 0.85) + 1
    out, plan = B.fit(s, _MODEL, requested=want, hard_limit=8_000_000, min_cells_across=12)
    assert out["edge_length_factor"] == 0.15 and out["boundary_layers"] == 5
    assert 0.8 < out["volume_element_factor"] <= 1.2 + 1e-3
    assert plan["predicted"] <= 0.85 * want + 1
    assert plan["steps"][0].startswith("interior coarsened")


def test_the_edge_factor_rises_only_to_the_passage_floor():
    s = {"edge_length_factor": 0.15, "boundary_layers": 5, "volume_element_factor": 0.8}
    out, plan = B.fit(s, _MODEL, requested=10_000, hard_limit=8_000_000, min_cells_across=12)
    assert out["edge_length_factor"] == pytest.approx(B.edge_factor_ceiling(12), rel=1e-3)
    assert out["volume_element_factor"] == pytest.approx(B.VOLUME_FACTOR_MAX)
    # a REQUESTED budget never costs the user's layers: over the request, under the limit
    assert out["boundary_layers"] == 5 and plan["within"] == "compute limit"
    assert "over the request but under the compute limit" in B.plan_words(plan)


def test_only_the_compute_limit_thins_the_layers():
    big = {k: v * 20 for k, v in _MODEL.items() if k != "elf_ref"} | {"elf_ref": 0.15}
    s = {"edge_length_factor": 0.15, "boundary_layers": 5, "volume_element_factor": 0.8}
    out, plan = B.fit(s, big, requested=8_000_000, hard_limit=8_000_000, min_cells_across=12)
    assert out["boundary_layers"] < 5
    assert any("compute limit" in step for step in plan["steps"])
    assert plan["predicted"] <= 0.85 * 8_000_000 or out["boundary_layers"] == 0


def test_the_cost_model_of_a_tube_reads_its_area_and_volume():
    tube = pv.Cylinder(radius=0.02, height=0.4, resolution=64, capping=False).triangulate()
    tube = tube.subdivide(2).clean()
    pts = np.asarray(tube.points)
    faces = np.asarray(tube.faces).reshape(-1, 4)[:, 1:]
    r = np.full(len(pts), 0.02)
    closed = pv.Cylinder(radius=0.02, height=0.4, resolution=64, capping=True).triangulate()
    cpts = np.asarray(closed.points)
    tris = cpts[np.asarray(closed.faces).reshape(-1, 4)[:, 1:]]
    m = B.cost_model(pts, faces, r, tris, h_min=0.0, h_max=1.0, samples=3000)
    area = 2 * np.pi * 0.02 * 0.4
    assert m["wall"] == pytest.approx(area / (0.15 * 0.02) ** 2, rel=0.05)
    assert m["volume_m3"] == pytest.approx(np.pi * 0.02 ** 2 * 0.4, rel=0.1)
    assert m["interior"] > 0


def test_staging_measures_the_cost_of_the_piece_vmtk_keeps():
    tube = pv.Cylinder(radius=0.02, height=0.4, resolution=48, capping=False).triangulate()
    tube = tube.subdivide(1).clean()
    tube.point_data[LS.SIZING_ARRAY] = np.full(tube.n_points, 0.02)
    m = LS.lumen_cost_model(tube, {"min_edge_length": 1e-4, "max_edge_length": 0.01})
    assert m["volume_m3"] == pytest.approx(np.pi * 0.02 ** 2 * 0.4, rel=0.15)
    assert m["wall"] > 0 and m["interior"] > 0


def _staged(ws, model):
    tri = pv.PolyData(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float), np.array([3, 0, 1, 2]))
    tri.save(str(ws / "lumen.vtp"))
    rec = {"ports": [{"name": "inlet", "role": "inlet", "centroid": [0, 0, 0], "size_m": 0.1,
                      "area_m2": 0.008}],
           "source_points": [0.0, 0.0, 0.0], "target_points": [1.0, 0.0, 0.0],
           "sizing_array": LS.SIZING_ARRAY, "min_edge_length": 1e-4, "max_edge_length": 0.02,
           "radius_m": {"min": 0.05, "median": 0.05, "max": 0.05}, "cost_model": model}
    (ws / LS.STAGING_FACT).write_text(json.dumps(rec))


def test_configure_mesh_sizes_the_fill_to_the_requested_budget(tmp_path):
    _staged(tmp_path, _MODEL)
    out = R.configure_mesh(tmp_path, strategy={"max_cells": 1_000_000})
    spec = out["spec"]
    assert spec["volume_element_factor"] > 0.8
    assert "-volumeelementfactor" in out["pype"]
    assert out["predicted_cells"] == spec["budget_plan"]["predicted"]
    assert spec["budget_plan"]["asked"]["volume_element_factor"] == 0.8
    # within budget: the pype is the one it always was
    roomy = R.configure_mesh(tmp_path, strategy={"max_cells": 8_000_000})
    assert "-volumeelementfactor" not in roomy["pype"]


def test_the_run_refits_the_interior_to_the_remeshed_wall(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if "vmtksurfaceremeshing" in " ".join(argv):
            # the remesh came back with three times the triangles the model expected
            wall = pv.Plane(i_resolution=200, j_resolution=165).triangulate()
            wall.save(str(tmp_path / "lumen.vtp"))
            return sp.CompletedProcess(argv, 0, stdout="Done executing.", stderr="")
        (tmp_path / "mesh.vtu").write_text("filled")
        return sp.CompletedProcess(argv, 0, stdout="Done executing vmtkmeshgenerator.", stderr="")

    monkeypatch.setattr(R, "run_guarded", fake_run)
    monkeypatch.setattr(R, "_folded_share", lambda ws: None)
    _staged(tmp_path, _MODEL)
    R.configure_mesh(tmp_path, strategy={"max_cells": 2_500_000})
    (tmp_path / "lumen_open.vtp").write_text("staged")
    res = R._run_vmtk_local(tmp_path, timeout=100)
    shipped = json.loads((tmp_path / "vmtk_spec.json").read_text())
    assert "re-sized to its budget" in res["repair_note"]
    assert shipped["volume_element_factor"] > 0.8
    gen = [c for c in calls if len(c) > 1 and c[1] == "vmtkmeshgenerator"]
    assert f"-volumeelementfactor {shipped['volume_element_factor']:g}" in " ".join(gen[-1])
