# Responsibility: Verify a failed run's closing message names what actually failed, per cause, and survives the record.
from __future__ import annotations

import pytest

from meshpipeline.application.final_result import (
    FailureCategory,
    FinalResult,
    TerminalStatus,
    build_final_result,
    merge_durable_facts,
    render_message,
)
from meshpipeline.contracts.failure_cause import FailureCause


def _fail(gate: str, cause: str = "", facts: dict | None = None, **kw) -> FinalResult:
    return build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.failed, engine=kw.pop("engine", "snappy"),
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="s",
        executor_success=False, reviewer_verdict=kw.pop("verdict", ""), failed_gate=gate,
        api_failure=kw.pop("api_failure", ""), attempts=kw.pop("attempts", 1), attempts_max=4,
        required_ready=False, delivered_types=[], optional_warnings=[],
        failure_cause=cause, failure_facts=facts or {}, **kw)


def test_the_car_wall_job_is_told_the_truth():
    """Job ac1daa3e as it would end today: manifest_valid named a missing patch."""
    fr = _fail("manifest_valid", FailureCause.CONTRACT_MISMATCH,
               {"missing": ["car wall"], "renamed": {"car wall": "car_wall"},
                "present": ["car_wall", "farfield", "ground"]})
    msg = render_message(fr)
    assert fr.failure_category == FailureCategory.gate_failed.value     # the coarse class is kept
    assert fr.failure_cause == "contract_mismatch"
    assert "quality" not in msg.lower()
    assert "'car wall'" in msg and "'car_wall'" in msg and "our mistake" in msg
    assert "did not try again" in msg
    assert "rename 'car wall' to 'car_wall'" in msg


CASES = [
    ("quality_floor", FailureCause.MESH_QUALITY,
     {"checks": [{"key": "skew_fraction", "label": "Skewed faces are localized",
                  "measured": 0.002, "op": "<=", "threshold": 0.0005}]},
     ["badly skewed", "0.2%", "0.05%"]),
    ("resolution_floor", FailureCause.UNDER_RESOLVED, {"cells_across": 7.0, "needed": 12},
     ["too coarse", "7 cells", "at least 12"]),
    ("manifest_valid", FailureCause.CELL_BUDGET, {"cells": 14_200_000, "limit": 12_000_000},
     ["14,200,000 cells", "12,000,000-cell limit"]),
    ("manifest_valid", FailureCause.PATCH_NOT_CAPTURED, {"patches": ["outlet_2"]},
     ["lost the boundary 'outlet_2'"]),
    ("boundary_types", FailureCause.BOUNDARY_TYPE,
     {"mistyped": [{"name": "sym", "declared": "symmetry", "want": "symmetryPlane",
                    "got": "patch"}]}, ["'sym' came out as type 'patch'"]),
    ("domain_extent", FailureCause.DOMAIN_EXTENT,
     {"misses": [{"direction": "downstream", "requested": 10.0, "measured": 4.0}]},
     ["downstream is 4 reference lengths where you asked for 10"]),
    ("solvability", FailureCause.NOT_SOLVABLE, {}, ["did not converge"]),
    ("finalize", FailureCause.ENGINE_CRASHED, {"engine": "snappyHexMesh"},
     ["snappyHexMesh", "stopped before it finished"]),
]


@pytest.mark.parametrize(("gate", "cause", "facts", "phrases"), CASES,
                         ids=[c[1].value for c in CASES])
def test_each_cause_gets_its_own_sentence(gate, cause, facts, phrases):
    fr = _fail(gate, cause, facts)
    msg = render_message(fr)
    for p in phrases:
        assert p in msg, msg
    assert "required quality checks" not in msg
    assert "did not try again" not in msg          # all of these keep their retries


def test_the_cause_survives_the_durable_record():
    fr = _fail("manifest_valid", FailureCause.CELL_BUDGET, {"cells": 9, "limit": 5})
    back = FinalResult.from_dict(fr.to_dict())
    assert (back.failure_cause, back.failure_detail, back.failure_next_step, back.retry_skipped) \
        == (fr.failure_cause, fr.failure_detail, fr.failure_next_step, fr.retry_skipped)
    assert render_message(back) == render_message(fr)


