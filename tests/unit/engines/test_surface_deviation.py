# Responsibility: Verify the surface-capture axis judges from the measured deviation reached through a declared hook.
from __future__ import annotations

from pathlib import Path


def test_context_surfaces_the_surface_deviation_number():
    from meshpipeline.agents.reviewer.context import build_review_prompt
    manifest = {
        "quality": {"surface_deviation": {"mean_ratio": 0.009, "p95_ratio": 0.035,
                                          "max_ratio": 1.29, "frac_beyond_one_cell": 8e-05,
                                          "cell_size_m": 0.00125}},
        "patches": {"aircraft": [1]}, "quality_criteria": {"criteria": []},
    }
    _sys, ctx = build_review_prompt(
        manifest=manifest, nav_context={}, workspace=Path("/tmp"), step_basename="g.stl",
        patch_names=["aircraft"], patch_colour_legend="", mesh_units="m", request="r",
        review_brief="b", job_id="j", engine="snappy", purpose="external_cfd")
    assert "mean=0.009" in ctx and "p95=0.035" in ctx
    assert "AUTHORITATIVE" in ctx
    assert "render cannot show it" in ctx.lower() or "a render cannot show" in ctx.lower()


def test_surface_capture_axis_judges_from_the_measured_deviation():
    from meshpipeline.engines.snappy.criteria import REVIEW_AXES
    ax = next(a for a in REVIEW_AXES if a.name == "surface_capture")
    g = ax.guidance.lower()
    assert "measured" in g and "deviation" in g
    assert "render cannot" in g or "a render cannot" in g   # don't eyeball snap quality
    assert "surface_deviation" in ax.evidence


def test_finalize_consumes_a_DECLARED_engine_hook_not_an_engine_name_list():
    src = (Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline" / "engines" /
           "cfmesh" / "finalize.py").read_text()
    # the surface-capture metric consumes a DECLARED hook, and the layer metric keys off
    # capability-by-presence - neither gates on a hardcoded engine name (that would be the creep).
    assert 'getattr(R, "surface_capture_reference", None)' in src
    assert 'hasattr(R, "parse_layer_coverage")' in src
    assert 'engine in ("snappy", "cfmesh")' not in src   # my metric no longer name-gates


def test_snappy_declares_the_surface_capture_reference_hook():
    from meshpipeline.engines.snappy import snappy_runner
    assert callable(getattr(snappy_runner, "surface_capture_reference", None))


def test_hook_returns_none_without_a_wall_patch():
    from meshpipeline.engines.snappy import snappy_runner
    assert snappy_runner.surface_capture_reference("/tmp", {"farfield": "farfield"}) is None
    assert snappy_runner.surface_capture_reference("/tmp", None) is None
