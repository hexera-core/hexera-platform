# Responsibility: Verify a submission is authorised by recorded state, never by prose, and one response is one round.
# Boundaries: a recommendation blocks submission for the rest of the invocation, by state rather than prompt wording.
from __future__ import annotations

import asyncio
import json
import types

import pytest

from meshpipeline.agents.intake.executor import IntakeExecutionState, IntakeToolExecutor
from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy
from meshpipeline.contracts.agent_loop import LoopExit, LoopLimits, LoopTally
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


def _tc(name, raw=None, **args):
    return ToolCallRequest(id=f"c-{name}", name=name,
                           arguments=raw if raw is not None else json.dumps(args))


def _round(*calls, text="", marker=""):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason="tool_calls" if calls else "stop",
                            provider=ProviderAttemptInfo(1, "p", "m"), failure_marker=marker)


class _Harness:
    def __init__(self):
        self.records: list = []
        self.dispatched: list[str] = []


@pytest.fixture
def h(monkeypatch):
    t = _Harness()
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: t.records.append(rec) or {})
    return t


def _state(**over):
    st = IntakeExecutionState(session_id="s1", owner_id="u1", revision="r1", user_msg_count=1,
                              latest_user_msg="hello", rec_authorized=True)
    for k, v in over.items():
        setattr(st, k, v)
    return st


def _policy_for(state, monkeypatch, *, engines=("snappy", "cfmesh")):
    ex = IntakeToolExecutor(state=state, job_id="j", implemented_engines=list(engines),
                            search_tool=lambda *a, **k: "search result")
    return IntakeLoopPolicy(exec_state=state, executor=ex,
                            limits_=LoopLimits(max_rounds=20))


def _drive(policy, script, h, monkeypatch):
    it = iter(script)

    async def _provider(**_kw):
        try:
            return next(it)
        except StopIteration:
            return _round(text="anything else?")

    from meshpipeline.agents.loop.runner import run_agent_loop
    return asyncio.run(run_agent_loop(
        driver=policy, provider_call=_provider, messages=[{"role": "system", "content": "s"}],
        tools=[], job_id="j", on_round=policy.note_round,
        append_tool_result=lambda m, cid, c: m.append(
            {"role": "tool", "tool_call_id": cid, "content": c}),
        record_sink=h.records.append))


def test_one_response_is_one_round_and_every_call_is_counted(h, monkeypatch):
    st = _state(rec_authorized=False)
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("web_search", query="a"), _tc("web_search", query="b"))], h, monkeypatch)
    tally = h.records[-1].tally
    assert tally.rounds <= 2 and tally.tool_calls >= 2


def test_calls_execute_in_provider_order(h, monkeypatch):
    st = _state(rec_authorized=False)
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("propose_engine_selection", engine="snappy"),
                      _tc("propose_engine_selection", engine="cfmesh"))], h, monkeypatch)
    # the LAST proposal in provider order is the standing one
    assert st.selection["engine"] == "cfmesh"


def test_invocation_local_state_starts_clean():
    for _ in range(2):
        st = _state()
        assert st.recommended_this_turn is False
        assert st.submit_args is None and st.approval is None


# TURN-SCOPED escalation
def test_recommendation_then_submit_in_one_response_is_refused(h, monkeypatch):
    st = _state()
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("recommend_compatible_engines", purpose="x", input_kind="y"),
                      _tc("submit_requirements", mesh_engine="snappy"))], h, monkeypatch)
    assert st.recommended_this_turn is True
    assert st.submit_args is None, "a comparison turn must never become a submission"
    assert st.submit_summary is None


def test_recommendation_in_round_one_blocks_submit_in_round_two(h, monkeypatch):
    st = _state()
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("recommend_compatible_engines", purpose="x", input_kind="y")),
               _round(_tc("submit_requirements", mesh_engine="snappy"))], h, monkeypatch)
    assert st.recommended_this_turn is True
    assert st.submit_args is None, "the block must not reset between provider rounds"
    assert st.submit_attempts == 1 and st.submit_rejections == 0


