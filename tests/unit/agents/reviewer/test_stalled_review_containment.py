# Responsibility: Verify a stalled review ends with no verdict, blames the application, and leaks no prompt text.
from __future__ import annotations

import asyncio
import json

import pytest

from meshpipeline.agents.loop import diagnostics
from meshpipeline.agents.reviewer.loop_policy import REVIEWER_NO_PROGRESS_THRESHOLD
from meshpipeline.agents.reviewer.unified import run_unified_review
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest

# REAL, from the sanitized recording: attempt 2's tool sequence, in order
RECORDED_SEQUENCE = [
    "go_to_coordinates", "zoom_to_region", "inspect_region",
    "set_camera_preset", "set_camera_preset", "set_camera_preset", "set_camera_preset",
    "submit_findings",
    "inspect_region", "inspect_region", "inspect_region", "inspect_region",
    "submit_findings",
    "toggle_patch",
    "submit_findings", "submit_findings", "submit_findings", "submit_findings",
    "submit_findings", "submit_findings", "submit_findings",
]
RECORDED_ROUNDS = 30
RECORDED_NAVIGATION = 12
RECORDED_SUBMISSIONS = 9

# FIXTURE FACTS: the sanitized recording does not preserve per-axis deficits
# The engine ∪ purpose rubric for snappy/external_cfd composes six axes; the fixture submits
# findings for none of them, which is the "every axis missing, unchanged" condition the recorded
# nine identical rejections demonstrate. Axis NAMES are real; their per-round status is fixture.
FIXTURE_AXES = ("surface_capture", "prism_layer_coverage", "feature_refinement_transition",
                "farfield_clearance", "domain_enclosure", "wake_resolution")


class _Ax:
    def __init__(self, name):
        self.name = name
        self.requires = ()


class _Plan:
    engine = "snappy"
    purpose = "external_cfd"
    required_gate_keys = frozenset()
    required_metric_keys = frozenset()
    required_render_artifacts = frozenset()
    required_targets = ()

    def __init__(self):
        self.axes = tuple(_Ax(a) for a in FIXTURE_AXES)


class _Opening:
    has_geometry = True
    initial_screenshot_b64 = ""
    inspection_targets = ()
    nav_context = ""
    patch_colour_legend = ()
    patch_views = ()


class _Runtime:

    def __init__(self):
        self._n = 0

    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import TypedToolResult
        from meshpipeline.contracts.review_evidence import EvidenceItem
        self._n += 1
        return TypedToolResult(
            content=f"{fn} rendered.",
            evidence=EvidenceItem(evidence_id="", seq=self._n, label=fn, purpose="inspect",
                                  image_ref=f"/tmp/frame-{self._n}.png", view_id=f"v{self._n}",
                                  artifact_key="mesh_paths.surface"),
            is_viewer_tool=True)


def _call(name):
    if name == "submit_findings":
        return ToolCallRequest(id="c-sub", name=name, arguments=json.dumps(
            {"axis_findings": [], "rebuild_required": False, "reasoning": "reviewed"}))
    return ToolCallRequest(id=f"c-{name}", name=name, arguments="{}")


@pytest.fixture
def replay(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(diagnostics, "emit",
                        lambda rec: records.append(diagnostics.sanitized(rec)) or records[-1])
    served: list[str] = []

    async def provider(**kw):
        name = RECORDED_SEQUENCE[len(served)] if len(served) < len(RECORDED_SEQUENCE) \
            else "submit_findings"
        served.append(name)
        return ModelRoundResult(tool_calls=(_call(name),), finish_reason="tool_calls")

    out = asyncio.run(run_unified_review(
        plan=_Plan(), ledger=EvidenceLedger(), runtime=_Runtime(), opening=_Opening(),
        system_prompt="s", opening_context_text="c", provider_call=provider,
        max_rounds=RECORDED_ROUNDS, total_timeout_s=1800.0, job_id="job-dpw4-replay", attempt=2))
    return out, records[0], served


# containment


def test_a_stalled_review_ends_early_through_no_progress(replay):
    out, rec, _served = replay
    assert out.llm_rounds < RECORDED_ROUNDS, (
        f"the recorded run burned all {RECORDED_ROUNDS} rounds; the migrated review must not")
    assert rec["exit"] == "no_progress", "the loop ended for some other reason"
    assert rec["tally"]["consecutive_no_progress"] == REVIEWER_NO_PROGRESS_THRESHOLD
    assert rec["tally"]["rounds"] == out.llm_rounds


def test_a_stalled_review_produces_no_verdict_of_either_polarity(replay):
    out, rec, _served = replay
    assert out.verdict == "" and out.verdict not in ("PASS", "FAIL")
    assert out.findings == () and out.rebuild_required is False
    assert "accepted_verdict" not in rec["extension"]


def test_a_stalled_review_is_classified_as_an_application_side_failure(replay):
    from meshpipeline.errors import FailureClass, classify_api_failure
    out, rec, _served = replay
    assert out.api_failure == "reviewer_evidence_missing"
    assert rec["failure_marker"] == out.api_failure
    fc = classify_api_failure(out.api_failure)
    assert fc is FailureClass.REVIEW_EVIDENCE_MISSING and fc.is_system


def test_the_user_is_told_the_review_failed_not_that_the_mesh_was_rejected(replay):
    from meshpipeline.application.final_result import (
        FailureCategory,
        ReviewExecution,
        TerminalStatus,
        build_final_result,
        render_message,
    )
    out, _rec, _served = replay
    fr = build_final_result(
        job_id="job-stalled-review", owner_id="o", status=TerminalStatus.failed, engine="snappy",
        purpose="external_cfd", dimensionality="3D", approved_snapshot_id="",
        executor_success=True,
        reviewer_verdict="FAIL",              # attempt 1's RETAINED verdict, exactly as recorded
        failed_gate="", api_failure=out.api_failure, attempts=2, attempts_max=5,
        required_ready=False, delivered_types=[], optional_warnings=[])
    assert fr.review_execution is ReviewExecution.failed_to_complete
    assert fr.reviewer_verdict is None
    assert fr.outcome_code == FailureCategory.internal_pipeline_failure.value
    assert "did not pass review" not in render_message(fr)
    assert "on our side" in render_message(fr)


def test_the_durable_record_names_what_was_never_resolved(replay):
    _out, rec, _served = replay
    ext = rec["extension"]
    assert set(ext["required_axes"]) == set(FIXTURE_AXES)
    assert set(ext["missing_axes"]) == set(FIXTURE_AXES), "every unresolved axis must be named"
    assert ext["covered_axes"] == ()
    assert ext["submissions"] >= 3 and ext["eligibility_rejections"] == ext["submissions"]
    assert any("surface_capture" in r for r in ext["rejection_reasons"]), \
        "eligibility's machine-readable reasons must be durable"
    assert ext["newly_usable_evidence"] == 0 and ext["became_usable_evidence"] == 0
    assert ext["new_evidence_since_last_submission"] is False
    assert rec["calls_by_category"].get("submission", 0) >= 3


def test_the_durable_record_leaks_no_prompt_or_reasoning(replay):
    _out, rec, _served = replay
    blob = json.dumps(rec)
    for banned in ("You are a mesh-quality reviewer", "reviewed", "system_prompt"):
        assert banned not in blob


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
