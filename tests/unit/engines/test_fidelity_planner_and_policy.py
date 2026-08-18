# Responsibility: Verify the artifact policy is the single source of requiredness, and the planner carries the tier.
from __future__ import annotations

import json

import pytest
from tests._geometry_support import prepared_surface

import meshpipeline.settings.policy as polcfg


# one artifact-policy authority, used everywhere
def test_artifact_policy_is_the_single_source():
    from meshpipeline.agents.intake import admission_token as at
    from meshpipeline.application import artifact_policy as ap
    # the approved-intent derivation delegates to the policy
    assert at.required_output_classes("gmsh") == ap.required_output_classes("gmsh")
    # the terminal readiness predicate and the uploader classification share it
    assert ap.required_ready("gmsh", ["mesh_bundle", "viewer_data"]) is True
    assert ap.required_ready("gmsh", ["mesh"]) is False
    assert ap.is_required_class("gmsh", "mesh_bundle") is True
    assert ap.is_required_class("gmsh", "mesh") is False


def test_optional_export_is_never_promoted_to_required():
    from meshpipeline.application import artifact_policy as ap
    assert "mesh" in ap.OPTIONAL_CLASSES
    assert ap.required_output_classes("gmsh") == ["mesh_bundle", "viewer_data"]   # the optional class is not in it
    assert ap.optional_warnings(["mesh_bundle"]) == ["mesh"]       # absent optional → warning only


def test_unset_engine_has_no_policy_and_is_not_ready():
    from meshpipeline.application import artifact_policy as ap
    assert ap.required_output_classes("") == []
    assert ap.required_ready("", ["mesh_bundle"]) is False
    assert ap.required_ready(None, ["mesh_bundle"]) is False


def test_no_stray_mesh_bundle_literal_defines_requiredness():
    from pathlib import Path
    src = Path(__file__).parents[3] / "src" / "meshpipeline"
    up = (src / "application" / "artifact_uploader.py").read_text()
    assert "is_required_class" in up            # requiredness comes from the policy
    assert "required=True" not in up            # never hardcoded
    # The readiness policy moved with terminal assembly: terminal_finalize now derives it, and
    # pipeline_run must not re-derive it. Both halves of the original guard are kept.
    tf = (src / "application" / "terminal_finalize.py").read_text()
    pr = (src / "application" / "pipeline_run.py").read_text()
    assert "required_ready" in tf and "in _delivered_types" not in tf
    assert "required_ready" not in pr, "the orchestrator applies the readiness policy again"


def test_artifact_policy_version_is_bound_into_the_intent():
    from meshpipeline.agents.intake.admission_token import approved_intent_canonical
    from meshpipeline.application.artifact_policy import ARTIFACT_POLICY_VERSION
    canon = approved_intent_canonical(
        engine="cfmesh", purpose="external_cfd", input_kind="solid", dimensionality="3D",
        patches=[], engine_params={}, requested_mesh_fidelity=None, request_txt="x",
        source_ref=None)
    assert canon["artifact_policy_version"] == ARTIFACT_POLICY_VERSION


# the Planner receives the tier as bounded advisory context
class _FakeResp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]
        self.usage = None


@pytest.fixture()
def captured_planner(monkeypatch, tmp_path):
    import meshpipeline.contracts.model_inference as router
    from meshpipeline.cad import analysis as cad
    from meshpipeline.engines.snappy import planner

    seen = {}

    async def _fake_call(messages, **kw):
        seen["messages"] = messages
        return _FakeResp(json.dumps({"max_cells": 3_000_000, "surface_level": [3, 3],
                                     "feature_level": 4})), None

    monkeypatch.setattr(router, "call_planner_model", _fake_call)
    monkeypatch.setattr(planner, "llm_router", router)
    monkeypatch.setattr(cad, "analyze_surface", lambda *_a, **_k: {
        "L": 1.0, "diag": 1.0, "min_feature": 0.01, "thin_gap": 0.02, "extent": [1.0, 1.0, 1.0],
        "surface_area": 6.0, "n_triangles": 1000, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}})
    (tmp_path / "input.stl").write_bytes(b"solid x\nendsolid x\n")
    return seen, tmp_path, planner


def _user_msg(seen):
    return next(m["content"] for m in seen["messages"] if m["role"] == "user")


@pytest.mark.parametrize("tier", ["draft", "standard", "max"])
async def test_planner_prompt_carries_the_tier(captured_planner, tier):
    seen, ws, planner = captured_planner
    await planner.make_mesh_plan(workspace=ws, job_id="j", request_txt="mesh it",
                                 surface=prepared_surface(ws / "input.stl"), mesh_fidelity=tier)
    msg = _user_msg(seen)
    assert f"MESH DETAIL PREFERENCE: {tier}" in msg
    assert "not an exact cell target" in msg
    assert "reviewer does NOT check" in msg.lower() or "reviewer does not check" in msg.lower()


async def test_planner_defensively_defaults_a_noncanonical_tier(captured_planner):
    seen, ws, planner = captured_planner
    await planner.make_mesh_plan(workspace=ws, job_id="j", request_txt="mesh it",
                                 surface=prepared_surface(ws / "input.stl"), mesh_fidelity="high")
    msg = _user_msg(seen)
    assert "MESH DETAIL PREFERENCE: standard" in msg
    assert "high" not in msg.lower().split("mesh detail preference:")[1][:20]


async def test_planner_omitted_tier_adds_no_directive(captured_planner):
    seen, ws, planner = captured_planner
    await planner.make_mesh_plan(workspace=ws, job_id="j", request_txt="mesh it",
                                 surface=prepared_surface(ws / "input.stl"), mesh_fidelity="")
    assert "MESH DETAIL PREFERENCE" not in _user_msg(seen)


async def test_planner_always_shows_the_hard_ceiling(captured_planner):
    seen, ws, planner = captured_planner
    await planner.make_mesh_plan(workspace=ws, job_id="j", request_txt="mesh it",
                                 surface=prepared_surface(ws / "input.stl"), mesh_fidelity="max")
    assert str(polcfg.CELL_HARD_LIMIT) in _user_msg(seen)


def test_clamp_cell_budget_bounds_every_tier():
    from meshpipeline.engines.snappy.planner import clamp_cell_budget
    ceil = polcfg.CELL_HARD_LIMIT
    assert clamp_cell_budget(ceil * 100, ceiling=ceil) == ceil       # max can't exceed the ceiling
    assert clamp_cell_budget("not-a-number", ceiling=ceil) <= ceil   # malformed → safe default
    assert clamp_cell_budget(-5, ceiling=ceil) <= ceil
    assert clamp_cell_budget(float("inf"), ceiling=ceil) <= ceil
