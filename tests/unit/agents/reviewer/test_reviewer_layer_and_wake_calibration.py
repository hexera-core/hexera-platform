# Responsibility: Verify the reviewer is given a decisive layer bar, an honest layer-policy account and a mesh-based wake test.
# Boundaries: prompt and rubric text only - the model's behaviour is evidenced in the PR, not here.
"""Night of 2026-10-01: on gpt-5.6-terra the reviewer rejected meshes the previous model passed.

* Prism layers: the same 59.6% coverage on the half-CRM failed on attempt 1 and passed on attempt 3
  of job 4d8b318d; the prompt gave bands ("40-70% partial") but no line between pass and fail, and
  claimed a thin-feature reduction that the uniform policy never made.
* Wake: "refine the wake" was read as "the builder's config must contain a wake region", which no
  rebuild of this engine can add - jobs e0fa8ad0, 4d8b318d, 76ad00ec and 8e8e8309.
"""
from __future__ import annotations

from pathlib import Path

from meshpipeline.agents.reviewer.context import (
    build_review_prompt,
    layer_grading,
    layer_policy_line,
)

APP = Path(__file__).parents[4] / "src" / "meshpipeline"


def _prompt(quality: dict, purpose: str = "external_cfd") -> tuple[str, str]:
    manifest = {"quality": quality, "patches": {"aircraft": [1]},
                "quality_criteria": {"criteria": []}}
    return build_review_prompt(
        manifest=manifest, nav_context={}, workspace=Path("/nonexistent-review-ws"),
        step_basename="crm.stl", patch_names=["aircraft"], patch_colour_legend="",
        mesh_units="m", request="Takeoff aero, 5 prism layers, wall functions y+ 30-300.",
        review_brief="b", job_id="j", engine="snappy", purpose=purpose)


# The half-CRM of job e0fa8ad0, attempt 2, as the manifest carried it: one `aircraft` patch, a
# uniform policy (the class split could not be staged), 56.4% of the wall thin or razor-sharp.
_CRM_UNIFORM = {
    "version": 1, "source": "thin_feature_classifier", "mode": "uniform",
    "requested_layers": 5, "escalation_stage": 0, "uniform_n_layers": 5,
    "classes": {"normal": {"n_layers": 5, "area_frac": 0.436},
                "thin": {"n_layers": 5, "area_frac": 0.301},
                "razor": {"n_layers": 5, "area_frac": 0.263}},
    "region_patches": {"aircraft": "uniform"},
}
_CRM_QUALITY = {
    "layer_coverage_pct": 48.0194, "layer_coverage_source": "overall",
    "per_patch_layers": {"aircraft": {"layers": 2.4, "target": 5, "coverage_pct": 57.1}},
    "layer_policy": _CRM_UNIFORM,
}


# the layer bar

def test_external_flow_states_where_partial_coverage_passes_and_where_it_fails():
    g = layer_grading("external_cfd")
    assert "NOT by itself a missed requirement" in g
    assert "40-70% is partial" in g and "PASSES this axis for a wall-function case" in g
    assert "below 40%" in g and "near zero" in g
    assert "wall-resolved" in g and "below 70%" in g


def test_internal_flow_keeps_the_guide_it_has_always_had():
    assert layer_grading("internal_cfd") == (
        "(Rough guide: >=70% is good, 40-70% partial, <40% poor; weigh it against the "
        "workflow's y+ target.)")
    assert layer_grading("") == layer_grading("internal_cfd")


def test_the_crm_review_context_carries_the_bar_and_the_true_policy():
    _sys, ctx = _prompt(_CRM_QUALITY)
    assert "48.0194%" in ctx and "aircraft: 57.1% (2.4/5 layers)" in ctx
    assert "PASSES this axis for a wall-function case" in ctx
    # the uniform policy reduced nothing, and the reviewer is no longer told it did
    assert "DELIBERATELY reduced" not in ctx
    assert "every face was asked for the full 5 layers" in ctx
    assert "56.4% of the wall is locally thin or razor-sharp" in ctx