def test_a_record_written_before_causes_existed_still_renders():
    d = _fail("quality_floor").to_dict()
    for k in ("failure_cause", "failure_detail", "failure_next_step", "retry_skipped"):
        d.pop(k)
    msg = render_message(FinalResult.from_dict(d))
    assert "did not pass one of its checks" in msg


def test_a_declared_gate_is_never_guessed_from_its_key():
    # manifest_valid can mean a crash, a missing boundary or a mesh over budget: with no
    # recorded cause the message stays neutral rather than naming the wrong one
    fr = _fail("manifest_valid")
    assert fr.failure_cause == "" and "did not pass one of its checks" in render_message(fr)


def test_a_seam_without_a_recorded_cause_is_still_named():
    fr = _fail("solvability")
    assert fr.failure_cause == "not_solvable" and "did not converge" in render_message(fr)


def test_a_review_that_did_not_conclude_is_not_blamed_on_a_gate():
    fr = _fail("patch_contract", FailureCause.CONTRACT_MISMATCH, {"missing": ["x"]},
               api_failure="reviewer_evidence_missing")
    assert fr.failure_category == FailureCategory.internal_pipeline_failure.value
    # the gate's cause is not told - the review is: it is what did not finish
    assert fr.failure_cause == "review_incomplete" and "'x'" not in render_message(fr)
    assert "review" in fr.failure_detail and "on our side" in fr.failure_detail


def test_a_crash_keeps_the_cause_the_checkpoint_recorded():
    facts = merge_durable_facts(checkpoint_state={
        "executor_failed_gate": "patch_contract", "executor_failure_cause": "contract_mismatch",
        "executor_failure_facts": {"missing": ["inlet"]}})
    assert facts["failure_cause"] == "contract_mismatch"
    assert facts["failure_facts"] == {"missing": ["inlet"]}


def test_success_carries_no_cause():
    fr = build_final_result(
        job_id="j", owner_id="o", status=TerminalStatus.succeeded, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="s",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=4, required_ready=True, delivered_types=["mesh_bundle"],
        optional_warnings=[], failure_cause="mesh_quality", failure_facts={})
    assert fr.failure_cause == "" and fr.failure_detail == "" and fr.retry_skipped is False


# admission refusals: the CAD's, or the setup's (review: "setup refusals blame the CAD")

def test_a_measured_refusal_is_the_cads_and_says_why():
    fr = _fail("geometry", FailureCause.GEOMETRY_REJECTED, {
        "reason": "[GEOMETRY_UNSUITABLE] the input surface self-intersects.",
        "phases": ["measured"], "codes": ["geometry_unsuitable"]})
    msg = render_message(fr)
    assert fr.failure_category == FailureCategory.input_rejected.value
    assert "the problem is in the CAD file" in msg
    assert "the input surface self-intersects" in msg and "[" not in msg


def test_a_declared_setup_refusal_does_not_blame_a_valid_file():
    fr = _fail("geometry", FailureCause.GEOMETRY_REJECTED, {
        "reason": "a 'symmetry' patch was declared, but the cfmesh engine does not produce "
                  "symmetry-plane patches yet.",
        "phases": ["declared"], "codes": ["symmetry_unsupported"]})
    msg = render_message(fr)
    assert fr.failure_category == FailureCategory.incompatible_requirements.value
    assert "CAD" not in msg and "upload it again" not in msg
    assert "symmetry-plane patches" in msg and "Your geometry was not the problem" in msg
    assert "the setting, or the engine" in msg


def test_a_mixed_refusal_names_both_changes():
    # review: fixing the file alone would meet the setup refusal on the next run
    fr = _fail("geometry", FailureCause.GEOMETRY_REJECTED, {
        "reason": "the input surface self-intersects.  engine_params: bad layers value",
        "phases": ["declared", "measured"],
        "measured_reason": "the input surface self-intersects.",
        "declared_reason": "engine_params: bad layers value"})
    msg = render_message(fr)
    assert fr.failure_category == FailureCategory.input_rejected.value
    assert "the problem is in the CAD file" in msg and "the input surface self-intersects" in msg
    assert "Fix the geometry and upload it again." in msg
    assert "The setup also needs a change before this can run: engine_params: bad layers value"         in msg


def test_an_older_admission_record_keeps_the_class_sentence():
    fr = _fail("geometry")
    assert fr.failure_category == FailureCategory.input_rejected.value
    assert "the problem is in the CAD file" in render_message(fr)
