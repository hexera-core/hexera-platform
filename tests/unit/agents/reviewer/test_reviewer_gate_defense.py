# Responsibility: Verify the reviewer refuses to assess unless the executor genuinely succeeded and its gates passed.
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.agents.reviewer.eligibility import Eligibility, evaluate_eligibility
from meshpipeline.contracts.evidence_ledger import EvidenceLedger, EvidenceStatus

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


REPO = Path(__file__).parents[4]


# node-level: refuse to assess without an explicit executor_success
def _drive(monkeypatch, tmp_path, **overrides):
    # node_reviewer no longer writes a TrainingLogger event of its own - the canonical run
    # record replaced it - so the diagnostic sink is silenced at its source instead.
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger",
                        lambda *a, **k: types.SimpleNamespace(log=lambda *a, **k: None))
    state = {
        "job_id": "j", "engine": "cfmesh", "purpose": "", "openfoam_workspace": str(tmp_path),
        "mesh_manifest": {}, "engine_params": {}, "retry_count": 0, "geometry_source": _GEOMETRY_SOURCE,
        "request_txt": "r", "review_brief_txt": "b", "user_id": "u",
    }
    state.update(overrides)
    return asyncio.run(visual.node_reviewer(state))


def _drive_with_assessment_spy(monkeypatch, tmp_path, **overrides):
    reached: list[int] = []
    orig = visual.validate_plan
    monkeypatch.setattr(visual, "validate_plan", lambda *a, **k: (reached.append(1), orig(*a, **k))[1])
    out = _drive(monkeypatch, tmp_path, **overrides)
    return out, reached


def test_refuses_before_assessing_when_executor_success_is_absent(monkeypatch, tmp_path):
    out, reached = _drive_with_assessment_spy(monkeypatch, tmp_path)   # no executor_success
    assert out.get("api_failure") == "reviewer_evidence_missing"
    assert "reviewer_verdict" not in out
    assert reached == [], "node proceeded to assess a mesh that was never validated"


def test_refuses_before_assessing_when_executor_success_is_false(monkeypatch, tmp_path):
    out, reached = _drive_with_assessment_spy(monkeypatch, tmp_path, executor_success=False)
    assert out.get("api_failure") == "reviewer_evidence_missing"
    assert reached == []


@pytest.mark.parametrize("truthy", [1, "yes", [1], {"a": 1}])
def test_a_truthy_but_non_true_executor_success_does_not_satisfy_the_check(monkeypatch, tmp_path, truthy):
    out, reached = _drive_with_assessment_spy(monkeypatch, tmp_path, executor_success=truthy)
    assert out.get("api_failure") == "reviewer_evidence_missing"
    assert reached == []


# eligibility-level: gate evidence can never be synthesized/missing/wrong-engine and PASS
class _Plan:
    required_metric_keys = frozenset()
    required_render_artifacts = frozenset()
    required_targets = ()
    axes = ()

    def __init__(self, gates=frozenset()):
        self.required_gate_keys = gates


def test_missing_required_gate_blocks_pass():
    d = evaluate_eligibility(_Plan(gates=frozenset({"rc"})), EvidenceLedger(), ())
    assert d.outcome is Eligibility.REJECT
    assert any("gate 'rc'" in r for r in d.reasons)


def test_errored_gate_cannot_pass():
    L = EvidenceLedger()
    L.add_gate("rc", EvidenceStatus.ERROR, "could not evaluate", "executor")
    d = evaluate_eligibility(_Plan(gates=frozenset({"rc"})), L, ())
    assert d.outcome is Eligibility.REJECT


def test_unavailable_gate_cannot_pass():
    L = EvidenceLedger()
    L.add_gate("rc", EvidenceStatus.UNAVAILABLE, "no result", "executor")
    d = evaluate_eligibility(_Plan(gates=frozenset({"rc"})), L, ())
    assert d.outcome is Eligibility.REJECT


def test_a_gate_from_another_engine_does_not_satisfy_the_required_gate():
    L = EvidenceLedger()
    L.add_gate("some_other_engine_gate", EvidenceStatus.PASS, "pass", "executor")
    d = evaluate_eligibility(_Plan(gates=frozenset({"rc"})), L, ())
    assert d.outcome is Eligibility.REJECT
    assert any("gate 'rc'" in r for r in d.reasons)


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
