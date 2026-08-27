# Caveated layer delivery (adversarially reviewed 2026-08-26): a solver-ready mesh whose
# ONLY review miss is prism-layer coverage delivers with a stated caveat. The eligibility
# predicate is ONE function used by both the status derivation and the caveat author, and
# every conjunct fails CLOSED - these tests knock out one conjunct at a time.
import logging

import pytest

from meshpipeline.application.final_result import (
    FINAL_RESULT_SCHEMA_VERSION,
    RunOutcome,
    build_final_result,
    derive_terminal_status,
    layer_coverage_caveat,
    render_message,
)

jlog = logging.getLogger(__name__)


def _eligible_state(**over):
    state = {
        "api_failure": "",
        "reviewer_verdict": "FAIL",
        "executor_success": True,
        "retry_count": 3,
        "executor_failed_gate": "",
        "reviewer_rebuild_required": False,
        "requirements_strict": False,
        "engine": "snappy",
        "flow_topology": "external",
        "user_dispute": {},
        "reviewer_axis_findings": [
            {"axis_key": "surface_capture", "passed": True, "attempt": 3},
            {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 3,
             "finding": "coverage 46.1% below the near-wall requirement"},
            {"axis_key": "feature_refinement_transition", "passed": True, "attempt": 3},
        ],
        "mesh_manifest": {"quality": {
            "layer_coverage_pct": 46.1,
            "layer_coverage_source": "overall",
            "layer_cells_with": 4610,
            "layer_cells_targeted": 10000,
            "per_patch_layers": {"body": {"layers": 3.2, "target": 5,
                                          "coverage_pct": 52.0}},
            "surface_deviation": {"mean": 0.21, "p95": 0.63},
        }},
    }
    state.update(over)
    return state


def _outcome(**over):
    return RunOutcome.from_graph_state(_eligible_state(**over))


def test_the_eligible_shape_caveats_and_succeeds():
    out = _outcome()
    caveat = layer_coverage_caveat(out)
    assert caveat and caveat["kind"] == "layer_coverage"
    assert caveat["coverage_pct"] == 46.1
    assert caveat["cells_with"] == 4610 and caveat["cells_targeted"] == 10000
    decision = derive_terminal_status(out, job_id="j", jlog=jlog)
    assert decision.succeeded


@pytest.mark.parametrize("knockout, expect_reason", [
    ({"api_failure": "reviewer_provider_down"}, "api failure"),
    ({"executor_success": False}, "executor never validated"),
    ({"executor_failed_gate": "patch_contract"}, "a hard gate failed"),
    ({"reviewer_rebuild_required": True}, "rebuild ordered"),
    ({"requirements_strict": True}, "strict request"),
    ({"engine": "cfmesh"}, "engine not snappy"),
    ({"flow_topology": "internal"}, "internal flow excluded in v1"),
    ({"user_dispute": {"flags": ["x"]}}, "dispute runs never caveat"),
    ({"reviewer_verdict": "PASS"}, "not a FAIL"),
    ({"reviewer_axis_findings": []}, "no findings"),
])
def test_each_knocked_out_conjunct_refuses(knockout, expect_reason):
    out = _outcome(**knockout)
    assert layer_coverage_caveat(out) is None, expect_reason


def test_a_second_failed_axis_refuses():
    findings = [
        {"axis_key": "surface_capture", "passed": False, "attempt": 3},
        {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 3},
        {"axis_key": "feature_refinement_transition", "passed": True, "attempt": 3},
    ]
    assert layer_coverage_caveat(_outcome(reviewer_axis_findings=findings)) is None


def test_stale_findings_from_an_earlier_attempt_refuse():
    # the retained-verdict trap: findings stamped attempt=2 while the run ended at retry 3
    findings = [
        {"axis_key": "surface_capture", "passed": True, "attempt": 2},
        {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 2},
    ]
    assert layer_coverage_caveat(_outcome(reviewer_axis_findings=findings)) is None


def test_an_ungraded_finding_refuses():
    findings = [
        {"axis_key": "surface_capture", "attempt": 3},          # no 'passed' grade
        {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 3},
    ]
    assert layer_coverage_caveat(_outcome(reviewer_axis_findings=findings)) is None


def _quality(**over):
    q = dict(_eligible_state()["mesh_manifest"]["quality"])
    q.update(over)
    return {"mesh_manifest": {"quality": q}}


@pytest.mark.parametrize("qover, expect_reason", [
    ({"layer_coverage_source": "per_patch_min"}, "thickness fallback is not areal"),
    ({"layer_coverage_source": None}, "missing provenance"),
    ({"layer_coverage_pct": 39.9}, "below the floor"),
    ({"layer_coverage_pct": None}, "unmeasured coverage"),
    ({"per_patch_layers": {}}, "no per-patch numbers"),
    ({"per_patch_layers": {"body": {"layers": 0.3, "target": 5, "coverage_pct": 6.0}}},
     "an effectively-bare wall patch"),
    ({"surface_deviation": None}, "surface deviation never measured"),
    ({"surface_deviation": {"mean": 0.4, "p95": 1.4}}, "surface deviation beyond one cell"),
])
def test_quality_conjuncts_fail_closed(qover, expect_reason):
    assert layer_coverage_caveat(_outcome(**_quality(**qover))) is None, expect_reason


