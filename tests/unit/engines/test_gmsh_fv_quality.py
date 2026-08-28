# Responsibility: Verify the gmsh path measures FV quality with checkMesh's formulas, optimizes
#   until the bars clear, gates fail-closed on a fluid domain, and honors element_order end-to-end.
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_DIR = Path(__file__).parents[3]

#: The calibrated quality-audit scorer these formulas are vendored from. The parity test runs
#: wherever the scorer checkout exists (the calibration machine); elsewhere it skips - the
#: vendored copies plus the known-answer test below still pin the arithmetic.
_SCORER_DIR = Path(os.environ.get("MESHSCORE_SCORER_DIR",
                                  str(Path.home() / "quality_audit" / "scorer")))


def _tiny_tet_mesh() -> tuple[np.ndarray, np.ndarray]:
    """Two tets sharing the (1,2,3) face - the smallest mesh with an internal face."""
    pts = np.array([[0.0, 0.0, 0.0],
                    [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0],
                    [0.0, 0.0, 1.0],
                    [1.1, 1.2, 1.3]])
    tets = np.array([[0, 1, 2, 3], [1, 2, 3, 4]])
    return pts, tets


def _meshed_pts_tets():
    """A real (small) gmsh tet mesh of a cylinder, raw Delaunay - varied face shapes."""
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import fv_metrics
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("parity")
        gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 0.1, 0.03)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.02)
        gmsh.option.setNumber("Mesh.Optimize", 0)
        gmsh.option.setNumber("Mesh.OptimizeNetgen", 0)
        gmsh.model.mesh.generate(3)
        return fv_metrics.extract_tets(gmsh)
    finally:
        gmsh.finalize()


# formula parity - the vendored implementations ARE the scorer's, to 1e-9

@pytest.mark.skipif(not _SCORER_DIR.exists(), reason="calibrated scorer checkout not present")
def test_vendored_formulas_agree_with_the_calibrated_scorer_to_1e9():
    sys.path.insert(0, str(_SCORER_DIR))
    try:
        from meshscore.metrics import compute_geometry, summarize_geometry
        from meshscore.msh_volume import build_from_tets
    finally:
        sys.path.remove(str(_SCORER_DIR))
    from meshpipeline.engines.gmsh import fv_metrics

    for pts, tets in (_tiny_tet_mesh(), _meshed_pts_tets()):
        # the scorer's face-addressed build (empty patch table) vs the vendored one
        mesh, _info = build_from_tets(pts, tets, {})
        g = compute_geometry(mesh.points, mesh.face_flat, mesh.face_off,
                             mesh.owner, mesh.neighbour)
        ff, fo, own, nei = fv_metrics.tet_face_topology(pts, tets)
        assert np.array_equal(own, mesh.owner) and np.array_equal(nei, mesh.neighbour)
        assert np.array_equal(ff, mesh.face_flat) and np.array_equal(fo, mesh.face_off)

        fC, fA = fv_metrics.face_geometry(pts, ff, fo)
        cC, _cV, _ = fv_metrics.cell_geometry(fC, fA, own, nei, len(tets))
        no = fv_metrics.non_orthogonality(cC, fC, fA, own, nei)
        sk = fv_metrics.skewness(pts, ff, fo, fC, fA, cC, own, nei)
        assert np.max(np.abs(no - g.nonortho)) < 1e-9
        assert np.max(np.abs(sk - g.skew)) < 1e-9

        # and the summary reports the scorer's own conventions (acos-mean-cos average)
        s = fv_metrics.fv_summary(pts, tets)
        ref = summarize_geometry(g)
        assert abs(s["max_non_ortho"] - ref["non_orthogonality"]["max"]) < 1e-3
        assert abs(s["avg_non_ortho"] - ref["non_orthogonality"]["avg"]) < 1e-3
        assert s["non_ortho_over_65"] == ref["non_orthogonality"]["n_over_65"]
        assert s["non_ortho_over_70"] == ref["non_orthogonality"]["n_over_70"]
        assert abs(s["max_skewness"] - ref["skewness"]["max"]) < 1e-3


