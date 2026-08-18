# Responsibility: Verify the accountant counts a round the same whatever it held, and keeps only scalars in the record.
from __future__ import annotations

import dataclasses

import pytest

from meshpipeline.agents.loop import progress
from meshpipeline.agents.loop.accounting import (
    UNCATEGORIZED,
    AgentRunAccountant,
    ToolInvocation,
)
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    ProgressObservation,
    ToolCallRecord,
)
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


class _Ext:
    def sanitized(self):
        return {"kind": "test"}


def _acct(**kw):
    kw.setdefault("role", AgentRole.reviewer)
    kw.setdefault("job_id", "job-1")
    kw.setdefault("limits", LoopLimits())
    return AgentRunAccountant(**kw)


def _round(*, calls=(), text="", finish="stop", attempts=1, tin=0, tout=0, cached=0, marker=""):
    return ModelRoundResult(
        tool_calls=tuple(calls), assistant_text=text, finish_reason=finish,
        input_tokens=tin, cached_input_tokens=cached, output_tokens=tout,
        provider=ProviderAttemptInfo(attempts=attempts, provider="p", model="m"),
        failure_marker=marker)


def _req(name, args="{}"):
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=args)


def test_a_text_only_round_counts_exactly_like_a_ten_call_round():
    a, b = _acct(), _acct()
    a.record_round(_round(text="thinking"))
    b.record_round(_round(calls=[_req(f"t{i}") for i in range(10)], finish="tool_calls"))
    assert a.tally().rounds == b.tally().rounds == 1
    assert a.tally().tool_calls == b.tally().tool_calls == 0    # calls counted on dispatch


def test_plain_text_turns_are_counted_separately():
    a = _acct()
    a.record_round(_round(text="just talking"))
    a.record_round(_round(calls=[_req("zoom")], finish="tool_calls"))
    a.record_round(_round(text=""))
    assert a.tally().plaintext_turns == 2


def test_a_failed_round_is_not_counted_as_a_plain_text_turn():
    a = _acct()
    a.record_round(_round(marker="<<API_FAILURE:reviewer_timeout>>"))
    assert a.tally().rounds == 1 and a.tally().plaintext_turns == 0


def test_finish_reason_and_tokens_are_recorded_per_round():
    a = _acct()
    a.record_round(_round(finish="length", tin=100, tout=7, cached=64))
    r = a.report(exit=LoopExit.rounds_exhausted, extension=_Ext()).rounds[0]
    assert r.finish_reason == "length"
    assert (r.input_tokens, r.cached_input_tokens, r.output_tokens) == (100, 64, 7)


# transient vs durable tool data
def test_a_tool_call_becomes_seven_scalars_and_the_payload_is_dropped():
    a = _acct()
    inv = ToolInvocation.of(_req("go_to_coordinates", '{"x": 1, "secret": "sk-live-xyz"}'),
                            round_index=1, call_index=1, parsed={"x": 1, "secret": "sk-live-xyz"})
    inv.result = {"image_b64": "AAAABBBB", "note": "a whole screenshot"}
    rec = a.record_tool_call(inv, category="navigation")
    assert isinstance(rec, ToolCallRecord)
    blob = repr(rec)
    assert "sk-live-xyz" not in blob and "AAAABBBB" not in blob
    assert rec.tool == "go_to_coordinates" and rec.category == "navigation"
    assert rec.malformed is False and rec.accepted is True


def test_the_transient_type_is_never_part_of_the_durable_record():
    a = _acct()
    a.record_tool_call(ToolInvocation.of(_req("zoom"), round_index=1, call_index=1, parsed={}),
                       category="navigation")
    record = a.report(exit=LoopExit.terminal_action, extension=_Ext())
    for t in record.tool_calls:
        assert not isinstance(t, ToolInvocation)
        assert not hasattr(t, "raw_arguments") and not hasattr(t, "result")


def test_genuinely_empty_arguments_are_not_malformed():
    a = _acct()
    inv = ToolInvocation.of(_req("reset_view", ""), round_index=1, call_index=1, parsed=None)
    assert a.record_tool_call(inv, category="navigation").malformed is False


def test_an_uncategorized_call_still_counts():
    a = _acct()
    a.record_tool_call(ToolInvocation.of(_req("x"), round_index=1, call_index=1, parsed={}))
    assert {c.category for c in a.tally().calls_by_category} == {UNCATEGORIZED}


# progress counting (generic)
def test_progress_resets_the_no_progress_streak():
    a = _acct()
    a.record_progress(ProgressObservation(made_progress=False, signature="s1"))
    a.record_progress(ProgressObservation(made_progress=False, signature="s1"))
    assert a.tally().consecutive_no_progress == 2
    a.record_progress(ProgressObservation(made_progress=True, signature="s2"))
    assert a.tally().consecutive_no_progress == 0 and a.tally().progress_count == 1


def test_being_stuck_a_NEW_way_starts_a_new_streak():
    a = _acct()
    a.record_progress(ProgressObservation(made_progress=False, signature="s1"))
    a.record_progress(ProgressObservation(made_progress=False, signature="s1"))
    a.record_progress(ProgressObservation(made_progress=False, signature="s2"))
    assert a.tally().consecutive_no_progress == 1


def test_the_stall_threshold_is_not_enforced_when_unset():
    assert progress.stalled(999, LoopLimits()) is False
    assert progress.stalled(2, LoopLimits(no_progress_threshold=3)) is False
    assert progress.stalled(3, LoopLimits(no_progress_threshold=3)) is True


# the frozen report
def test_the_report_freezes_everything_and_names_the_exit():
    a = _acct(pipeline_attempt=2, agent_attempt=2)
    a.record_round(_round(calls=[_req("zoom")], finish="tool_calls"))
    a.record_tool_call(ToolInvocation.of(_req("zoom"), round_index=1, call_index=1, parsed={}),
                       category="navigation")
    rec = a.report(exit=LoopExit.rounds_exhausted, extension=_Ext(),
                   failure_marker="reviewer_evidence_missing")
    assert rec.role is AgentRole.reviewer and rec.job_id == "job-1"
    assert rec.pipeline_attempt == 2 and rec.agent_attempt == 2
    assert rec.exit is LoopExit.rounds_exhausted
    assert rec.failure_marker == "reviewer_evidence_missing"
    assert isinstance(rec.rounds, tuple) and isinstance(rec.tool_calls, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.failure_marker = "something else"


def test_a_tally_is_a_reading_not_the_live_counter():
    a = _acct()
    first = a.tally()
    a.record_round(_round())
    assert first.rounds == 0 and a.tally().rounds == 1


def test_terminal_action_completion_is_recorded():
    a = _acct()
    assert a.terminal_action_done is False
    a.record_terminal_action()
    assert a.terminal_action_done is True


# no schema was touched
def test_the_state_carries_append_only_agent_run_history():
    import typing

    from meshpipeline.contracts import pipeline_state as ps
    assert ps.STATE_SCHEMA_VERSION == 9
    hints = typing.get_type_hints(ps.PipelineState, include_extras=True)
    import operator
    assert operator.add in hints["agent_run_records"].__metadata__, "history must be append-only"