def test_before_round_does_not_clear_the_escalation_latch():
    st = _state(recommended_this_turn=True)
    p = IntakeLoopPolicy(exec_state=st, executor=None)
    p.before_round(LoopTally(rounds=1), [])
    assert st.recommended_this_turn is True, "resetting per round would re-open escalation"


def test_a_new_invocation_resets_the_block(h, monkeypatch):
    st1 = _state()
    _drive(_policy_for(st1, monkeypatch),
           [_round(_tc("recommend_compatible_engines", purpose="x", input_kind="y"))],
           h, monkeypatch)
    assert st1.recommended_this_turn is True
    st2 = _state()                      # a new user turn = a new invocation
    assert st2.recommended_this_turn is False


def test_the_block_is_enforced_by_state_not_prompt_wording(h, monkeypatch):
    st = _state(recommended_this_turn=True)
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"],
                            search_tool=lambda *a, **k: "")
    out = asyncio.run(ex.run("submit_requirements", {"mesh_engine": "snappy"}))
    assert out.accepted is False and "Not allowed in this turn" in out.content
    assert st.submit_args is None


# ordered-batch terminal resolution
def _approved_state(monkeypatch):
    st = _state(rec_authorized=False, pending={"token": "T", "canonical": {"engine": "snappy"}},
                selection={"id": "sel1", "confirmed": True, "engine": "snappy"})
    monkeypatch.setattr("meshpipeline.agents.intake.executor.validate_submission",
                        lambda a: [])
    monkeypatch.setattr("meshpipeline.agents.intake.executor.at.verify_for_submit",
                        lambda *a, **k: (True, ""))
    monkeypatch.setattr("meshpipeline.agents.intake.executor.at.CONFIRM_REQUIREMENTS_ASK",
                        "CANONICAL SUMMARY")
    monkeypatch.setattr("meshpipeline.agents.intake.executor.ap.create",
                        lambda **k: {"id": "ap1", "status": "awaiting"})
    return st


def test_an_authorized_submission_renders_the_application_summary(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    r = _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy"))], h, monkeypatch)
    assert st.submit_args is not None
    assert r.exit is LoopExit.terminal_action
    assert "CANONICAL SUMMARY" in str(r.payload)


def test_submit_then_engine_proposal_invalidates_the_approval(h, monkeypatch):
    st = _approved_state(monkeypatch)
    monkeypatch.setattr("meshpipeline.agents.intake.executor.ap.invalidate",
                        lambda a, why: {"id": "ap1", "status": "invalidated", "why": why})
    monkeypatch.setattr("meshpipeline.agents.intake.executor.es.propose",
                        lambda e, **k: {"id": "sel2", "engine": e})
    monkeypatch.setattr("meshpipeline.agents.intake.executor.es.render_selection_statement",
                        lambda e: f"Selected engine: {e}")
    p = _policy_for(st, monkeypatch)
    r = _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy"),
                          _tc("propose_engine_selection", engine="cfmesh"))], h, monkeypatch)
    assert st.submit_summary is None and st.submit_args is None, "the submission is void"
    assert st.approval["status"] == "invalidated"
    assert "Selected engine: cfmesh" in str(r.payload), "the selection prompt wins"


def test_submit_then_a_read_only_call_preserves_the_approval(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    r = _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy"),
                          _tc("web_search", query="anything"))], h, monkeypatch)
    assert st.submit_args is not None, "a read-only call changes no canonical payload"
    assert "CANONICAL SUMMARY" in str(r.payload)


def test_submit_then_a_no_op_call_preserves_the_approval(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    r = _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy"),
                          _tc("not_a_tool", x=1))], h, monkeypatch)
    assert st.submit_args is not None
    assert "CANONICAL SUMMARY" in str(r.payload)