def test_known_answer_non_orthogonality_on_two_tets():
    # Independent arithmetic: tet centroids are vertex means, the shared triangle's centre
    # and area vector are the exact triangle formulas - compute the angle by hand.
    from meshpipeline.engines.gmsh import fv_metrics
    pts, tets = _tiny_tet_mesh()
    ff, fo, own, nei = fv_metrics.tet_face_topology(pts, tets)
    assert len(nei) == 1                                  # exactly one internal face
    fC, fA = fv_metrics.face_geometry(pts, ff, fo)
    cC, cV, _ = fv_metrics.cell_geometry(fC, fA, own, nei, 2)
    assert np.max(np.abs(cC[0] - pts[[0, 1, 2, 3]].mean(axis=0))) < 1e-12
    assert np.max(np.abs(cC[1] - pts[[1, 2, 3, 4]].mean(axis=0))) < 1e-12

    d = cC[1] - cC[0]
    s = fA[0]                                             # internal face is face 0
    expected = np.degrees(np.arccos(
        np.dot(d, s) / (np.linalg.norm(d) * np.linalg.norm(s))))
    no = fv_metrics.non_orthogonality(cC, fC, fA, own, nei)
    assert abs(float(no[0]) - expected) < 1e-9
    # volumes: signed tet volume formula, positive after orientation fix
    assert abs(float(cV[0]) - 1.0 / 6.0) < 1e-12


# the optimization ladder - a deliberately-bad raw mesh gets measurably better

def test_ladder_improves_a_raw_delaunay_mesh_below_the_warn_bar():
    gmsh = pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("ladder")
        # a branched cylinder meshed coarsely with NO optimization: raw Delaunay slivers
        gmsh.model.occ.addCylinder(0, 0, 0, 0, 0, 0.2, 0.03)
        gmsh.model.occ.addCylinder(0, 0, 0.1, 0.08, 0, 0, 0.015, tag=2)
        gmsh.model.occ.fuse([(3, 1)], [(3, 2)])
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", 0.012)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 12)
        gmsh.option.setNumber("Mesh.Optimize", 0)
        gmsh.option.setNumber("Mesh.OptimizeNetgen", 0)
        gmsh.model.mesh.generate(3)
        final, history = driver._fv_measure_and_optimize(gmsh)
    finally:
        gmsh.finalize()
    assert final is not None and len(history) >= 2, history
    initial = history[0]
    assert final["max_non_ortho"] < initial["max_non_ortho"], (initial, final)
    th = driver._fv_thresholds()
    assert final["max_non_ortho"] <= th["nonortho_warn"], (final, history)
    assert final["max_skewness_internal"] <= th["skew_internal_hard"]


def test_ladder_is_deterministic_and_stops_once_targets_are_met():
    from meshpipeline.engines.gmsh import driver
    th = driver._fv_thresholds()
    good = {"max_non_ortho": 30.0, "max_skewness_internal": 0.5,
            "max_skewness_boundary": 0.5}
    assert driver._fv_targets_met(good, th)
    assert not driver._fv_targets_met({**good, "max_non_ortho": th["nonortho_warn"] + 1}, th)
    # the ladder sequence is a pure function of the pass budget
    n = th["max_passes"]
    seq = [driver._FV_LADDER_CYCLE[i % len(driver._FV_LADDER_CYCLE)] for i in range(n)]
    assert seq == [driver._FV_LADDER_CYCLE[i % 3] for i in range(n)]


# the hard gate - fail-closed for a fluid domain, advisory for structural, n/a for 2D

def _fv_ok_quality() -> dict:
    return {"cells": 100, "min_sicn": 0.5, "fatal": [],
            "max_non_ortho": 55.0, "avg_non_ortho": 20.0,
            "non_ortho_over_65": 0, "non_ortho_over_70": 0,
            "max_skewness": 1.2, "max_skewness_internal": 1.2,
            "max_skewness_boundary": 1.9}


def _manifest_ws(tmp_path, quality: dict, *, flow_topology: str = "",
                 purpose: str = "") -> Path:
    ws = tmp_path
    (ws / "mesh_manifest.json").write_text(json.dumps({
        "schema_version": "2.1", "geometry": {}, "patches": {}, "validation": {},
        "quality": quality}))
    if flow_topology:
        (ws / "flow_topology").write_text(flow_topology)
    if purpose:
        (ws / "purpose").write_text(purpose)
    return ws


def test_gate_fails_a_fluid_domain_over_the_severe_nonortho_bar(tmp_path):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_fv_quality
    q = {**_fv_ok_quality(), "max_non_ortho": 73.9, "non_ortho_over_70": 12}
    ws = _manifest_ws(tmp_path, q, flow_topology="internal")
    ok, fb = _gate_fv_quality(GateCtx(workspace=ws, engine="gmsh"))
    assert not ok
    assert "[QUALITY]" in fb and "73.9" in fb and "70" in fb, fb
    assert "gmsh_spec.json" in fb            # actionable: names the fix surface


def test_gate_fails_a_fluid_domain_on_checkmesh_severe_skewness(tmp_path):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_fv_quality
    q = {**_fv_ok_quality(), "max_skewness_internal": 4.6}
    ws = _manifest_ws(tmp_path, q, flow_topology="internal")
    ok, fb = _gate_fv_quality(GateCtx(workspace=ws, engine="gmsh"))
    assert not ok and "4.6" in fb and "maxInternalSkewness" in fb


