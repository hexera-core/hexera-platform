# The caveat floors are DELIVERY policy, not review guidance: if they leaked into
# prompt-rendered text the reviewer would start grading against them, quietly turning a
# waiver threshold into a review bar. The criteria row's advisory threshold stays 0.0.
from meshpipeline.engines.quality_criteria import render_defaults_block
from meshpipeline.engines.snappy.criteria import CRITERIA_ROWS


def test_the_advisory_layer_threshold_is_still_zero():
    row = next(c for c in CRITERIA_ROWS if c.key == "layer_coverage_pct")
    assert row.threshold == 0.0 and row.gating is False


def test_floor_constants_never_reach_prompt_rendered_text():
    block = render_defaults_block()
    assert "LAYER_CAVEAT" not in block
    assert "caveat" not in block.lower()