def test_the_terminal_priority_is_admission_then_selection_then_submission(monkeypatch):
    from meshpipeline.agents.intake.loop_policy import TERMINAL_PRIORITY
    assert TERMINAL_PRIORITY == ("admission_block", "selection_prompt", "submit_summary")
    # a FRESH state each time: resolving a higher-priority terminal deliberately voids the
    # submission below it, so the same state cannot be reused to probe the next rung
    def _resolve(**kw):
        st = _state(**kw)
        return IntakeLoopPolicy(exec_state=st, executor=None).resolve_terminal(), st

    got, st = _resolve(admission_block="BLOCK", selection_prompt="SELECT",
                       submit_summary="SUMMARY")
    assert got == "BLOCK"
    assert st.submit_summary is None, "a blocked admission voids the submission beneath it"
    assert _resolve(selection_prompt="SELECT", submit_summary="SUMMARY")[0] == "SELECT"
    assert _resolve(submit_summary="SUMMARY")[0] == "SUMMARY"
    assert _resolve()[0] is None, "no terminal means the loop continues"


def test_no_further_round_follows_a_final_authorized_summary(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    r = _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy")),
                   _round(_tc("web_search", query="should never run"))], h, monkeypatch)
    assert r.exit is LoopExit.terminal_action
    assert h.records[-1].tally.rounds == 1, "the turn ended at the authorized submission"


def test_close_out_never_auto_submits():
    st = _state()
    p = IntakeLoopPolicy(exec_state=st, executor=None)
    assert asyncio.run(p.close_out(LoopTally(rounds=1))) is None
    assert st.submit_args is None and st.approval is None


# authorization
@pytest.mark.parametrize("reason", ["wrong owner", "wrong session", "wrong revision",
                                    "payload mismatch", "expired", "missing", "malformed",
                                    "invalidated"])
def test_every_token_failure_refuses_without_partial_submission(reason, monkeypatch):
    st = _state(rec_authorized=False, pending={"token": "T", "canonical": {}})
    monkeypatch.setattr("meshpipeline.agents.intake.executor.validate_submission", lambda a: [])
    monkeypatch.setattr("meshpipeline.agents.intake.executor.at.verify_for_submit",
                        lambda *a, **k: (False, reason))
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"],
                            search_tool=lambda *a, **k: "")
    out = asyncio.run(ex.run("submit_requirements", {"mesh_engine": "snappy"}))
    assert out.accepted is False and reason in out.content
    assert st.submit_args is None and st.submit_summary is None and st.approval is None
    assert st.submit_rejections == 1


def test_model_prose_never_authorizes(monkeypatch):
    st = _state(rec_authorized=False, pending=None)
    monkeypatch.setattr("meshpipeline.agents.intake.executor.validate_submission", lambda a: [])
    monkeypatch.setattr("meshpipeline.agents.intake.executor.at.verify_for_submit",
                        lambda *a, **k: (False, "no token"))
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"],
                            search_tool=lambda *a, **k: "")
    asyncio.run(ex.run("submit_requirements",
                       {"mesh_engine": "snappy", "user_said": "yes I approve"}))
    assert st.submit_args is None


def test_missing_required_values_refuse_before_the_token_is_even_consulted(monkeypatch):
    st = _state(rec_authorized=False)
    monkeypatch.setattr("meshpipeline.agents.intake.executor.validate_submission",
                        lambda a: ["domain is required"])
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"],
                            search_tool=lambda *a, **k: "")
    out = asyncio.run(ex.run("submit_requirements", {}))
    assert out.accepted is False and "required values missing" in out.content
    assert st.submit_args is None and st.val_errors_seen == ["domain"]


# plain text
def test_ordinary_plain_text_completes_the_turn(h, monkeypatch):
    st = _state(rec_authorized=False)
    p = _policy_for(st, monkeypatch)
    r = _drive(p, [_round(text="Which engine would you like?")], h, monkeypatch)
    assert r.exit is LoopExit.turn_complete, "not exhaustion, not failure - a finished turn"
    assert r.payload == "Which engine would you like?"
    assert h.records[-1].tally.rounds == 1, "no extra provider round"


def test_the_completed_turn_emits_a_truthful_non_failure_exit(h, monkeypatch):
    st = _state(rec_authorized=False)
    _drive(_policy_for(st, monkeypatch), [_round(text="hi")], h, monkeypatch)
    rec = h.records[-1]
    assert rec.exit is LoopExit.turn_complete
    assert rec.exit not in (LoopExit.provider_failed, LoopExit.no_progress,
                            LoopExit.rounds_exhausted, LoopExit.terminal_action)
    assert rec.failure_marker == ""