def test_gate_passes_a_clean_fluid_domain(tmp_path):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_fv_quality
    ws = _manifest_ws(tmp_path, _fv_ok_quality(), flow_topology="internal")
    ok, fb = _gate_fv_quality(GateCtx(workspace=ws, engine="gmsh"))
    assert ok, fb


def test_gate_fails_closed_when_a_fluid_domain_was_never_measured(tmp_path):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_fv_quality
    q = {"cells": 100, "min_sicn": 0.5, "fatal": []}       # no FV keys at all
    ws = _manifest_ws(tmp_path, q, flow_topology="internal")
    ok, fb = _gate_fv_quality(GateCtx(workspace=ws, engine="gmsh"))
    assert not ok and "missing" in fb and "fail-closed" in fb


def test_gate_scopes_hard_by_purpose_not_engine(tmp_path):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_fv_quality
    over = {**_fv_ok_quality(), "max_non_ortho": 80.0}
    # a fluid PURPOSE (no flow_topology file) still gates hard
    ws = tmp_path / "cfd"
    ws.mkdir()
    _manifest_ws(ws, over, purpose="internal_cfd")
    ok, _fb = _gate_fv_quality(GateCtx(workspace=ws, engine="gmsh"))
    assert not ok
    # a structural deck over the bar passes (advisory) - FE assembly does not integrate
    # over face non-orthogonality; the criteria report still shows the number
    ws2 = (tmp_path / "fea")
    ws2.mkdir()
    _manifest_ws(ws2, over, purpose="structural")
    ok2, fb2 = _gate_fv_quality(GateCtx(workspace=ws2, engine="gmsh"))
    assert ok2, fb2


def test_gate_not_applicable_to_a_2d_planar_mesh(tmp_path):
    from meshpipeline.engines.gates import GateCtx
    from meshpipeline.engines.gmsh.gates import _gate_fv_quality
    q = {"cells": 100, "min_sicn": 0.5, "fatal": [], "dimensionality": "2D"}
    ws = _manifest_ws(tmp_path, q)
    ok, fb = _gate_fv_quality(GateCtx(workspace=ws, engine="gmsh"))
    assert ok, fb


def test_run_enricher_blocks_submission_on_a_hard_fv_breach(tmp_path):
    from meshpipeline.engines.gmsh.gmsh_runner import run_enricher
    (tmp_path / "flow_topology").write_text("internal")
    q = {**_fv_ok_quality(), "max_non_ortho": 74.2}
    out = {"success": True, "mesh_ok": True, "fatal_defects": []}
    run_enricher(None, tmp_path, {"rc": 0}, q, out)
    assert out["success"] is False and out["mesh_ok"] is False
    assert "do not submit" in out["guidance"].lower() and "74.2" in out["guidance"]
    # a clean mesh keeps the ordinary submit guidance
    out2 = {"success": True, "mesh_ok": True, "fatal_defects": []}
    run_enricher(None, tmp_path, {"rc": 0}, _fv_ok_quality(), out2)
    assert out2["success"] is True and "submit_mesh" in out2["guidance"]


# criteria registry - the reviewer sees the measured numbers beside checkMesh's bars

def test_gmsh_declares_the_fv_criteria_rows_beside_the_fe_rows():
    import meshpipeline.engines.gmsh.settings as gq
    from meshpipeline.engines.quality_criteria import criteria_for, evaluate
    rows = {c.key: c for c in criteria_for("gmsh")}
    assert rows["max_non_ortho"].gating is False           # snappy's convention: advisory row,
    assert rows["max_non_ortho"].threshold == gq.GMSH_FV_NONORTHO_WARN   # hard bar in the gate
    assert rows["max_skewness_internal"].threshold == gq.GMSH_FV_SKEW_INTERNAL_HARD
    assert rows["max_skewness_boundary"].threshold == gq.GMSH_FV_SKEW_BOUNDARY_HARD
    assert {"min_sicn", "fatal", "rc", "timed_out"} <= set(rows)   # the FE rows survive
    report = {r["key"]: r for r in evaluate("gmsh", _fv_ok_quality())}
    assert report["max_non_ortho"]["measured"] == 55.0
    assert report["max_non_ortho"]["passed"] is True
    report_bad = {r["key"]: r for r in evaluate("gmsh", {**_fv_ok_quality(),
                                                         "max_non_ortho": 68.0})}
    assert report_bad["max_non_ortho"]["passed"] is False  # warn bar flags 65..70 to the reviewer