def test_the_internal_review_context_is_unchanged():
    _sys, ctx = _prompt({"layer_coverage_pct": 50.0}, purpose="internal_cfd")
    assert "(Rough guide: >=70% is good, 40-70% partial, <40% poor" in ctx
    assert "PASSES this axis" not in ctx


# the policy line says what was authored

def test_a_real_thin_feature_reduction_is_still_declared_as_one():
    pol = {"mode": "split", "requested_layers": 5, "escalation_stage": 0,
           "classes": {"normal": {"n_layers": 5, "area_frac": 0.7},
                       "thin": {"n_layers": 3, "area_frac": 0.2},
                       "razor": {"n_layers": 1, "area_frac": 0.1}},
           "region_patches": {"body": "normal", "body_thin": "thin", "body_razor": "razor"}}
    line = layer_policy_line(pol)
    assert "DELIBERATELY reduced" in line and "thin: 3 layers over 20.0%" in line
    assert "Coverage missing OUTSIDE the declared thin/razor fraction is still a real finding" in line


def test_a_uniform_policy_is_not_described_as_a_reduction():
    line = layer_policy_line(_CRM_UNIFORM)
    assert "DELIBERATELY" not in line
    assert "no class carries fewer" in line
    assert "do not credit a reduction this policy did not make" in line


def test_a_whole_wall_escalation_says_the_whole_wall_was_cut():
    pol = {**_CRM_UNIFORM, "escalation_stage": 1, "uniform_n_layers": 2,
           "classes": {c: {"n_layers": 2, "area_frac": v["area_frac"]}
                       for c, v in _CRM_UNIFORM["classes"].items()}}
    line = layer_policy_line(pol)
    assert "lowered on the WHOLE wall" in line
    assert "kept on well-proportioned surface" not in line


def test_a_global_policy_says_the_whole_wall_carries_the_reduced_count():
    pol = {"mode": "global", "requested_layers": 5, "escalation_stage": 0,
           "classes": {"normal": {"n_layers": 5, "area_frac": 0.02},
                       "thin": {"n_layers": 3, "area_frac": 0.9},
                       "razor": {"n_layers": 1, "area_frac": 0.08}},
           "region_patches": {"blade": "thin"}}
    line = layer_policy_line(pol)
    assert "WHOLE wall carries the thin count of 3 layers instead of the requested 5" in line


def test_a_malformed_policy_record_never_breaks_the_prompt():
    assert layer_policy_line({"classes": {"thin": "x", "razor": {"n_layers": "?"}}})
    assert layer_policy_line({"classes": {"normal": {}}, "requested_layers": "five"})


# the wake is judged on the mesh

def test_the_wake_axis_judges_the_mesh_not_the_recipe():
    from meshpipeline.engines.purpose_review_axes import _EXTERNAL_CFD_AXES
    ax = next(a for a in _EXTERNAL_CFD_AXES if a.name == "wake_resolution")
    g = ax.guidance
    assert "not the recipe" in g
    assert "not by itself a failure" in g
    assert "background-sized" in g
    # the real failure stays a failure
    assert "an abrupt refinement drop immediately behind the body" in ax.failure_signals


def test_the_layer_axis_never_fails_on_the_average_count_alone():
    from meshpipeline.engines.snappy.criteria import REVIEW_AXES
    ax = next(a for a in REVIEW_AXES if a.name == "prism_layer_coverage")
    assert "never fail this axis for the average count alone" in ax.guidance


def test_the_system_prompt_reads_a_requested_setting_as_a_target():
    text = (APP / "prompts" / "reviewer" / "system.txt").read_text()
    assert "It is not a promise about every face" in text
    assert "judge the mesh you inspected, not the recipe that produced it" in text
    # and a setting that was never applied still fails
    assert "a different count was configured" in text