def test_builder_and_reviewer_plain_text_still_continues():
    from meshpipeline.agents.builder.loop_policy import BuilderLoopPolicy
    from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
    b = BuilderLoopPolicy(engine="cfmesh", mode="initial", limits_=LoopLimits(), executor=None)
    d = b.on_plaintext(LoopTally(rounds=1))
    assert d.complete is False and "submit_mesh" in d.message
    from meshpipeline.contracts.evidence_ledger import EvidenceLedger
    r = ReviewLoopPolicy(plan=types.SimpleNamespace(axes=()), ledger=EvidenceLedger(),
                         runtime=None, limits_=LoopLimits(max_rounds=30))
    d2 = r.on_plaintext(LoopTally(rounds=1))
    assert d2.complete is False and "Rounds remaining" in d2.message


# malformed calls
def test_malformed_json_is_recorded_and_executes_nothing(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("submit_requirements", raw="{not json"))], h, monkeypatch)
    assert st.submit_args is None and st.submit_summary is None
    assert st.submit_attempts == 0, "a call that never ran is not an attempt"
    assert p.malformed_calls == 1
    assert h.records[-1].tally.malformed_calls == 1


def test_a_malformed_call_does_not_block_a_later_valid_call(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("web_search", raw="{bad"),
                      _tc("submit_requirements", mesh_engine="snappy"))], h, monkeypatch)
    assert st.submit_args is not None, "the valid call still runs"


def test_missing_arguments_are_never_inferred(monkeypatch):
    st = _state(rec_authorized=False)
    ex = IntakeToolExecutor(state=st, job_id="j", implemented_engines=["snappy"],
                            search_tool=lambda *a, **k: "")
    out = asyncio.run(ex.run("propose_engine_selection", {}))
    assert out.accepted is False, "an absent engine is not guessed"
    assert st.selection is None


# diagnostics
@pytest.mark.parametrize("script,expected", [
    ([_round(text="a question")], LoopExit.turn_complete),
    ([_round(marker="intake_provider_down")], LoopExit.provider_failed),
])
def test_every_exit_emits_exactly_one_record(script, expected, h, monkeypatch):
    st = _state(rec_authorized=False)
    _drive(_policy_for(st, monkeypatch), script, h, monkeypatch)
    assert len(h.records) == 1
    assert h.records[0].exit is expected


def test_round_exhaustion_emits_one_record(h, monkeypatch):
    st = _state(rec_authorized=False)
    p = IntakeLoopPolicy(exec_state=st,
                         executor=IntakeToolExecutor(state=st, job_id="j",
                                                     implemented_engines=["snappy"],
                                                     search_tool=lambda *a, **k: ""),
                         limits_=LoopLimits(max_rounds=2))
    _drive(p, [_round(_tc("web_search", query="a")), _round(_tc("web_search", query="b")),
               _round(_tc("web_search", query="c"))], h, monkeypatch)
    assert len(h.records) == 1 and h.records[0].exit is LoopExit.rounds_exhausted


def test_the_sanitized_record_carries_no_token_payload_or_prose(h, monkeypatch):
    from meshpipeline.agents.loop.diagnostics import sanitized
    st = _approved_state(monkeypatch)
    st.pending = {"token": "SECRET-TOKEN-VALUE", "canonical": {"engine": "snappy"}}
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy",
                          request_txt="my private geometry notes"))], h, monkeypatch)
    blob = json.dumps(sanitized(h.records[-1]))
    for leak in ("SECRET-TOKEN-VALUE", "my private geometry notes", "CANONICAL SUMMARY",
                 "preview_token", "signature"):
        assert leak not in blob, f"the Intake record leaked {leak}"


def test_the_extension_reports_classifications_not_values(h, monkeypatch):
    st = _approved_state(monkeypatch)
    p = _policy_for(st, monkeypatch)
    _drive(p, [_round(_tc("submit_requirements", mesh_engine="snappy"))], h, monkeypatch)
    ext = h.records[-1].extension
    assert ext.authorization_state == "authorized"
    assert ext.selection_state == "confirmed"
    assert ext.canonical_revision == "r1"
    assert "submit_requirements" in ext.tools_invoked
