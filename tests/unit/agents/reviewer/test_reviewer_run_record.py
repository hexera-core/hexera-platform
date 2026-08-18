# Responsibility: Verify the record agrees with the outcome's own counts, names every rejection, defaults no verdict.
from __future__ import annotations

import asyncio
import json

import pytest

from meshpipeline.agents.loop import diagnostics
from meshpipeline.agents.reviewer.unified import run_unified_review
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest


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

    def __init__(self, *axes):
        self.axes = tuple(_Ax(a) for a in (axes or ("surface_capture",)))


class _Opening:
    has_geometry = True
    initial_screenshot_b64 = ""
    inspection_targets = ()
    nav_context = ""
    patch_colour_legend = ()
    patch_views = ()


class _Runtime:
    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        return TypedToolResult(content=NOT_VIEWER_TOOL, evidence=None, is_viewer_tool=False)


def _tc(name, **args):
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=json.dumps(args))


def _round(*calls, text=""):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason="tool_calls" if calls else "stop")


@pytest.fixture
def captured(monkeypatch):
    out: list[dict] = []

    def _spy(record):
        payload = diagnostics.sanitized(record)
        out.append(payload)
        return payload
    # the canonical loop imports `emit` at call time, so patching the module attribute is enough
    monkeypatch.setattr(diagnostics, "emit", _spy)
    return out


def _drive(script, *, plan=None, max_rounds=6, attempt=0):
    it = iter(script)

    async def provider(**kw):
        try:
            return next(it)
        except StopIteration:
            return _round(text="thinking")     # no tool call -> the loop nudges

    return asyncio.run(run_unified_review(
        plan=plan or _Plan(), ledger=EvidenceLedger(), runtime=_Runtime(), opening=_Opening(),
        system_prompt="s", opening_context_text="c", provider_call=provider,
        max_rounds=max_rounds, total_timeout_s=30.0, job_id="job-rec", attempt=attempt))


# a record on EVERY exit, not just PASS
def test_a_non_verdict_exit_still_emits_a_record(captured):
    out = _drive([_round(_tc("submit_findings", axis_findings=[], rebuild_required=False,
                             reasoning="r"))], max_rounds=4)
    assert out.verdict == "" and out.api_failure          # the review did NOT conclude
    assert len(captured) == 1, "the failure path must record as much as the success path"
    assert captured[0]["failure_marker"] == out.api_failure


def test_a_provider_failure_exit_emits_a_record(captured):
    async def provider(**kw):
        return ModelRoundResult(failure_marker="<<API_FAILURE:reviewer_rate_limit>>")
    out = asyncio.run(run_unified_review(
        plan=_Plan(), ledger=EvidenceLedger(), runtime=_Runtime(), opening=_Opening(),
        system_prompt="s", opening_context_text="c", provider_call=provider,
        max_rounds=3, total_timeout_s=30.0, job_id="job-rec"))
    assert out.api_failure and captured[0]["exit"] == "provider_failed"


def test_a_round_exhausted_exit_names_itself(captured):
    _drive([], max_rounds=2)
    assert captured[0]["exit"] == "rounds_exhausted"


# dual-emission parity with the old counters
def test_the_record_agrees_with_the_outcomes_own_round_and_call_counts(captured):
    out = _drive([_round(_tc("zoom")),
                  _round(_tc("inspect_region", region_name="wing")),
                  _round(_tc("submit_findings", axis_findings=[], rebuild_required=False,
                             reasoning="r"))], max_rounds=6)
    tally = captured[0]["tally"]
    assert tally["rounds"] == out.llm_rounds
    assert tally["tool_calls"] == out.tool_calls
    assert tally["rounds"] == 6 and tally["tool_calls"] == 3


def test_plain_text_rounds_are_counted_by_both(captured):
    _drive([_round(text="I am thinking"), _round(text="still thinking")], max_rounds=3)
    assert captured[0]["tally"]["plaintext_turns"] == 3


def test_navigation_is_counted_in_the_reviewers_own_vocabulary(captured):
    _drive([_round(_tc("zoom")), _round(_tc("inspect_region", region_name="w")),
            _round(_tc("submit_findings", axis_findings=[], rebuild_required=False,
                       reasoning="r"))], max_rounds=4)
    cats = captured[0]["calls_by_category"]
    assert cats["navigation"] == 2 and cats["submission"] == 1
    assert "verdict_protocol" not in cats, "there is no separate verdict protocol any more"


# the diagnostics the incident needed and never had
def test_every_eligibility_rejection_and_its_reasons_are_recorded(captured):
    submit = _tc("submit_findings", axis_findings=[], rebuild_required=False, reasoning="r")
    _drive([_round(submit),
            _round(submit), _round(submit)], max_rounds=8)
    ext = captured[0]["extension"]
    assert ext["submissions"] == 3
    assert ext["eligibility_rejections"] == 3
    assert ext["rejection_reasons"], "eligibility's machine-readable reasons must survive"
    assert any("surface_capture" in r for r in ext["rejection_reasons"])


def test_missing_axes_are_named(captured):
    _drive([_round(_tc("submit_findings", axis_findings=[], rebuild_required=False,
                       reasoning="r"))],
           plan=_Plan("surface_capture", "wake_resolution"), max_rounds=4)
    ext = captured[0]["extension"]
    assert set(ext["required_axes"]) == {"surface_capture", "wake_resolution"}
    assert set(ext["missing_axes"]) == {"surface_capture", "wake_resolution"}
    assert ext["covered_axes"] == ()


def test_an_unchanged_resubmission_is_visible_as_no_progress(captured):
    submit = _tc("submit_findings", axis_findings=[], rebuild_required=False, reasoning="r")
    _drive([_round(submit), _round(submit), _round(submit)], max_rounds=8)
    assert captured[0]["tally"]["consecutive_no_progress"] >= 2
    assert captured[0]["extension"]["last_submission_changed"] is False


def test_the_attempt_number_is_recorded_so_attempts_stay_distinguishable(captured):
    _drive([], max_rounds=2, attempt=2)
    assert captured[0]["pipeline_attempt"] == 2 and captured[0]["agent_attempt"] == 2


def test_no_verdict_is_ever_defaulted_in_the_record(captured):
    _drive([], max_rounds=2)
    assert "accepted_verdict" not in captured[0]["extension"]


# observation changes nothing
def test_the_limits_recorded_are_exactly_what_the_review_already_enforced(captured):
    _drive([], max_rounds=5)
    limits = captured[0]["limits"]
    assert limits["max_rounds"] == 5                     # the value it was ALREADY given
    assert limits["total_timeout_s"] == 30.0             # the budget it ALREADY settled
    # ACTIVATED by this cutover - and only this one.
    assert limits["no_progress_threshold"] == 3
    for unset in ("warn_at_remaining_rounds", "closing_at_remaining_rounds"):
        assert limits[unset] is None, f"{unset} must stay unset - it needs measured evidence"


def test_a_broken_diagnostic_path_never_changes_the_review(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("diagnostics down")
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _boom)
    out = _drive([_round(_tc("zoom"))], max_rounds=3)
    assert out.api_failure and out.verdict == ""         # unchanged, truthful non-verdict


def test_the_review_still_produces_only_pass_fail_or_nothing(captured):
    out = _drive([_round(_tc("submit_findings", axis_findings=[], rebuild_required=False,
                             reasoning="r"))], max_rounds=4)
    assert out.verdict in ("", "PASS", "FAIL")
    assert bool(out.verdict) != bool(out.api_failure)


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