def test_a_non_wall_patch_with_zero_target_is_ignored():
    q = _quality(per_patch_layers={
        "body": {"layers": 3.2, "target": 5, "coverage_pct": 52.0},
        "inlet": {"layers": 0, "target": 0, "coverage_pct": 0.0},   # no layers targeted
    })
    caveat = layer_coverage_caveat(_outcome(**q))
    assert caveat and list(caveat["per_patch"]) == ["body"]


def test_surface_capture_failure_with_domain_caveats_never_caveats_layers():
    # the naive-predicate hole: exhausted FAIL with surface_capture failed and domain
    # caveats sitting in state must stay a terminal failure
    findings = [
        {"axis_key": "surface_capture", "passed": False, "attempt": 3},
        {"axis_key": "prism_layer_coverage", "passed": False, "attempt": 3},
    ]
    out = _outcome(reviewer_axis_findings=findings)
    assert layer_coverage_caveat(out) is None
    assert not derive_terminal_status(out, job_id="j", jlog=jlog).succeeded


# the terminal record and its message


def _final(caveats):
    return build_final_result(
        job_id="j", owner_id="o", status=__import__(
            "meshpipeline.application.final_result", fromlist=["TerminalStatus"]
        ).TerminalStatus.succeeded,
        engine="snappy", purpose="external_cfd", dimensionality="3D",
        approved_snapshot_id="s", executor_success=True, reviewer_verdict="FAIL",
        failed_gate="", api_failure="", attempts=3, attempts_max=3, required_ready=True,
        delivered_types=["mesh"], optional_warnings=[], requirement_caveats=caveats)


def test_succeeded_with_fail_verdict_requires_the_layer_caveat():
    with pytest.raises(ValueError):
        _final([])
    with pytest.raises(ValueError):
        _final([{"kind": "domain_extent", "direction": "up", "requested": 5.0,
                 "measured": 4.6, "ruler_m": 0.06}])
    fr = _final([{"kind": "layer_coverage", "coverage_pct": 46.1, "cells_with": 4610,
                  "cells_targeted": 10000, "per_patch": {}, "finding": "f"}])
    assert fr.status.value == "succeeded" and fr.outcome_code == "success"


def test_layer_caveat_message_is_caveats_first_and_never_overclaims():
    fr = _final([{"kind": "layer_coverage", "coverage_pct": 46.1, "cells_with": 4610,
                  "cells_targeted": 10000,
                  "per_patch": {"body": {"layers": 3.2, "target": 5, "coverage_pct": 52.0},
                                "wing": {"layers": 2.0, "target": 5, "coverage_pct": 33.0}},
                  "finding": "coverage below the near-wall requirement"}])
    msg = render_message(fr)
    assert msg.startswith("Delivered with stated deviations")
    assert "46.1% of the targeted wall cells (4610 of 10000" in msg
    assert "patch wing" in msg and "patch body" in msg
    assert "Every mesh-quality check passed" not in msg
    assert msg.index("prism layers") < msg.index("Mesh generation completed successfully")


def test_mixed_kind_and_legacy_kindless_caveats_render_without_crashing():
    fr = _final([
        {"direction": "up", "requested": 5.0, "measured": 4.6, "ruler_m": 0.06},  # kind-less
        {"kind": "layer_coverage", "coverage_pct": 41.0, "cells_with": None,
         "cells_targeted": None, "per_patch": {}, "finding": ""},
        {"kind": "someday_new_kind"},
    ])
    msg = render_message(fr)
    assert "up margin: requested 5, delivered 4.6" in msg
    assert "41.0% of the targeted wall cells" in msg
    assert "unrecognized kind" in msg
    assert "Every mesh-quality check passed" not in msg


def test_malformed_domain_caveat_degrades_to_prose_not_typeerror():
    # the latent crash the review found: f-string :g on None raised TypeError at terminal
    fr = build_final_result(
        job_id="j", owner_id="o", status=__import__(
            "meshpipeline.application.final_result", fromlist=["TerminalStatus"]
        ).TerminalStatus.succeeded,
        engine="snappy", purpose="external_cfd", dimensionality="3D",
        approved_snapshot_id="s", executor_success=True, reviewer_verdict="PASS",
        failed_gate="", api_failure="", attempts=1, attempts_max=3, required_ready=True,
        delivered_types=["mesh"], optional_warnings=[],
        requirement_caveats=[{"kind": "domain_extent", "direction": "up",
                              "requested": None, "measured": 4.6, "ruler_m": 0.06}])
    msg = render_message(fr)
    assert "fell short of the request (see the result record)" in msg


def test_v5_roundtrip_of_kindless_records_is_unchanged():
    from meshpipeline.application.final_result import FinalResult
    fr = _final([{"kind": "layer_coverage", "coverage_pct": 46.1, "cells_with": 1,
                  "cells_targeted": 2, "per_patch": {}, "finding": "f"}])
    again = FinalResult.from_dict(fr.to_dict())
    assert again.requirement_caveats == fr.requirement_caveats
    assert again.schema_version == FINAL_RESULT_SCHEMA_VERSION


def test_layer_parse_provenance_shapes():
    from meshpipeline.engines.snappy_hexmesh import parse_layer_coverage
    log = "Added 4610 out of 10000 cells (46.1%)\n"
    out = parse_layer_coverage(log)
    assert out["overall_pct"] == 46.1
    assert out["cells_with_layers"] == 4610 and out["cells_targeted"] == 10000
    assert parse_layer_coverage("")["overall_pct"] is None
