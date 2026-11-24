"""CFDREQ-28 (rulebook 94): an external body's planned layer stack kept on under the layer row's share of the wall is
refused on snappyHexMesh with no lever there - the ladder's next engine (cfMesh, which now refines the external wake
as snappy does, so it is the same mesh) builds it; the snappy mesh is the deliverable fallback, and of two refused
meshes the one whose wall is nearer the bar is delivered."""
from __future__ import annotations

import json

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines.snappy import flow_gates as FG
from meshpipeline.pipeline import engine_fallback as EF


def _ws(ws, *, flow="external", layers=10, treatment="resolved"):
    p = ws / CR.PLAN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"engine": "snappy", "rows": {"c1": {"field": "layers:y_plus", "treatment": treatment,
                                                                 "first_m": 9.3e-6, "layers": layers,
                                                                 "bl_delta_m": 0.0022}}}))
    (ws / "flow_topology").write_text(flow)
    return ws


class _Ctx:
    def __init__(self, ws, q):
        self.workspace, self._q = ws, q

    def manifest_or_load(self):
        return {"quality": self._q}


def test_the_bar_is_the_layer_rows_own():
    assert CR.STACK_KEPT_PCT == 80.0
    assert CR._layers_row({"layers": {"count": 10}}, {"layer_coverage_pct": 79.9}, "")["met"] is False


def test_a_stack_kept_on_too_little_of_an_external_wall_is_found(tmp_path):
    _ws(tmp_path)
    # abc 00000645_001 (batch 7): 10 laminar layers planned, kept on 61.8% of the body
    got = CR.stack_lost(tmp_path, {"layer_coverage_pct": 61.8116})
    assert got == {"coverage_pct": 61.8, "layers": 10, "treatment": "resolved", "first_m": 9.3e-6}
    # wall functions too: abc 00015899_000 kept its 6-layer stack on 63.1%
    _ws(tmp_path, layers=6, treatment="functions")
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": 63.1})["treatment"] == "functions"


def test_kept_internal_unmeasured_or_no_stack_is_not_judged(tmp_path):
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": 10.0}) is None          # no plan
    _ws(tmp_path)
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": 80.0}) is None          # at the bar
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": 97.5}) is None
    assert CR.stack_lost(tmp_path, {}) is None                                    # not measured
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": True}) is None
    _ws(tmp_path, layers=0)
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": 10.0}) is None          # no stack planned
    _ws(tmp_path, flow="internal")
    assert CR.stack_lost(tmp_path, {"layer_coverage_pct": 10.0}) is None          # internal: CFDREQ-27's


def test_snappy_gate_refuses_with_its_number_before_the_cad_check(tmp_path):
    _ws(tmp_path)
    ok, why = FG._gate_layer_stack(_Ctx(tmp_path, {"layer_coverage_pct": 61.8116}))
    assert not ok and "61.8% of the wall" in why and "10-layer" in why
    assert getattr(why, "facts", {}).get("coverage_pct") == 61.8
    assert FG._gate_layer_stack(_Ctx(tmp_path, {"layer_coverage_pct": 92.0})) == (True, "")
    keys = [g.key for g in FG.FLOW_GATES]
    assert keys.index("layer_stack") < keys.index("cad_fidelity")


def test_cfmesh_judges_the_same_gate_in_the_same_place(tmp_path):
    from meshpipeline.engines.cfmesh import flow_gates as CG
    _ws(tmp_path)
    ok, why = CG._gate_layer_stack(_Ctx(tmp_path, {"layer_coverage_pct": 41.0}))
    assert not ok and "41.0% of the wall" in why
    assert CG._gate_layer_stack(_Ctx(tmp_path, {"layer_coverage_pct": 99.3})) == (True, "")
    assert [g.key for g in CG.FLOW_GATES] == [g.key for g in FG.FLOW_GATES]


def test_no_lever_on_either_engine_the_mesh_stays_deliverable_and_the_nearer_wall_wins():
    assert not EF.lever_bears("layer_stack", "snappy") and not EF.lever_bears("layer_stack", "cfmesh")
    assert EF.refusal_without_lever({"executor_success": False, "executor_failed_gate": "layer_stack",
                                     "engine": "snappy"})
    assert "layer_stack" in EF.DELIVERABLE_GATES
    c = EF.deliverable_refusal("layer_stack", {}, {"coverage_pct": 61.8, "layers": 10})
    assert c is not None and c["gate"] == "layer_stack" and "62% of the wall" in c["words"] and "10-layer" in c["words"]
    assert EF.deliverable_refusal("layer_stack", {}, {}) is None
    near = EF.gate_distance("layer_stack", {}, {"coverage_pct": 77.0})
    far = EF.gate_distance("layer_stack", {}, {"coverage_pct": 41.0})
    assert near == 3.0 and far == 39.0
    assert EF.gate_distance("layer_stack", {}, {}) is None


def test_cfmesh_is_the_same_mesh_as_an_external_snappy_plan_now_that_it_refines_the_wake():
    from meshpipeline.engines.registry import get_spec
    snappy, cfmesh = get_spec("snappy"), get_spec("cfmesh")
    assert cfmesh.delivered_mesh.wake_refinement
    reqs = EF.plan_requirements({"purpose": "external_cfd", "engine": "snappy"}, snappy)
    assert EF.REQ_WAKE in reqs
    assert EF.uncovered(cfmesh, reqs) == ()
    assert EF._plan_changes(cfmesh, EF.layer_request("", ""), reqs) == []


def test_cfmesh_external_case_writes_the_shared_wake_boxes(tmp_path):
    from meshpipeline.engines.cfmesh import cfmesh_runner as CM
    from meshpipeline.engines.wake_region import WakeRequest
    body = ([0.0, 0.0, 0.0], [0.1, 0.04, 0.03])
    dmin, dmax = [-0.5, -0.4, -0.4], [1.1, 0.44, 0.43]
    out = CM.render_cfmesh_case(tmp_path, surface_file="constant/triSurface/case.fms", wall_patch="body",
                                patches=[{"name": "body", "type": "wall"}, {"name": "farfield", "type": "farfield"}],
                                body_bbox=body, L=0.1, domain_min=dmin, domain_max=dmax,
                                strategy={"wall_cell": 0.002}, cell_budget=4_000_000, wall_area=0.02,
                                wake=WakeRequest(flow_axis="+x"))
    rec = out.get("wake_refinement")
    assert rec and rec["enabled"] and rec["tiers"]
    md = (tmp_path / "system" / "meshDict").read_text()
    assert "objectRefinements" in md and "wake0" in md
    assert json.loads((tmp_path / "wake_refinement.json").read_text())["enabled"] is True
    # no wake asked (an internal case): nothing written
    out2 = CM.render_cfmesh_case(tmp_path / "int", surface_file="constant/triSurface/case.fms", wall_patch="body",
                                 patches=[{"name": "body", "type": "wall"}], body_bbox=body, L=0.1,
                                 domain_min=dmin, domain_max=dmax, strategy={"wall_cell": 0.002},
                                 cell_budget=4_000_000, wall_area=0.02)
    assert "wake_refinement" not in out2
