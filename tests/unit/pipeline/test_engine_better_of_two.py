"""CFDREQ-28, amended: a run the layer-stack gate moved to a second engine ends on the BETTER of its two meshes by the
product's own measured verdict (pass beats concerns; of two with concerns, fewer missed rows, then the worst row
missed by less; a tie keeps the first, cheaper mesh), the mesh not chosen is said in one line, and a kept mesh is
always handed back when the next engine runs out of time (abc 00025560, CFD board batch 8 external: cfMesh ran into
the wall clock and the kept snappyHexMesh mesh was lost)."""
from __future__ import annotations

import json
import time

from meshpipeline.contracts import native_time as NT
from meshpipeline.engines import cfd_requirements as CR
from meshpipeline.engines import resolution_policy as RP
from meshpipeline.engines import verdict_limits as VL
from meshpipeline.pipeline import engine_fallback as EF
from meshpipeline.pipeline import graph as G


def _mesh(ws, engine, **q):
    ws.mkdir(parents=True, exist_ok=True)
    quality = {"max_non_ortho": 66.0, "skew_fraction": 0.0, "regions": 1, "layers_requested": 2,
               "layers_in_meshdict": 2, **q}
    (ws / "mesh_manifest.json").write_text(json.dumps({"mesh_mode": engine, "quality": quality}))
    return str(ws)


def _stack_concern(cover):
    return EF.deliverable_refusal("layer_stack", {}, {"coverage_pct": cover, "layers": 2})


def _state(tmp_path, *, snappy_cover=79.2, cfmesh_q=None, snappy_refused=True):
    s_ws = _mesh(tmp_path / "a1", "snappy", layer_coverage_pct=snappy_cover)
    c_ws = _mesh(tmp_path / "a2", "cfmesh", **(cfmesh_q or {"layer_coverage_pct": 99.9, "max_non_ortho": 75.07}))
    snappy = {"attempt": 1, "workspace": s_ws, "engine": "snappy", "engine_params": {},
              "requirement_caveats": [_stack_concern(snappy_cover)] if snappy_refused else []}
    if snappy_refused:
        snappy.update(refused="layer_stack", refused_distance=max(0.0, 80.0 - snappy_cover))
    cfmesh = {"attempt": 2, "workspace": c_ws, "engine": "cfmesh", "engine_params": {}, "requirement_caveats": []}
    return {"job_id": "t", "purpose": "external_cfd", "engine": "cfmesh", "retry_count": 2,
            "executor_success": True, "requirement_caveats": [], "openfoam_workspace": c_ws,
            RP.SOUND_KEY: [snappy, cfmesh]}


def test_the_gate_and_the_verdict_hold_one_bar():
    bar = next(lim.limit for lim in VL.PRODUCTION_LIMITS if lim.key == "layer_coverage_pct")
    assert CR.STACK_KEPT_PCT == bar


def test_a_layer_stack_refusal_is_the_layer_row_not_a_second_missed_requirement():
    from meshpipeline.application.final_result import _near_misses
    concern = _stack_concern(79.2)
    assert RP.near_misses([concern]) == 0 and _near_misses([concern]) == 0
    assert RP.near_misses([{"kind": "requirement", "gate": ""}, concern]) == 1


def test_a_concerns_mesh_missing_by_less_is_kept_over_the_other_engines(tmp_path):
    # batch 8, abc 00045950: snappy's stack on 79.2% (one row, 1% short) against cfMesh's worst face 75 degrees
    # over the 70 limit (one row, 7% over): snappy's mesh is the better by the verdict and is delivered
    state = _state(tmp_path)
    best = RP.deliverable_sound(state)
    assert best["engine"] == "snappy"
    choice = RP.engine_choice(state)
    assert choice["best"]["engine"] == "snappy" and choice["rule"] == RP.ENGINE_BETTER_OF_TWO
    assert "cfMesh mesh of attempt 2 was not chosen" in choice["line"] and "75.1 against 70" in choice["line"]


