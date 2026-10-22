# Responsibility: Prove review state is carried to the mesh and attempt it belongs to, never further.
# Boundaries: the builder's turn patch, the reviewer's submission parse and the classifier's branch
# choice; the graph-level rerun-then-rebuild run is in tests/integration/test_graph_runtime.py.
# Found by the 2026-10-19 reviewer-path audit:
#   - the review rerun count was job-wide, so a freshly rebuilt mesh was announced as "reviewing the
#     same mesh again" and its never-judge-the-same-mesh-twice reuse was switched off;
#   - rebuild_required="false" (a string) read as True and sent a targeted retry back to TOPOLOGY;
#   - the reviewer-feedback retry on a caveated delivery candidate handed the builder the gate's
#     near-miss text instead of the change the review asked for.
from __future__ import annotations

import asyncio
import types

import pytest

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.agents.builder.turn_patch import TurnPatch
from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
from meshpipeline.contracts.agent_loop import LoopLimits
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.pipeline.classifier import node_classifier


def _turn(**kw) -> dict:
    return TurnPatch(workspace="/w", request_txt="r", review_brief_txt="b", deadline_epoch=1.0,
                     retry_count=2, **kw).state()


# P7: the rerun count belongs to one mesh

def test_a_new_attempt_starts_its_review_with_no_reruns():
    assert _turn()["review_rerun_count"] == 0


@pytest.mark.parametrize("kw", [{"api_failure": "openai: 500"}, {"stop": "reviewed_case_repeats"}])
def test_a_turn_that_built_nothing_leaves_the_count_alone(kw):
    assert "review_rerun_count" not in _turn(**kw)


# P5: rebuild_required is a literal boolean, like `passed`

AXES = ("surface_capture", "wake_resolution")


def _policy():
    plan = types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="engine:snappy", requires=())
                   for n in AXES),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset(), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())
    return ReviewLoopPolicy(plan=plan, ledger=EvidenceLedger(), runtime=None,
                            limits_=LoopLimits(max_rounds=30, no_progress_threshold=3))


def _submit(policy, rebuild):
    args = {"axis_findings": [{"axis_key": a, "finding": "fine", "evidence_ids": ["v-001"],
                               "passed": True} for a in AXES], "reasoning": "r"}
    if rebuild is not _ABSENT:
        args["rebuild_required"] = rebuild
    inv = ToolInvocation(round_index=1, call_index=1, tool="submit_findings",
                         raw_arguments="{}", parsed=args)
    return asyncio.run(policy.execute(inv))


_ABSENT = object()


@pytest.mark.parametrize("rebuild", ["false", "true", "no", 0, 1])
def test_a_non_boolean_rebuild_required_is_corrected_not_guessed(rebuild):
    p = _policy()
    out = _submit(p, rebuild)
    assert out.accepted is False and "rebuild_required" in out.content
    assert p.malformed_submissions == 1 and p.accepted_rebuild_required is False


@pytest.mark.parametrize("rebuild", [False, None, _ABSENT])
def test_false_or_left_out_is_no_rebuild(rebuild):
    p = _policy()
    out = _submit(p, rebuild)
    assert p.malformed_submissions == 0, out.content
    assert p.accepted_rebuild_required is False


# P6: the review of THIS attempt wins over the near-miss diagnostic

CAVEAT = {"kind": "domain_extent", "direction": "downstream", "requested": 10.0, "measured": 9.6}
FINDING = {"axis_key": "prism_layer_coverage", "passed": False, "improve": True, "blocking": "",
           "builder_change": "layers", "change_request": "add 3 layers on the wing",
           "brief_requirement": "y+ < 1 on the wing"}


def _caveated(retry_count: int, history: list) -> dict:
    return {"job_id": "j", "engine": "snappy", "purpose": "", "executor_success": True,
            "retry_count": retry_count, "requirement_caveats": [CAVEAT],
            "executor_output": "[DOMAIN_EXTENT] downstream 9.6 of 10 reference lengths",
            "reviewer_verdict": "FAIL",
            "reviewer_feedback": "The review asks for these changes:\n- [layers] add 3 layers",
            "reviewer_axis_findings": [FINDING], "review_history": history}


@pytest.fixture(autouse=True)
def _no_training_log(monkeypatch):
    class _TL:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr("meshpipeline.pipeline.classifier.TrainingLogger", _TL)


def test_the_reviewer_feedback_retry_on_a_caveated_mesh_hands_the_builder_the_review():
    n = bcfg.MAX_BUILDER_RETRIES + 1
    out = asyncio.run(node_classifier(_caveated(n, [
        {"attempt": n, "verdict": "FAIL", "findings": [FINDING], "evidence_key": "k"}])))
    r = out["classifier_result"]
    assert r["error_source"] == "reviewer_fail"
    assert r["failed_axes"] == ["prism_layer_coverage"]
    assert "add 3 layers" in r["summary"]


def test_a_fail_left_from_an_earlier_attempt_does_not_replace_the_near_miss():
    out = asyncio.run(node_classifier(_caveated(2, [
        {"attempt": 1, "verdict": "FAIL", "findings": [FINDING], "evidence_key": "k"}])))
    r = out["classifier_result"]
    assert r["error_source"] == "requirements_near_miss"
    assert r["summary"].startswith("[DOMAIN_EXTENT]")
