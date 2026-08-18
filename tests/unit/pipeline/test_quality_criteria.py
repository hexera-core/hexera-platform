# Responsibility: Verify each criterion carries a rationale and evidence link, and gates in its declared order.
from meshpipeline.engines.quality_criteria import (  # noqa: E402
    criteria_for,
    evaluate,
    production_grade,
    render_defaults_block,
    render_report,
)
from meshpipeline.engines.registry import engine_names  # noqa: E402
from meshpipeline.engines.snappy.drivers import _judge_snappy  # noqa: E402

_CLEAN_SNAPPY = {"timed_out": False, "rc": 0, "wall_faces": 227461,
                 "fatal": [], "skew_fraction": 3e-6}


def test_every_criterion_has_rationale_and_https_evidence():
    # rows now load through the SPEC (EngineSpec.criteria) - no pipeline-level dict
    for engine in engine_names():
        criteria = criteria_for(engine)
        assert criteria, f"{engine} has no criteria"
        for c in criteria:
            assert len(c.rationale) > 40, f"{engine}/{c.key}: rationale too thin to justify anything"
            assert c.evidence_url.startswith("https://"), f"{engine}/{c.key}: no https citation"


def test_historical_thresholds_are_preserved():
    snappy = {c.key: c for c in criteria_for("snappy")}
    assert snappy["skew_fraction"].threshold == 5e-4 and snappy["skew_fraction"].op == "<="
    assert snappy["rc"].threshold == 0 and snappy["rc"].gating
    assert snappy["wall_faces"].op == ">" and snappy["wall_faces"].threshold == 0
    assert snappy["fatal"].op == "empty" and snappy["fatal"].gating
    assert snappy["max_non_ortho"].threshold == 65.0 and not snappy["max_non_ortho"].gating
    cf = {c.key: c for c in criteria_for("cfmesh")}
    assert "fatal" in cf and "rc" in cf
    assert "skew_fraction" not in cf     # localization bar is snappy's (hex+prism) bar
    assert "wall_faces" not in cf


def test_unknown_engine_falls_back_to_cfmesh():
    assert criteria_for("does-not-exist") == criteria_for("cfmesh")


def test_production_grade_clean_mesh_passes():
    ok, failing = production_grade("snappy", _CLEAN_SNAPPY)
    assert ok and failing is None


def test_production_grade_fails_on_first_gating_violation_in_order():
    ok, failing = production_grade("snappy", {**_CLEAN_SNAPPY, "timed_out": True, "rc": 1})
    assert not ok and failing["key"] == "timed_out"   # ordered: timeout precedes rc
    ok, failing = production_grade("snappy", {**_CLEAN_SNAPPY, "skew_fraction": 1e-3})
    assert not ok and failing["key"] == "skew_fraction"


def test_missing_measurement_is_not_evaluated_and_does_not_fail():
    rows = {r["key"]: r for r in evaluate("snappy", {"fatal": []})}
    assert rows["fatal"]["passed"] is True
    assert rows["skew_fraction"]["passed"] is None    # absent → not evaluated
    ok, _ = production_grade("snappy", {"fatal": []})
    assert ok                                          # absent gating keys don't fail


def test_advisory_criteria_never_gate():
    ok, _ = production_grade("snappy", {**_CLEAN_SNAPPY,
                                        "max_non_ortho": 89.0, "layer_coverage": 0.0})
    assert ok


def test_render_report_carries_measured_values_and_links():
    report = render_report(evaluate("snappy", _CLEAN_SNAPPY))
    assert "PASS" in report and "https://" in report
    assert "227461" in report


def test_render_defaults_block_covers_both_engines_with_links():
    block = render_defaults_block()
    assert "[snappyHexMesh]" in block and "[cfMesh]" in block
    assert block.count("https://") >= 8


# --- judge parity: same verdicts + repair guidance as the pre-registry chain --- #

def test_judge_clean_mesh_is_production_grade():
    ok, reason = _judge_snappy({"rc": 0, "timed_out": False},
                               {"fatal": [], "skew_fraction": 3e-6, "skew_faces": 19}, 227461)
    assert ok and reason == "production-grade"


def test_judge_repair_guidance_parity():
    cases = [
        ({"timed_out": True}, {}, 100, "TIMEOUT"),
        ({"rc": 1}, {}, 100, "snappyHexMesh FAILED"),
        ({"rc": 0}, {}, 0, "CARVE LEAKED"),
        ({"rc": 0}, {"fatal": ["incorrectly oriented faces"]}, 100, "prism LAYERS are inverting"),
        ({"rc": 0}, {"fatal": ["open cells"]}, 100, "topology/carve defect"),
        ({"rc": 0}, {"fatal": [], "skew_fraction": 1e-3, "skew_faces": 4000}, 100,
         "WIDESPREAD skewness"),
    ]
    for result, q, wall_faces, expect in cases:
        ok, reason = _judge_snappy(result, q, wall_faces)
        assert not ok, (result, q, wall_faces)
        assert expect in reason, f"expected {expect!r} in {reason!r}"


def test_judge_rc_none_means_no_run_recorded_and_passes_rc_gate():
    # historic behavior: only an explicit nonzero rc fails; None is tolerated
    ok, _ = _judge_snappy({"rc": None, "timed_out": False},
                          {"fatal": [], "skew_fraction": 0.0}, 100)
    assert ok