def test_fv_settings_defaults_match_checkmesh_conventions():
    import meshpipeline.engines.gmsh.settings as gq
    assert gq.GMSH_FV_NONORTHO_HARD == 70.0
    assert gq.GMSH_FV_NONORTHO_WARN == 65.0
    assert gq.GMSH_FV_SKEW_INTERNAL_HARD == 4.0
    assert gq.GMSH_FV_SKEW_BOUNDARY_HARD == 20.0
    assert gq.GMSH_FV_NONORTHO_WARN <= gq.GMSH_FV_NONORTHO_HARD


# element_order honored end-to-end (the audit: order-1 declarations delivered tet10)

def _box_workspace(tmp_path, *, engine_params: dict | None, spec: dict) -> Path:
    gmsh = pytest.importorskip("gmsh")
    ws = tmp_path
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("box")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    if engine_params is not None:
        (ws / "engine_params.json").write_text(json.dumps(engine_params))
    (ws / "gmsh_spec.json").write_text(json.dumps(spec))
    return ws


def _msh_volume_element_types(msh_path: Path) -> set[int]:
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(msh_path))
        etypes, _, _ = gmsh.model.mesh.getElements(3)
        return {int(t) for t in etypes}
    finally:
        gmsh.finalize()


def test_declared_order_1_delivers_tet4(tmp_path):
    pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    ws = _box_workspace(tmp_path, engine_params={"element_order": "1"},
                        spec={"size": {"mode": "factor", "value": 0.4}})
    assert driver.main(str(ws)) == 0
    q = json.loads((ws / "quality.json").read_text())
    assert q["element_order"] == 1 and q["element_order_requested"] == 1
    assert q["element_order_source"] == "intake"
    assert not q["fatal"]
    assert _msh_volume_element_types(ws / "mesh.msh") == {4}          # tet4 ONLY
    # the FV metrics and the ladder history shipped with the quality report
    assert q["max_non_ortho"] > 0 and "max_skewness_internal" in q
    assert q["fv_optimization"] and q["fv_optimization"][0]["pass"] == "initial"


def test_declared_order_2_still_delivers_tet10(tmp_path):
    pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    ws = _box_workspace(tmp_path, engine_params={"element_order": "2"},
                        spec={"size": {"mode": "factor", "value": 0.4}})
    assert driver.main(str(ws)) == 0
    q = json.loads((ws / "quality.json").read_text())
    assert q["element_order"] == 2 and q["element_order_source"] == "intake"
    assert _msh_volume_element_types(ws / "mesh.msh") == {11}         # tet10
    assert not q["fatal"]


def test_spec_contradicting_the_declared_order_is_rejected(tmp_path, capsys):
    pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    ws = _box_workspace(tmp_path, engine_params={"element_order": "1"},
                        spec={"element_order": 2,
                              "size": {"mode": "factor", "value": 0.4}})
    assert driver.main(str(ws)) == 6
    err = capsys.readouterr().err
    assert "contradicts" in err and "element_order" in err
    assert not (ws / "mesh.msh").exists()                 # nothing was meshed


def test_without_a_declaration_the_spec_or_default_decides(tmp_path):
    pytest.importorskip("gmsh")
    from meshpipeline.engines.gmsh import driver
    ws = _box_workspace(tmp_path, engine_params=None,
                        spec={"element_order": 1,
                              "size": {"mode": "factor", "value": 0.4}})
    assert driver.main(str(ws)) == 0
    q = json.loads((ws / "quality.json").read_text())
    assert q["element_order"] == 1 and q["element_order_source"] == "spec"


def test_finalize_refuses_a_deck_whose_order_violates_the_declaration(tmp_path):
    from meshpipeline.engines.gmsh import gmsh_runner
    ws = tmp_path
    (ws / "mesh.inp").write_text("*HEADING\n")
    (ws / "mesh.msh").write_text("$MeshFormat\n")
    (ws / "quality.json").write_text(json.dumps({
        "cells": 10, "nodes": 20, "element_order": 2, "min_sicn": 0.5,
        "sicn_low_fraction": 0.0, "fatal": [], "size_h": 0.1,
        "bounds": [0, 0, 0, 1, 1, 1], "groups": {}, "default_group": "free",
        "default_group_used": False}))
    out = gmsh_runner.finalize(str(ws), [], "gmsh",
                               engine_params={"element_order": "1"})
    assert out["success"] is False
    assert "element order" in out["output"] and "declared" in out["output"]
    # matching declaration passes
    out2 = gmsh_runner.finalize(str(ws), [], "gmsh",
                                engine_params={"element_order": "2"})
    assert out2["success"] is True
