"""CFDREQ-27 (rulebook 87): a snappyHexMesh wall-function layer under a jump over more of the wall than the y+ row can
leave out is refused with no lever on that engine - the ladder's next engine builds the part, the snappy mesh is the
deliverable fallback with its number said."""
from __future__ import annotations

import json

from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines.snappy import flow_gates as FG
from meshpipeline.pipeline import engine_fallback as EF

FIRST = 0.0019241


def _plan(ws, treatment="functions"):
    p = ws / CR.PLAN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"engine": "snappy", "rows": {"c1": {"field": "layers:y_plus", "treatment": treatment,
                                                                 "first_m": FIRST, "layers": 1}}}))


def _q(jump):
    # the quality record's census at the planned height (built_first_cells reads it as measured)
    return {"layer_coverage_basis": "height", "layer_first_planned_m": FIRST, "layer_area_at_plan_pct": 99.0,
            "layer_first_cell_m": FIRST, "layer_area_under_jump_pct": jump, "layer_next_over_first": 0.30}


def test_switch_bar_is_what_the_y_plus_row_can_leave_out():
    assert CR.WALL_FUNCTION_JUMP_SWITCH_PCT == 100.0 - CR.Y_PLUS_WALL_SHARE == 20.0


def test_a_wall_function_layer_under_a_jump_on_most_of_the_wall_is_found(tmp_path):
    _plan(tmp_path)
    got = CR.wall_function_jump(tmp_path, _q(71.1))
    assert got == {"under_jump_pct": 71.1, "next_over_first": 0.30, "first_m": FIRST}


def test_within_the_row_resolved_or_no_plan_is_not_judged(tmp_path):
    assert CR.wall_function_jump(tmp_path, _q(71.1)) is None           # no plan
    _plan(tmp_path)
    assert CR.wall_function_jump(tmp_path, _q(8.9)) is None            # 00154423's share: the row can take it
    assert CR.wall_function_jump(tmp_path, _q(20.0)) is None
    assert CR.wall_function_jump(tmp_path, {}) is None                 # no mesh to measure
    _plan(tmp_path, treatment="resolved")
    assert CR.wall_function_jump(tmp_path, _q(71.1)) is None


class _Ctx:
    def __init__(self, ws, q):
        self.workspace, self._q = ws, q

    def manifest_or_load(self):
        return {"quality": self._q}


def test_snappy_gate_refuses_with_its_number_and_passes_otherwise(tmp_path):
    _plan(tmp_path)
    ok, why = FG._gate_wall_function_layer(_Ctx(tmp_path, _q(71.1)))
    assert not ok and "71.1% of the wall" in why and "1.92 mm" in why
    assert getattr(why, "facts", {}).get("under_jump_pct") == 71.1
    assert FG._gate_wall_function_layer(_Ctx(tmp_path, _q(8.9))) == (True, "")
    keys = [g.key for g in FG.FLOW_GATES]
    assert keys.index("wall_function_layer") < keys.index("cad_fidelity")


def test_cfmesh_answers_the_same_gate_and_never_refuses_the_layer_it_is_sent_to_build(tmp_path):
    from meshpipeline.engines.cfmesh import flow_gates as CG
    _plan(tmp_path)
    # the same jump snappy is refused for: cfMesh splits its wall cell, so it is where the ladder sends this layer
    assert CG._gate_wall_function_layer(_Ctx(tmp_path, _q(71.1))) == (True, "")
    keys = [g.key for g in CG.FLOW_GATES]
    assert keys == [g.key for g in FG.FLOW_GATES]


def test_no_lever_on_snappy_so_the_ladder_moves_and_the_mesh_stays_deliverable():
    assert not EF.lever_bears("wall_function_layer", "snappy")
    assert EF.refusal_without_lever({"executor_success": False, "executor_failed_gate": "wall_function_layer",
                                     "engine": "snappy"})
    assert "wall_function_layer" in EF.DELIVERABLE_GATES
    c = EF.deliverable_refusal("wall_function_layer", {}, {"under_jump_pct": 71.1})
    assert c is not None and c["gate"] == "wall_function_layer" and "71% of the wall" in c["words"]
    assert EF.deliverable_refusal("wall_function_layer", {}, {}) is None
