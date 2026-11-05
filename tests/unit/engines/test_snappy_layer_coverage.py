# Responsibility: Verify the layer-coverage number is parsed and reaches the reviewer's prism axis to be cited.
from __future__ import annotations

from pathlib import Path

from meshpipeline.engines.snappy import snappy_runner as R  # noqa: E402

# the real OpenFOAM v2412 layer summary table from the half-CRM run
_LOG = """\
Added 405659 out of 858355 cells (47.26%).

patch    faces        layers        overall thickness
                  target   mesh     [m]       [%]
-----    -----    -----    ----     ---       ---
aircraft 171671   5        2.36     0.000947  53.1

Layer mesh : cells:3937840 faces:12191023 points:4348747
"""


def test_parser_reads_the_average_layers_column_as_a_float():
    d = R.parse_layer_coverage(_LOG)
    assert d["overall_pct"] == 47.26
    ap = d["per_patch"]["aircraft"]
    assert ap["layers"] == 2.36
    assert ap["layers_target"] == 5
    assert ap["coverage_pct"] == 53.1


def test_parser_survives_an_all_integer_layers_row():
    log = _LOG.replace("2.36", "5")
    ap = R.parse_layer_coverage(log)["per_patch"]["aircraft"]
    assert ap["layers"] == 5.0


def test_prism_axis_guidance_tells_the_reviewer_to_cite_the_number():
    from meshpipeline.engines.snappy.criteria import REVIEW_AXES
    ax = next(a for a in REVIEW_AXES if a.name == "prism_layer_coverage")
    g = ax.guidance.lower()
    assert "measured" in g and "number" in g
    assert "not resolvable" in g or "never infer" in g  # don't eyeball it
    assert "workflow needs" in g  # still weigh it against the workflow (no numeric setpoint)


def test_reviewer_context_surfaces_the_layer_number_when_present():
    from meshpipeline.agents.reviewer.context import build_review_prompt
    manifest = {
        "quality": {"layer_coverage_pct": 47.26,
                    "per_patch_layers": {"aircraft": {"layers": 2.36, "target": 5,
                                                      "coverage_pct": 53.1}}},
        "patches": {"aircraft": [1]}, "quality_criteria": {"criteria": []},
    }
    _sys, ctx = build_review_prompt(
        manifest=manifest, nav_context={}, workspace=Path("/tmp"),
        step_basename="half_crm.stl", patch_names=["aircraft"],
        patch_colour_legend="", mesh_units="m", request="r", review_brief="b",
        job_id="j", engine="snappy", purpose="external_cfd")
    assert "47.26%" in ctx
    assert "aircraft: 53.1%" in ctx
    assert "MEASURED" in ctx and "no layers" in ctx.lower()   # the anti-hallucination steer


def test_domain_bbox_shows_the_true_far_field_not_the_body():
    from pathlib import Path

    from meshpipeline.agents.reviewer.context import build_review_prompt
    manifest = {
        # true far-field domain (checkMesh) vs the tiny body - they must not be confused
        "quality": {"bounds": [-20.79, 0.0, -20.87, 29.6, 21.65, 21.09]},
        "geometry": {"body_box": {"xmin": 0.06, "xmax": 1.8, "ymin": 0.0, "ymax": 0.8,
                                  "zmin": -0.02, "zmax": 0.24}},
        "patches": {"aircraft": [1]}, "quality_criteria": {"criteria": []},
    }
    _sys, ctx = build_review_prompt(
        manifest=manifest,
        nav_context={"bbox_mm": {"xmin": 0.06, "xmax": 1.8, "ymin": 0.0, "ymax": 0.8,
                                 "zmin": -0.02, "zmax": 0.24}},
        workspace=Path("/tmp"), step_basename="g.stl", patch_names=["aircraft"],
        patch_colour_legend="", mesh_units="m", request="r", review_brief="b",
        job_id="j", engine="snappy", purpose="external_cfd")
    # the Domain bbox line carries the FAR-FIELD extent (29.6), not the body (1.8)
    assert "29.6" in ctx and "-20.79" in ctx
    assert "far-field mesh extent" in ctx.lower()
