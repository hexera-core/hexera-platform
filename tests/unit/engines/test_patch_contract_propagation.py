# Responsibility: Verify the approved patches reach the gate from the job request, and no stale session field does.
from __future__ import annotations

import json

from meshpipeline.application.pipeline_run import JobRequest
from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.registry import get_spec
from meshpipeline.pipeline.state_factory import make_pipeline_state

_APPROVED_PATCHES = [{"name": "wing", "type": "wall"}, {"name": "fuselage", "type": "wall"},
                     {"name": "farfield", "type": "farfield"}]


def _ctx_from_state(state, tmp_path, patch_types):
    (tmp_path / "mesh_manifest.json").write_text(json.dumps({
        "schema_version": "2.1", "patch_types": patch_types,
        "patches": {n: [] for n in patch_types}, "quality": {"cells": 1, "fatal": []}}))
    # exactly how executor.py builds the gate context
    return GateCtx(workspace=tmp_path, engine=state.get("engine", ""),
                   domain=state.get("domain", ""),
                   intake_patches=state.get("intake_patches", []) or [],
                   engine_params=state.get("engine_params", {}) or {})


def test_approved_patches_flow_jobrequest_to_state_to_gate(tmp_path):
    req = JobRequest(job_id="j", mesh_engine="cfmesh", intake_patches=list(_APPROVED_PATCHES),
                     approved_snapshot_id="snap-9")
    state = make_pipeline_state(job_id="j", user_id="u", engine=req.mesh_engine,
                                intake_patches=req.intake_patches)
    assert state["intake_patches"] == _APPROVED_PATCHES

    sp = get_spec("cfmesh")
    gate = next(g for g in sp.gates if g.key == sp.user_contract_gate_key)
    # exact delivery of the approved set → pass
    ctx = _ctx_from_state(state, tmp_path,
                          {"wing": "wall", "fuselage": "wall", "farfield": "farfield"})
    assert gate.check(ctx)[0] is True
    # a merge of the approved set → fail (the gate is judging the APPROVED patches)
    ctx = _ctx_from_state(state, tmp_path, {"body": "wall", "farfield": "farfield"})
    assert gate.check(ctx)[0] is False


def test_the_gate_does_not_read_regions_instead_of_intake_patches(tmp_path):
    req = JobRequest(job_id="j", mesh_engine="snappy_multiregion",
                     intake_patches=list(_APPROVED_PATCHES),
                     engine_params={"_regions": [{"name": "air", "type": "fluid"}]})
    state = make_pipeline_state(job_id="j", user_id="u", engine=req.mesh_engine,
                                intake_patches=req.intake_patches,
                                engine_params=req.engine_params)
    sp = get_spec("snappy_multiregion")
    gate = next(g for g in sp.gates if g.key == "patch_contract")
    # delivered user boundaries are WRONG (walls merged) even though _regions is fine
    ctx = _ctx_from_state(state, tmp_path, {"body": "wall", "farfield": "farfield"})
    ok, diag = gate.check(ctx)
    assert ok is False and "CONTRACT" in diag
    # the gate never consulted engine_params['_regions'] to pass itself
    assert "air" not in diag


def test_stale_session_style_fields_do_not_reach_the_gate(tmp_path):
    state = make_pipeline_state(job_id="j", user_id="u", engine="cfmesh",
                                intake_patches=list(_APPROVED_PATCHES))
    sp = get_spec("cfmesh")
    gate = next(g for g in sp.gates if g.key == sp.user_contract_gate_key)
    ctx = _ctx_from_state(state, tmp_path, {"stale_wall": "wall"})
    assert gate.check(ctx)[0] is False
