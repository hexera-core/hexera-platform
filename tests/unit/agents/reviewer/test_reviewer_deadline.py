# Responsibility: Verify a hung or exhausted review is capped, yields no pass, and is not a provider failure.
from __future__ import annotations

import asyncio

import pytest

import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.agents.reviewer.unified import run_unified_review
from meshpipeline.contracts.evidence_ledger import EvidenceLedger


class _Ax:
    def __init__(self, name):
        self.name = name
        self.requires = ()


class _Plan:
    engine = "cfmesh"
    purpose = ""
    required_gate_keys = frozenset()
    required_metric_keys = frozenset()
    required_render_artifacts = frozenset()
    required_targets = ()

    def __init__(self):
        self.axes = (_Ax("connectivity"),)


class _Opening:
    has_geometry = True
    initial_screenshot_b64 = ""     # no opening image -> no render_view seeded
    inspection_targets = ()


class _Runtime:
    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        return TypedToolResult(content=NOT_VIEWER_TOOL, evidence=None, is_viewer_tool=False)


def _pass_response():
    from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
    return ModelRoundResult(
        tool_calls=(ToolCallRequest(id="c1", name="submit_findings",
                                    arguments='{"axis_findings": [], "rebuild_required": false,'
                                              ' "reasoning": "r"}'),),
        finish_reason="tool_calls")


def _drive(provider, *, total_timeout_s):
    return asyncio.run(run_unified_review(
        plan=_Plan(), ledger=EvidenceLedger(), runtime=_Runtime(), opening=_Opening(),
        system_prompt="s", opening_context_text="c", provider_call=provider,
        max_rounds=30, total_timeout_s=total_timeout_s))


def test_zero_budget_exhausts_before_any_provider_call():
    calls: list[int] = []

    async def provider(**kw):
        calls.append(1)
        return _pass_response(), None

    out = _drive(provider, total_timeout_s=0.0)
    assert calls == [], "a round started after the budget was already exhausted"
    assert out.verdict == "" and out.api_failure == "reviewer_exhausted"
    assert out.failure_class == "reviewer_deadline_exhausted"


def test_a_hung_provider_call_is_capped_and_never_yields_a_pass():
    async def hung(**kw):
        await asyncio.sleep(30)          # far past the tiny budget - wait_for cancels this
        return _pass_response(), None

    out = _drive(hung, total_timeout_s=0.02)
    assert out.verdict == "" and out.api_failure == "reviewer_exhausted"
    assert out.failure_class == "reviewer_deadline_exhausted"


def test_reviewer_exhausted_is_not_a_provider_failure():
    from meshpipeline.agents.reviewer.visual import _is_provider_failure
    assert _is_provider_failure("reviewer_exhausted") is False


def test_total_timeout_setting_is_finite_and_positive():
    assert isinstance(rcfg.REVIEWER_TOTAL_TIMEOUT_SECONDS, int)
    assert rcfg.REVIEWER_TOTAL_TIMEOUT_SECONDS > 0
    # far below the pathological REVIEWER_MAX_ROUNDS * provider-timeout the deadline replaces
    assert rcfg.REVIEWER_TOTAL_TIMEOUT_SECONDS < rcfg.REVIEWER_MAX_ROUNDS * 1800


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
