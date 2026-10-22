# Responsibility: Verify the curved FEA mesh is settled as the rules say: a CAD cusp is found where two edges of a face leave a vertex together (and nowhere else), the plan carries it and cites PLAN-fea-10, curving never leaves an element worse than straight (PLAN-fea-11), the elements touching a cusp get straight sides and are set aside from the floor - every other element still held to it - and the delivery names each cusp as a concern, never as a pass or a failure.
# Boundaries: parts are built in the test (tests/fea_fixtures.py) and meshed in-process with gmsh at a small made-up level; the real cases (Areen's bearing bracket, the step-8 nuts and screws) are the lab's (~/bkt_lab).
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad.fea_measure import cad_cusps, measure_cad  # noqa: E402
from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402
from meshpipeline.engines.gmsh import fea_adapter, fea_settle  # noqa: E402

SMALL = P.Level("draft", "Quick", 8_000)


def test_a_boss_tangent_to_an_edge_is_a_cusp_and_ordinary_parts_have_none():
    found = cad_cusps(fx.tangent_boss(), 1.0)
    assert sorted(tuple(c["at_mm"]) for c in found) == sorted(fx.TANGENT_BOSS_CUSPS)
    assert all(c["angle_deg"] < 1.0 for c in found)
    for build in (fx.plate_with_hole, fx.bracket, fx.stepped_shaft, fx.audit_l_bracket, fx.audit_plate_with_hole):
        assert cad_cusps(build(), 1.0) == [], build.__name__
    # the measurement record carries them, and the hot-spot counts do not
    rec = measure_cad(fx.tangent_boss(), mm_per_unit=1.0)
    assert len(rec["cusps"]) == 1 and "cusp" not in rec["counts"]


