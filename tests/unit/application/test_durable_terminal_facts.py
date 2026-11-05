# Responsibility: Verify a terminal verdict is reconstructed from durable facts, fabricating no gate result or delivery.
from __future__ import annotations

# A LATE FAILURE MUST NOT REWRITE WHAT EARLIER STAGES ALREADY PROVED.
# A real DPW4 CRM run selected snappy, meshed natively, and passed all four blocking gates.
# The Reviewer then crashed building its evidence. The terminal verdict was right that the
# run failed - and wrong about everything that had already succeeded: engine "",
# executor_success false, attempts 0. Those facts were not unknown, they were merely out of
# scope at the crash site.
# The overall outcome is still owned by the application and still conservative: a crash is a
# failure, delivery is never assumed, and nothing here can turn a failed run into a
# successful one.
import pytest

from meshpipeline.application.final_result import (
    FailureCategory,
    FinalResult,
    ReviewVerdict,
    TerminalStatus,
    build_final_result,
    merge_durable_facts,
)

APPROVED = {"mesh_engine": "snappy", "purpose": "external_cfd", "input_kind": "body-surface",
            "dimensionality": "3D", "approved_snapshot_id": "snap-001"}

# what the checkpointer holds after the executor finished and the reviewer then crashed
AFTER_EXECUTOR = {"engine": "snappy", "purpose": "external_cfd", "dimensionality": "3D",
                  "executor_success": True, "executor_failed_gate": "", "retry_count": 1}


def _crash_result(facts: dict, **over) -> FinalResult:
    kw = {"job_id": "job-1", "owner_id": "owner-1", "status": TerminalStatus.failed,
          "requested_mesh_fidelity": "draft", "effective_mesh_fidelity": "draft",
          "mesh_fidelity_source": "user", "fidelity_policy_version": "v1",
          "api_failure": "pipeline: crash", "attempts_max": 5,
          "required_ready": False, "delivered_types": [], "optional_warnings": []}
    kw.update({k: facts[k] for k in ("engine", "purpose", "dimensionality",
                                     "approved_snapshot_id", "executor_success",
                                     "reviewer_verdict", "failed_gate", "attempts")})
    kw.update(over)
    return build_final_result(**kw)


# late Reviewer exception (1-12)

def test_a_late_reviewer_crash_keeps_the_facts_the_run_had_already_established():
    facts = merge_durable_facts(approved=APPROVED, checkpoint_state=AFTER_EXECUTOR,
                                db_attempt=1)
    fr = _crash_result(facts)

    # 5-6: the outcome is still a failure, and still the right kind of failure
    assert fr.status is TerminalStatus.failed
    assert fr.failure_category == FailureCategory.internal_pipeline_failure.value

    # 1-2, 7-9: what earlier stages proved survives
    assert fr.engine == "snappy", "the selected engine was erased by a later failure"
    assert fr.purpose == "external_cfd"
    assert fr.dimensionality == "3D"
    assert fr.approved_snapshot_id == "snap-001"
    assert fr.executor_success is True, (
        "a completed executor stage was rewritten as unsuccessful")
    assert fr.attempts == 1, "the attempt count was reset by the crash"

    # 3: gates passed, so no gate is blamed
    assert fr.failed_gate == ""

    # 10: a crash is not a verdict - neither a PASS nor a quality rejection
    assert fr.reviewer_verdict is not ReviewVerdict.passed
    assert fr.failure_category != FailureCategory.review_rejected.value
    assert fr.reviewer_verdict is None

    # 11: packaging never ran, so nothing may be called ready
    assert fr.required_ready is False
    assert fr.missing_outputs == ["mesh_bundle", "viewer_data"]  # nothing delivered


def test_the_crash_verdict_serializes_consistently_for_every_surface():
    # 12: one dict is persisted, returned by REST, carried in the outbox row and published in
    # the closing event - so agreement is structural, not four separate renderings
    facts = merge_durable_facts(approved=APPROVED, checkpoint_state=AFTER_EXECUTOR, db_attempt=1)
    fr = _crash_result(facts)
    payload = fr.to_dict()
    assert payload["engine"] == "snappy"
    assert payload["executor_success"] is True
    assert payload["required_ready"] is False
    assert payload["outcome_code"] == FailureCategory.internal_pipeline_failure.value
    # the round trip a REST/outbox consumer performs
    assert FinalResult.from_dict(payload).engine == "snappy"
    assert FinalResult.from_dict(payload).executor_success is True