def test_a_passing_mesh_beats_a_concerns_one_whichever_engine(tmp_path):
    state = _state(tmp_path, cfmesh_q={"layer_coverage_pct": 99.9, "max_non_ortho": 60.0})
    assert RP.deliverable_sound(state)["engine"] == "cfmesh"
    assert "snappyHexMesh mesh of attempt 1 was not chosen" in RP.engine_choice(state)["line"]


def test_fewer_missed_rows_wins_and_a_tie_keeps_the_first_mesh(tmp_path):
    # cfMesh misses two rows (worst face and its layers short) against snappy's one
    two = _state(tmp_path / "x", cfmesh_q={"layer_coverage_pct": 99.9, "max_non_ortho": 71.0, "layers_in_meshdict": 1})
    assert RP.deliverable_sound(two)["engine"] == "snappy"
    # the same miss on each (snappy 79.5% cover; cfMesh 79.5% cover): the first, cheaper mesh is kept
    tie = _state(tmp_path / "y", snappy_cover=79.5,
                 cfmesh_q={"layer_coverage_pct": 79.5, "max_non_ortho": 60.0})
    assert RP.deliverable_sound(tie)["engine"] == "snappy"
    assert "measures the same and was built first" in RP.engine_choice(tie)["line"]


def test_one_engines_meshes_keep_the_order_they_had(tmp_path):
    state = _state(tmp_path)
    for row in state[RP.SOUND_KEY]:
        row["engine"] = "snappy"
    assert RP.engine_choice(state) is None
    assert RP.deliverable_sound(state)["attempt"] == 2     # unrefused beats refused, as before (LAD-cfd-8)


def test_a_sound_mesh_from_the_second_engine_goes_to_the_choice(tmp_path):
    state = _state(tmp_path)
    assert G.route_after_executor(state) == "node_deliver_sound"


def test_a_guard_trip_with_a_kept_mesh_hands_it_back(tmp_path):
    from meshpipeline.contracts.failure_cause import FailureCause
    state = _state(tmp_path)
    state[RP.SOUND_KEY] = state[RP.SOUND_KEY][:1]
    state.update(executor_success=False, executor_failure_cause=FailureCause.ENGINE_TIMED_OUT.value,
                 executor_failure_facts={"guard": {"where": "cfmesh", "native_run": True}})
    assert G.route_after_executor(state) == "node_deliver_sound"


def test_a_held_run_ends_by_the_hand_back_time_and_is_not_moved_off_the_wall(tmp_path):
    assert NT.wall_for(tmp_path) == NT.CLOUD_RUN_WALL_S
    NT.hold_for_handback(tmp_path, time.time() + 3000)
    assert 2990 <= NT.wall_for(tmp_path) <= 3000 and NT.held_short_of_the_wall(tmp_path)
    caps = []

    def once(cap):
        caps.append(cap)
        return {"timed_out": True, "rc": 124}
    assert NT.run_off_the_wall(tmp_path, once, engine="cfmesh")["timed_out"] and len(caps) == 1
    NT.hold_for_handback(tmp_path, None)
    assert NT.handback_cap(tmp_path) is None and NT.wall_for(tmp_path) == NT.CLOUD_RUN_WALL_S


def test_the_builder_holds_an_attempt_that_follows_a_kept_mesh(tmp_path):
    from meshpipeline.agents.builder.agent import _hold_for_handback
    state = _state(tmp_path)
    state["pipeline_deadline_epoch"] = time.time() + 10_000
    ws = tmp_path / "a3"
    ws.mkdir()
    _hold_for_handback(ws, state)
    assert NT.handback_cap(ws) <= 10_000 - NT.HANDBACK_RESERVE_S
    state[RP.SOUND_KEY] = []
    _hold_for_handback(ws, state)
    assert NT.handback_cap(ws) is None