def _stage(shape, ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
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
    return ws


def test_the_plan_carries_the_cusps_in_metres_and_cites_the_rules(tmp_path):
    ws = _stage(fx.tangent_boss(), tmp_path / "bore")
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.make_plan(record, surface, diag, fidelity="", level=SMALL)
    assert sorted(tuple(round(v, 6) for v in c["at_m"]) for c in plan["cusps"]) == sorted(
        tuple(round(v / 1000.0, 6) for v in p) for p in fx.TANGENT_BOSS_CUSPS)
    rules = {d["rule"] for d in plan["decisions"]}
    assert {"PLAN-fea-10", "PLAN-fea-11"} <= rules
    assert any(w.startswith("Cusps: 1 place(s)") for w in plan["words"])


def _curved_mesh(ws: Path, plan: dict):
    """The adapter's mesh of the staged part, before order 2: (straight minSICN by tag)."""
    gmsh.initialize(interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setNumber("General.NumThreads", 1)
    gmsh.option.setNumber("Mesh.RandomSeed", 1)
    gmsh.model.add("t")
    gmsh.model.occ.importShapes(str(ws / "geometry.step"))
    gmsh.model.occ.synchronize()
    gmsh.option.setNumber("Mesh.MeshSizeMax", float(plan["base_size_m"]))
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
    surfaces = {t for _, t in gmsh.model.getEntities(2)}
    bg = fea_adapter.size_fields(gmsh, plan, surfaces)
    if bg is not None:
        gmsh.model.mesh.field.setAsBackgroundMesh(bg)
    gmsh.model.mesh.generate(3)
    t, q = fea_settle.qualities(gmsh)
    straight = dict(zip(t.tolist(), q.tolist(), strict=True))
    gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)
    gmsh.model.mesh.setOrder(2)
    return straight


def test_settling_straightens_what_curving_spoiled_and_sets_aside_only_cusp_elements(tmp_path):
    ws = _stage(fx.tangent_boss(), tmp_path / "settle")
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.make_plan(record, surface, diag, fidelity="", level=SMALL)
    try:
        straight = _curved_mesh(ws, plan)
        # a floor far above any real one, so that the rules have elements to act on
        floor = 0.4
        out = fea_settle.settle(gmsh, straight, [c["at_m"] for c in plan["cusps"]], floor)
        tags, q = fea_settle.qualities(gmsh)
        now = dict(zip(tags.tolist(), q.tolist(), strict=True))
        # PLAN-fea-11: no element is left under the floor AND worse than its straight shape
        assert out["straightened"] > 0
        assert not [t for t, v in now.items() if v < floor and v < straight[t] - 1e-9 and t not in out["set_aside"]]
        # PLAN-fea-10: only elements by a cusp are set aside, each with straight sides
        cusps = np.array([c["at_m"] for c in plan["cusps"]])
        assert out["set_aside"] and len(out["cusp_places_m"]) == 1
        for t in out["set_aside"]:
            _typ, nodes, _d, _e = gmsh.model.mesh.getElement(t)
            c = np.array([gmsh.model.mesh.getNode(n)[0] for n in nodes[:4]])
            h = max(np.linalg.norm(c[i] - c[j]) for i in range(4) for j in range(i + 1, 4))
            assert np.linalg.norm(cusps[:, None, :] - c[None], axis=2).min() < fea_settle.CUSP_REACH * h
            mids = {tuple(np.round(0.5 * (c[i] + c[j]), 12)) for i in range(4) for j in range(i + 1, 4)}
            for n in nodes[4:10]:
                assert tuple(np.round(gmsh.model.mesh.getNode(n)[0], 12)) in mids
            assert now[t] > 0.0                  # straight sides: always valid
    finally:
        gmsh.finalize()


def test_a_cusp_is_delivered_as_a_concern_that_names_the_place():
    from meshpipeline.engines.fea_results import results
    from meshpipeline.engines.verdict_limits import fea_limits

    q = {"fea": {"plan": True}, "fea_set_aside": {"rule": "PLAN-fea-10", "elements": 16, "min_sicn": 0.066,
                                                  "places_mm": [[-95.09, -668.86, 431.07]]}}
    row = next(r for r in fea_limits(q) if r["key"] == "fea_cusps")
    assert row["measured"] == ["(-95.09, -668.86, 431.07) mm"]
    assert "the material runs to nothing here, so stress at this point is not meaningful; round it off in CAD " \
           "for a clean mesh" in row["label"]
    assert row["source"].startswith("docs/RULES.md PLAN-fea-10")
    assert results is not None


def test_the_adapter_reports_the_floor_without_the_set_aside_elements(tmp_path):
    ws = _stage(fx.tangent_boss(), tmp_path / "adapter")
    record, surface, diag = FD.measure_staged(ws)
    FD.write_plan(ws, FD.make_plan(record, surface, diag, fidelity="", level=SMALL))
    assert fea_adapter.mesh(ws) == 0
    q = json.loads((ws / "quality.json").read_text())
    aside = q["fea_set_aside"]
    assert aside["rule"] == "PLAN-fea-10/PLAN-fea-12" and isinstance(aside["elements"], int)
    assert "fea_straightened" in q
    # validity holds every element; the floor every element not set aside
    assert not q["fatal"] and q["fea_inverted"] == 0
    assert q["elements_under_floor"] == 0 and q["min_sicn"] >= 0.1


# ------------------------------------------------------------------ micro details ----
def test_a_micro_step_is_found_and_ordinary_parts_have_none():
    from meshpipeline.cad.fea_measure import cad_micro

    found = cad_micro(fx.micro_step(), 1.0, diag_mm=25.0)
    kinds = sorted(m["kind"] for m in found)
    assert "face" in kinds and kinds.count("edge") >= 2
    assert all(m["size_mm"] < 0.01 and m["points_mm"] for m in found)
    for build in (fx.plate_with_hole, fx.bracket, fx.tangent_boss):
        assert cad_micro(build(), 1.0, diag_mm=60.0) == [], build.__name__


def test_the_plan_keeps_micro_details_far_below_its_elements_and_the_adapter_sets_them_aside(tmp_path):
    ws = _stage(fx.micro_step(), tmp_path / "micro")
    record, surface, diag = FD.measure_staged(ws)
    plan = FD.make_plan(record, surface, diag, fidelity="", level=SMALL)
    assert plan["micro"] and all(m["points_m"] for m in plan["micro"])
    assert "PLAN-fea-12" in {d["rule"] for d in plan["decisions"]}
    assert any("contact stress at the tooth tips" in w for w in plan["words"])
    FD.write_plan(ws, plan)
    assert fea_adapter.mesh(ws) == 0
    q = json.loads((ws / "quality.json").read_text())
    aside = q["fea_set_aside"]
    assert not q["fatal"] and q["fea_inverted"] == 0
    assert q["elements_under_floor"] == 0
    if aside["micro_elements"]:
        assert aside["micro"] and all(m["size_mm"] < 0.01 for m in aside["micro"])


def test_a_micro_detail_is_delivered_as_a_concern_that_says_what_is_not_resolved():
    from meshpipeline.engines.verdict_limits import fea_limits

    q = {"fea": {"plan": True}, "fea_set_aside": {"elements": 1181, "cusp_elements": 0, "micro_elements": 1181,
                                                  "places_mm": [], "micro": [
                                                      {"at_mm": [1.0, 2.0, 3.0], "kind": "face", "size_mm": 0.00106}]}}
    rows = {r["key"]: r for r in fea_limits(q)}
    assert "fea_cusps" not in rows
    row = rows["fea_micro_details"]
    assert row["measured"] == ["(1, 2, 3) mm, a face 0.00106 mm across"]
    assert "the contact stress at the tooth tips is not resolved" in row["label"]
    assert row["source"].startswith("docs/RULES.md PLAN-fea-12")