def test_a_reviewer_crash_is_distinguishable_from_a_reviewer_rejection():
    crash = _crash_result(merge_durable_facts(approved=APPROVED,
                                              checkpoint_state=AFTER_EXECUTOR, db_attempt=1))
    rejected = build_final_result(
        job_id="job-1", owner_id="owner-1", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="snap-001",
        executor_success=True, reviewer_verdict="FAIL", failed_gate="", api_failure="",
        attempts=1, attempts_max=5, required_ready=False, delivered_types=[],
        optional_warnings=[])
    assert crash.failure_category == FailureCategory.internal_pipeline_failure.value
    assert rejected.failure_category == FailureCategory.review_rejected.value
    assert crash.reviewer_verdict is None
    assert rejected.reviewer_verdict is ReviewVerdict.failed


# early failure (13-16)

def test_an_early_crash_does_not_claim_executor_success():
    # nothing had been checkpointed yet
    facts = merge_durable_facts(approved=APPROVED, checkpoint_state=None, db_attempt=0)
    fr = _crash_result(facts)
    assert fr.executor_success is False, "success was inferred from an absent checkpoint"
    assert fr.failed_gate == ""
    assert fr.status is TerminalStatus.failed
    # 15: the approved intent is still known - it was verified before dispatch
    assert fr.engine == "snappy" and fr.purpose == "external_cfd"


def test_an_absent_checkpoint_never_fabricates_a_gate_result():
    facts = merge_durable_facts(approved=APPROVED, checkpoint_state={}, db_attempt=0)
    assert facts["executor_success"] is False
    assert facts["failed_gate"] == ""
    assert facts["reviewer_verdict"] == ""


def test_intent_is_reported_even_when_the_engine_was_never_selected():
    facts = merge_durable_facts(approved={"purpose": "external_cfd"}, checkpoint_state=None)
    assert facts["engine"] == "", "an engine was invented for a run that never chose one"
    assert facts["purpose"] == "external_cfd"


# executor failure (17-20)

def test_a_failed_gate_is_reported_as_a_gate_failure_not_a_crash():
    after_gate_fail = {"engine": "snappy", "purpose": "external_cfd",
                       "executor_success": False, "executor_failed_gate": "patch_contract",
                       "retry_count": 2}
    facts = merge_durable_facts(approved=APPROVED, checkpoint_state=after_gate_fail, db_attempt=2)
    fr = build_final_result(
        job_id="job-1", owner_id="owner-1", status=TerminalStatus.failed,
        engine=facts["engine"], purpose=facts["purpose"], dimensionality=facts["dimensionality"],
        approved_snapshot_id=facts["approved_snapshot_id"],
        executor_success=facts["executor_success"], reviewer_verdict=facts["reviewer_verdict"],
        failed_gate=facts["failed_gate"], api_failure="", attempts=facts["attempts"],
        attempts_max=5, required_ready=False, delivered_types=[], optional_warnings=[])
    assert fr.executor_success is False
    assert fr.failure_category == FailureCategory.gate_failed.value
    assert fr.patch_contract_ok is False
    # 19: the reviewer never ran and must not be reported as having done so
    assert fr.reviewer_verdict is None


def test_executor_failure_keeps_its_attempt_count():
    facts = merge_durable_facts(approved=APPROVED,
                                checkpoint_state={"executor_success": False, "retry_count": 3},
                                db_attempt=3)
    assert facts["attempts"] == 3


# successful run unchanged (21-23)

def test_a_successful_run_is_unchanged():
    fr = build_final_result(
        job_id="job-1", owner_id="owner-1", status=TerminalStatus.succeeded, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="snap-001",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=5, required_ready=True,
        delivered_types=["mesh_bundle", "viewer_data"], optional_warnings=[])
    assert fr.status is TerminalStatus.succeeded
    assert fr.outcome_code == "success"
    assert fr.failure_category is None
    assert fr.reviewer_verdict is ReviewVerdict.passed
    assert fr.required_ready is True
    assert fr.missing_outputs == []


