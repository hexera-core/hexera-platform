# Responsibility: Verify the FEA quality gate and the V1 result block: the element measures are right on elements of known shape, the FEA limits join the verdict only for a mesh built from an FEA plan, an inverted element is refused, and a meshed part comes back to the viewer face by face with its quality, plan, hot spots, units and download in words.
# Boundaries: the element checks are pure numpy; the result block is built on a small fixture part meshed in-process at a made-up level.
from __future__ import annotations

import json

import numpy as np
import pytest

from meshpipeline.contracts import decision_record as dr
from meshpipeline.engines import fea_quality as FQ
from meshpipeline.engines import verdict_limits as vl

_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))
#: a perfect tetrahedron, positively oriented, with straight edges (10 nodes, solver order)
_P = np.array([[1, -1, -1], [1, 1, 1], [-1, 1, -1], [-1, -1, 1]], dtype=float)
PERFECT = np.vstack([_P, [(_P[a] + _P[b]) / 2 for a, b in _EDGES]])


def _m(X):
    return {k: float(v[0]) for k, v in FQ.element_measures(X[None]).items()}


def test_a_perfect_element_reads_perfect():
    m = _m(PERFECT)
    assert m["min_detj"] > 0
    assert m["scaled_jacobian"] == pytest.approx(1.0)
    assert m["jac_ratio"] == pytest.approx(1.0)
    assert m["aspect"] == pytest.approx(1.0)
    assert m["min_dihedral"] == pytest.approx(70.5288, abs=1e-3)
    assert m["max_dihedral"] == pytest.approx(70.5288, abs=1e-3)


def test_a_bent_midside_node_lowers_the_jacobian_and_raises_the_ratio():
    bent = PERFECT.copy()
    bent[4] += 0.45 * (_P[2] - (_P[0] + _P[1]) / 2)
    m = _m(bent)
    assert 0 < m["scaled_jacobian"] < 1 and m["jac_ratio"] > 1.5


def test_an_inverted_element_is_counted_and_fails_the_gate():
    flipped = PERFECT[[1, 0, 2, 3, 4, 6, 5, 8, 7, 9]]
    assert _m(flipped)["min_detj"] < 0

    class Mesh:
        node_ids = np.arange(1, 11)
        xyz = flipped
        elements = np.arange(1, 11)[None]

    out = FQ.measure(Mesh())
    assert out["fea_inverted"] == 1


def test_a_flat_sliver_misses_the_shape_limits():
    sliver = PERFECT.copy()
    sliver[3] = sliver[:3].mean(axis=0) + np.array([0.0, 0.0, 0.02])
    for k, (a, b) in enumerate(_EDGES):
        sliver[4 + k] = (sliver[a] + sliver[b]) / 2
    m = _m(sliver)
    assert m["aspect"] > 20 or m["min_dihedral"] < 5 or m["max_dihedral"] > 170


def test_the_fea_limits_join_the_verdict_only_for_an_fea_mesh():
    fea_q = {"fea": {"plan": True}, "fea_min_scaled_jacobian": 0.15, "fea_max_jacobian_ratio": 1.3,
             "fea_max_aspect_ratio": 4.0, "fea_min_dihedral_deg": 12.0, "fea_max_dihedral_deg": 150.0,
             "fea_rules_short": 0}
    inputs = vl.inputs_for(engine="gmsh", topology="", manifest={"quality": fea_q}, valid=True)
    keys = {r["key"] for r in inputs["limits"]}
    assert {k for k, *_ in FQ.LIMITS} | {"fea_rules_short"} <= keys
    assert dr.verdict(inputs) == (dr.OUTCOME_CONCERNS, dr.VER_CONCERNS)
    assert [r["key"] for r in dr.missed_limits(inputs)] == ["fea_min_scaled_jacobian"]
    fea_q["fea_min_scaled_jacobian"] = 0.3
    assert dr.verdict(vl.inputs_for(engine="gmsh", topology="", manifest={"quality": fea_q}, valid=True)) == (
        dr.OUTCOME_CLEAN, dr.VER_CLEAN)
    # a CFD gmsh mesh is judged exactly as before
    cfd = vl.inputs_for(engine="gmsh", topology="internal", manifest={"quality": {"max_non_ortho": 60}}, valid=True)
    assert not any(r["key"].startswith("fea_") for r in cfd["limits"])


# #
# the result block, on a meshed part
# #

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    from tests import fea_fixtures as fx

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import fea_plan as P
    from meshpipeline.engines.gmsh import fea_adapter

    ws = tmp_path_factory.mktemp("fea_result")
    src = fx.write_step(fx.plate_with_hole(), ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    record, surface, diag = FD.measure_staged(ws)
    FD.write_plan(ws, FD.make_plan(record, surface, diag, fidelity="", level=P.Level("draft", "Quick", 40_000)))
    assert fea_adapter.mesh(ws) == 0
    return ws


def test_the_mesh_is_measured_the_way_the_solvers_judge_it(plate):
    q = json.loads((plate / "quality.json").read_text())
    assert q["fea_inverted"] == 0
    assert 0.2 <= q["fea_min_scaled_jacobian"] <= 1.0
    assert 1.0 <= q["fea_max_jacobian_ratio"] <= 30
    assert 1.0 <= q["fea_max_aspect_ratio"] <= 20
    assert 5 <= q["fea_min_dihedral_deg"] <= q["fea_max_dihedral_deg"] <= 170
    assert q["fea"]["quality"]["elements"] == q["cells"]


def test_the_viewer_gets_every_face_and_the_fea_block(plate):
    from meshpipeline.engines.registry import get_spec

    resp = get_spec("gmsh").viewer_surface(plate, roles={"free": "free"}, units="m")
    names = [p["name"] for p in resp["patches"]]
    plan = json.loads((plate / "fea_plan.json").read_text())
    assert names == [f"Face_{k}" for k in range(1, plan["measure"]["faces"] + 1)]
    f = resp["fea"]
    assert f["check"]["outcome"] == "pass"
    assert {r["key"] for r in f["check"]["rows"]} >= {"fea_inverted", "through_wall_min", "fea_min_scaled_jacobian",
                                                     "fea_max_aspect_ratio"}
    assert f["through_wall"]["ok"] and f["through_wall"]["words"].startswith("Yes")
    assert f["units"]["mesh"] == "m" and "metres" in f["units"]["words"]
    assert f["plan_words"] == plan["words"]
    hole = next(h for h in f["hot_spots"] if h["type"] == "hole")
    assert hole["words"] == "hole, radius 7.5 mm" and len(hole["at_m"]) == 3
    assert [x["name"] for x in f["faces"]] == names
    assert [c["file"] for c in f["download"]["contains"]][0] == "mesh.inp"


def test_a_cfd_gmsh_workspace_gets_no_fea_block(tmp_path):
    from meshpipeline.engines.fea_results import results

    (tmp_path / "quality.json").write_text(json.dumps({"cells": 10}))
    assert results(tmp_path) is None