def test_required_artifacts_still_gate_readiness():
    fr = build_final_result(
        job_id="job-1", owner_id="owner-1", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="snap-001",
        executor_success=True, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=5, required_ready=False, delivered_types=[],
        optional_warnings=[])
    # reviewed and passed, but nothing was stored: delivery failed, and it is NOT ready
    assert fr.failure_category == FailureCategory.delivery_failed.value
    assert fr.required_ready is False


def test_model_prose_cannot_change_the_outcome():
    # the verdict is built from measured arguments; there is no prose input to it at all
    import inspect
    params = set(inspect.signature(build_final_result).parameters)
    for prose in ("message", "text", "summary", "reasoning", "explanation", "closing"):
        assert prose not in params, f"terminal truth accepts prose via {prose!r}"
    # and a PASS the executor never earned is refused
    fr = build_final_result(
        job_id="job-1", owner_id="owner-1", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=False, reviewer_verdict="PASS", failed_gate="", api_failure="",
        attempts=1, attempts_max=5, required_ready=False, delivered_types=[],
        optional_warnings=[])
    assert fr.reviewer_verdict is None, "a stale PASS counted on a run that never validated"
    assert fr.status is TerminalStatus.failed


# idempotency and ordering (24-26)

def test_merging_the_same_durable_facts_twice_is_identical():
    a = merge_durable_facts(approved=APPROVED, checkpoint_state=AFTER_EXECUTOR, db_attempt=1)
    b = merge_durable_facts(approved=APPROVED, checkpoint_state=AFTER_EXECUTOR, db_attempt=1)
    assert a == b


def test_replay_of_the_same_durable_state_reconstructs_the_same_result():
    facts = merge_durable_facts(approved=APPROVED, checkpoint_state=AFTER_EXECUTOR, db_attempt=1)
    first, second = _crash_result(facts).to_dict(), _crash_result(facts).to_dict()
    for d in (first, second):
        d.pop("finalized_at")          # the only field that is a wall-clock stamp
    assert first == second


def test_the_checkpoint_outranks_a_stale_attempt_counter():
    # a stale row must not drag the recorded attempt backwards
    facts = merge_durable_facts(approved=APPROVED,
                                checkpoint_state={"executor_success": True, "retry_count": 3},
                                db_attempt=1)
    assert facts["attempts"] == 3


@pytest.mark.parametrize("bad", [None, "2", -1, True, {}])
def test_a_malformed_attempt_count_falls_back_conservatively(bad):
    facts = merge_durable_facts(approved=APPROVED,
                                checkpoint_state={"executor_success": True, "retry_count": bad},
                                db_attempt=None)
    assert facts["attempts"] == 0


def test_state_engine_outranks_the_approved_payload_when_both_exist():
    # engine_select may legitimately resolve an engine the payload left unset
    facts = merge_durable_facts(approved={"mesh_engine": ""},
                                checkpoint_state={"engine": "cfmesh"})
    assert facts["engine"] == "cfmesh"


def test_non_string_state_values_never_reach_the_verdict():
    facts = merge_durable_facts(approved={"mesh_engine": 123},
                                checkpoint_state={"engine": None, "purpose": ["x"]})
    assert facts["engine"] == "" and facts["purpose"] == ""


# the crash path uses this, and does not hardcode

def test_the_crash_path_builds_from_durable_facts():
    # The crash verdict is built by terminal_finalize.finalize_crash now, not inline in
    # _run_async. The guarantees are unchanged and asserted against the authority that owns them.
    # CODE only: both the comments AND the docstrings quote the old defect on purpose, so the
    # prose is stripped rather than scanned - otherwise the guard fires on its own explanation.
    import ast
    import inspect
    import textwrap

    import meshpipeline.application.terminal_finalize as tf

    def _code(fn):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    ast.get_docstring(node) is not None:
                node.body = node.body[1:]
        return ast.unparse(tree)

    crash = _code(tf.finalize_crash) + "\n" + _code(tf.durable_facts_after_crash)
    assert "merge_durable_facts" in crash, "the crash verdict no longer consults durable facts"
    for hardcoded in ('engine=""', 'purpose=""', "executor_success=False", "attempts=0"):
        assert hardcoded not in crash, (
            f"the crash verdict hardcodes {hardcoded} instead of reporting what is durable")
    # delivery is still never assumed on a crash
    assert "delivered_types=[]" in crash
