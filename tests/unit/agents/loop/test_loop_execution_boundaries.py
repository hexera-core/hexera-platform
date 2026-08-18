# Responsibility: Verify what the runner may not do - run a tool, call the provider, parse arguments, move the messages.
# Boundaries: also the budget arithmetic and the role neutrality that keep the loop reusable across agents.
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

from meshpipeline.agents.loop import diagnostics, tracing
from meshpipeline.agents.loop.budget import LoopBudget
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.agents.loop.provider_round import (
    ProviderRound,
    RoundAnswered,
    RoundEnded,
    assistant_turn,
)
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.agents.loop.tool_execution import parse_arguments
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    LoopTally,
    ProgressObservation,
)
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


class _Ext:
    def sanitized(self):
        return {"kind": "test"}


class _Driver:

    role = AgentRole.reviewer

    def __init__(self, *, limits=None, execute=None, plaintext=None, supersede=(),
                 close_out=None):
        self._limits = limits or LoopLimits(max_rounds=8)
        self._execute = execute
        self._plaintext = plaintext or RoundDecision(message="call a tool", complete=False)
        self._supersede = tuple(supersede)
        self._close_out = close_out
        self.executed: list[str] = []

    def limits(self): return self._limits
    def category_of(self, tool): return "navigation"
    def observe(self, tally): return ProgressObservation(made_progress=True, signature="s")
    def correction(self, stage, tally, observation): return None
    def before_round(self, tally, messages): return None
    def forced_tool(self, tally): return None
    def on_plaintext(self, tally): return self._plaintext
    def is_supersession(self, exc): return isinstance(exc, self._supersede)
    def extension(self): return _Ext()

    async def close_out(self, tally):
        return self._close_out(tally) if self._close_out else None

    async def execute(self, invocation):
        self.executed.append(invocation.tool)
        if self._execute:
            return await self._execute(invocation)
        return ToolOutcome(content=f"{invocation.tool} ok")


def _tc(name, args="{}", cid=None):
    return ToolCallRequest(id=cid or f"c-{name}", name=name, arguments=args)


def _round(*calls, text="", marker="", attempts=1):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason="tool_calls" if calls else "stop",
                            provider=ProviderAttemptInfo(attempts, "p", "m"),
                            failure_marker=marker)


def _append(messages, call_id, content):
    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


def _drive(script, driver, *, messages=None, provider=None, tools=None, **kw):
    it = iter(script)

    async def _default(**_kw):
        try:
            return next(it)
        except StopIteration:
            return _round(text="nothing more")

    msgs = messages if messages is not None else []
    kw.setdefault("deadline_s", 30.0)
    result = asyncio.run(run_agent_loop(
        driver=driver, provider_call=provider or _default, messages=msgs,
        tools=tools if tools is not None else [],
        job_id="job-6d", append_tool_result=_append, **kw))
    return result, msgs


@pytest.fixture(autouse=True)
def captured(monkeypatch):
    out: list = []
    monkeypatch.setattr(diagnostics, "emit", lambda rec: out.append(rec) or rec)
    return out


# the ordinary shapes

def test_a_final_response_with_no_tool_call_can_end_the_invocation(captured):
    driver = _Driver(plaintext=RoundDecision(complete=True, payload="the answer",
                                             exit=LoopExit.turn_complete))
    result, _ = _drive([_round(text="here is my answer")], driver)
    assert result.exit is LoopExit.turn_complete
    assert result.payload == "the answer"
    assert driver.executed == []


def test_one_tool_call_then_a_final_response(captured):
    driver = _Driver(plaintext=RoundDecision(complete=True, payload="done"))
    result, messages = _drive([_round(_tc("inspect")), _round(text="finished")], driver)
    assert driver.executed == ["inspect"]
    assert result.exit is LoopExit.turn_complete
    assert [m["role"] for m in messages] == ["assistant", "tool", "assistant"]


def test_multiple_sequential_tool_calls_across_rounds(captured):
    driver = _Driver(plaintext=RoundDecision(complete=True))
    _drive([_round(_tc("a")), _round(_tc("b")), _round(_tc("c")), _round(text="done")], driver)
    assert driver.executed == ["a", "b", "c"]


def test_several_calls_in_one_provider_response_run_in_the_providers_order(captured):
    driver = _Driver(plaintext=RoundDecision(complete=True))
    _, messages = _drive([_round(_tc("first"), _tc("second"), _tc("third")),
                          _round(text="done")], driver)
    assert driver.executed == ["first", "second", "third"]
    ids = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert ids == ["c-first", "c-second", "c-third"]


# what a call can do wrong

def test_an_unknown_tool_is_refused_by_the_agent_and_still_counted(captured):
    async def _refuse(inv):
        return ToolOutcome(content="no such tool", accepted=False)
    driver = _Driver(execute=_refuse, plaintext=RoundDecision(complete=True))
    _, messages = _drive([_round(_tc("no_such_tool")), _round(text="done")], driver)
    assert driver.executed == ["no_such_tool"], "the loop refused a call the agent never saw"
    rec = captured[0]
    assert rec.tally.tool_calls == 1
    assert rec.tool_calls[0].accepted is False
    assert any(m.get("content") == "no such tool" for m in messages)


def test_malformed_arguments_are_recorded_as_malformed_and_never_silently_emptied(captured):
    seen: list = []

    async def _capture(inv):
        seen.append(inv.parsed)
        return ToolOutcome(content="refused", accepted=False)
    driver = _Driver(execute=_capture, plaintext=RoundDecision(complete=True))
    _drive([_round(_tc("t", args="{not json")), _round(text="done")], driver)
    assert seen == [None], "malformed arguments reached the agent as an empty dict"
    assert captured[0].tally.malformed_calls == 1


def test_a_tool_exception_produces_exactly_one_record_and_still_propagates(captured):
    async def _boom(inv):
        raise RuntimeError("the tool broke")
    driver = _Driver(execute=_boom)
    with pytest.raises(RuntimeError, match="the tool broke"):
        _drive([_round(_tc("t"))], driver)
    assert len(captured) == 1, f"{len(captured)} records for one failure"
    assert captured[0].exit is LoopExit.executor_failed


# what the provider can do

def test_a_provider_exception_propagates_and_is_never_dressed_as_a_provider_failure(captured):
    async def _boom(**kw):
        raise RuntimeError("router bug")
    with pytest.raises(RuntimeError, match="router bug"):
        _drive([], _Driver(), provider=_boom)
    assert not any(getattr(r, "exit", None) is LoopExit.provider_failed for r in captured)


def test_provider_attempts_are_reported_by_the_router_and_never_retried_here(captured):
    calls = {"n": 0}

    async def _once(**kw):
        calls["n"] += 1
        return _round(marker="<<API_FAILURE:rate_limit>>", attempts=3)
    result, _ = _drive([], _Driver(), provider=_once)
    assert calls["n"] == 1, "the loop retried a provider failure itself"
    assert result.exit is LoopExit.provider_failed
    assert result.failure_marker == "<<API_FAILURE:rate_limit>>"
    assert captured[0].tally.provider_attempts == 3, "the router's attempts were not recorded"


def test_a_deterministic_contract_failure_is_not_retried_into_a_second_round(captured):
    rounds = {"n": 0}

    async def _fail(**kw):
        rounds["n"] += 1
        return _round(marker="<<API_FAILURE:invalid_request>>")
    result, _ = _drive([], _Driver(limits=LoopLimits(max_rounds=8)), provider=_fail)
    assert rounds["n"] == 1 and result.exit is LoopExit.provider_failed


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2, 3]", '"a string"', "null"])
def test_a_response_whose_arguments_are_not_an_object_is_malformed_not_a_crash(arguments):
    assert parse_arguments(arguments) is None


def test_absent_arguments_are_an_empty_call_not_a_malformed_one():
    assert parse_arguments("") == {} and parse_arguments("   ") == {}
    assert parse_arguments("{}") == {}


# budget

def test_the_round_limit_ends_the_invocation(captured):
    driver = _Driver(limits=LoopLimits(max_rounds=3))
    result, _ = _drive([_round(_tc("t"))] * 10, driver)
    assert result.exit is LoopExit.rounds_exhausted
    assert captured[0].tally.rounds == 3


def test_an_expired_deadline_starts_no_new_round(captured):
    result, _ = _drive([_round(_tc("t"))] * 5, _Driver(), deadline_s=-1.0)
    assert result.exit is LoopExit.deadline_exhausted
    assert captured[0].tally.rounds == 0, "a round began after the deadline had passed"


class TestTheBudgetItself:

    def test_an_unbounded_budget_never_stops_anything(self):
        b = LoopBudget(deadline=None, max_rounds=None)
        assert b.remaining() is None and not b.expired()
        assert b.may_start_round(10_000)

    def test_the_round_limit_is_a_count_of_completed_rounds(self):
        b = LoopBudget(max_rounds=3)
        assert b.may_start_round(0) and b.may_start_round(2)
        assert not b.may_start_round(3), "a fourth round was allowed under a limit of 3"

    def test_an_elapsed_deadline_is_expired_and_a_future_one_is_not(self):
        assert LoopBudget.of(LoopLimits(), -1.0).expired()
        assert not LoopBudget.of(LoopLimits(), 30.0).expired()

    def test_the_deadline_never_decides_the_round_limit(self):
        assert LoopBudget.of(LoopLimits(max_rounds=5), -1.0).may_start_round(0) is True


# cancellation

def test_cancellation_during_the_provider_call_propagates_and_records_nothing(captured):
    async def _cancelled(**kw):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        _drive([], _Driver(), provider=_cancelled)
    assert captured == [], "a cancelled provider call manufactured a run record"


def test_cancellation_during_tool_execution_always_propagates(captured):
    async def _cancelled(inv):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        _drive([_round(_tc("t"))], _Driver(execute=_cancelled))


def test_a_superseded_generation_writes_no_authoritative_record(captured):
    class _Lost(Exception):
        pass

    async def _lost(inv):
        raise _Lost()
    superseded: list = []
    import meshpipeline.agents.loop.diagnostics as diag
    real = diag.emit_superseded
    diag.emit_superseded = lambda **kw: superseded.append(kw)
    try:
        with pytest.raises(_Lost):
            _drive([_round(_tc("t"))], _Driver(execute=_lost, supersede=(_Lost,)))
    finally:
        diag.emit_superseded = real
    assert captured == [], "a superseded generation wrote an authoritative record"
    assert len(superseded) == 1, "the non-authoritative breadcrumb was not emitted"


# correlation

def test_every_tool_result_carries_the_id_of_the_call_it_answers(captured):
    driver = _Driver(plaintext=RoundDecision(complete=True))
    _, messages = _drive([_round(_tc("a", cid="id-a"), _tc("b", cid="id-b")),
                          _round(text="done")], driver)
    announced = [c["id"] for m in messages if m["role"] == "assistant"
                 for c in m.get("tool_calls", [])]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert announced == ["id-a", "id-b"]
    assert answered == announced, "a tool result was correlated to the wrong call"


def test_a_provider_that_repeats_one_id_still_gets_two_distinct_trace_identities(captured):
    from meshpipeline.agents.loop.tracing import TraceContext
    ctx = TraceContext(publisher=object(), job_id="j", role="reviewer", attempt=1)
    first = tracing.call_id(ctx, 1, 1, "same-id")
    second = tracing.call_id(ctx, 1, 2, "same-id")
    assert first != second, "two executions shared one trace id"

    driver = _Driver(plaintext=RoundDecision(complete=True))
    _drive([_round(_tc("t", cid="dup"), _tc("t", cid="dup")), _round(text="done")], driver)
    assert driver.executed == ["t", "t"], "a duplicate id suppressed an execution"


def test_a_call_id_never_contains_the_tool_name_or_its_arguments():
    from meshpipeline.agents.loop.tracing import TraceContext
    cid = tracing.call_id(TraceContext(job_id="j", role="builder", attempt=2), 3, 4, "prov-9")
    assert "write_file" not in cid and "{" not in cid
    assert cid.startswith("c:j:builder:2:3:4")


# trace and usage accounting

def test_a_round_is_counted_even_when_the_provider_reports_a_failure(captured):
    _drive([], _Driver(), provider=_failing_provider())
    assert captured[0].tally.rounds == 1, "a failed round was not counted"


def _failing_provider():
    async def _p(**kw):
        return _round(marker="<<API_FAILURE:down>>")
    return _p


def test_token_usage_is_recorded_per_round(captured):
    async def _p(**kw):
        return ModelRoundResult(tool_calls=(), assistant_text="hi", finish_reason="stop",
                                provider=ProviderAttemptInfo(1, "p", "m"),
                                input_tokens=11, output_tokens=7, cached_input_tokens=3)
    _drive([], _Driver(plaintext=RoundDecision(complete=True)), provider=_p)
    r = captured[0].rounds[0]
    assert (r.input_tokens, r.output_tokens, r.cached_input_tokens) == (11, 7, 3)


def test_a_broken_trace_publisher_never_costs_a_round(captured):
    from meshpipeline.agents.loop.tracing import TraceContext

    class _Broken:
        def reasoning(self, *a, **k): raise RuntimeError("collector down")
        def tool_call(self, *a, **k): raise RuntimeError("collector down")
        def tool_result(self, *a, **k): raise RuntimeError("collector down")

    driver = _Driver(plaintext=RoundDecision(complete=True))
    result, _ = _drive([_round(_tc("t")), _round(text="done")], driver,
                       trace=TraceContext(publisher=_Broken(), job_id="j", role="reviewer"))
    assert result.exit is LoopExit.turn_complete
    assert driver.executed == ["t"]


# the transcript

def test_the_transcript_is_assistant_then_results_then_the_agents_nudge(captured):
    driver = _Driver(plaintext=RoundDecision(message="do something", complete=False),
                     limits=LoopLimits(max_rounds=2))
    _, messages = _drive([_round(_tc("a"), _tc("b")), _round(text="")], driver)
    roles = [m["role"] for m in messages]
    assert roles == ["assistant", "tool", "tool", "assistant", "user"], roles
    assert messages[-1]["content"] == "do something"


def test_the_assistant_turn_announces_every_call_before_any_result_is_appended(captured):
    driver = _Driver(plaintext=RoundDecision(complete=True))
    _, messages = _drive([_round(_tc("a"), _tc("b")), _round(text="done")], driver)
    first_tool = next(i for i, m in enumerate(messages) if m["role"] == "tool")
    assistant = messages[first_tool - 1]
    assert len(assistant["tool_calls"]) == 2, "results were appended before the calls were announced"


def test_the_assistant_turn_carries_the_providers_own_arguments_verbatim():
    r = _round(_tc("t", args='{"path": "a.txt"}'))
    turn = assistant_turn(r)
    assert turn["tool_calls"][0]["function"]["arguments"] == '{"path": "a.txt"}'
    assert turn["role"] == "assistant"


def test_the_loop_never_writes_a_system_message_and_never_moves_the_callers(captured):
    seeded = [{"role": "system", "content": "SECRET SYSTEM PROMPT"},
              {"role": "user", "content": "hello"}]
    driver = _Driver(plaintext=RoundDecision(message="keep going", complete=False),
                     limits=LoopLimits(max_rounds=2))
    _, messages = _drive([_round(_tc("t")), _round(text="")], driver, messages=seeded)
    systems = [i for i, m in enumerate(messages) if m.get("role") == "system"]
    assert systems == [0], "the loop added or moved a system message"
    added = messages[2:]
    assert not any("SECRET SYSTEM PROMPT" in str(m.get("content", "")) for m in added), (
        "the system prompt was echoed into the visible transcript")


def test_no_nudge_or_correction_is_ever_written_as_a_system_message(captured):
    driver = _Driver(plaintext=RoundDecision(message="nudge", complete=False),
                     limits=LoopLimits(max_rounds=2))
    _, messages = _drive([_round(text=""), _round(text="")], driver)
    assert all(m["role"] != "system" for m in messages)
    assert [m["role"] for m in messages if m["role"] == "user"] == ["user", "user"]


# the provider request

def test_the_request_carries_the_callers_tools_and_identity_unchanged():
    tools = [{"type": "function", "function": {"name": "x"}}]
    r = ProviderRound(provider_call=None, tools=tools, job_id="j1", user_id="u1")
    req = r.request([{"role": "user", "content": "hi"}], None)
    assert req["tools"] is tools and req["job_id"] == "j1" and req["user_id"] == "u1"
    assert "tool_choice" not in req, "an unforced round sent a tool_choice"


def test_a_forced_tool_becomes_a_provider_level_choice_and_nothing_else():
    r = ProviderRound(provider_call=None, tools=[])
    req = r.request([], "submit_findings")
    assert req["tool_choice"] == {"type": "function",
                                  "function": {"name": "submit_findings"}}


def test_the_round_reports_answered_and_ended_as_different_types():
    assert isinstance(RoundAnswered(result=_round()), RoundAnswered)
    ended = RoundEnded(exit=LoopExit.provider_failed, failure_marker="m")
    assert ended.exit is LoopExit.provider_failed and ended.failure_marker == "m"


# the three roles

def test_each_role_answers_an_empty_round_in_its_own_way():
    from meshpipeline.agents.builder.loop_policy import BuilderLoopPolicy
    from meshpipeline.agents.intake.executor import (
        IntakeExecutionState,
        IntakeToolExecutor,
    )
    from meshpipeline.agents.intake.loop_policy import IntakeLoopPolicy
    from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy

    state = IntakeExecutionState(session_id="s", owner_id="u", revision="r",
                                 user_msg_count=1, latest_user_msg="hi")
    intake = IntakeLoopPolicy(
        exec_state=state,
        executor=IntakeToolExecutor(state=state, job_id="j", implemented_engines=["snappy"],
                                    search_tool=lambda *a, **k: ""),
        limits_=LoopLimits(max_rounds=12))
    builder = BuilderLoopPolicy(engine="snappy", mode="initial", executor=None,
                                limits_=LoopLimits(max_rounds=60))
    from meshpipeline.contracts.evidence_ledger import EvidenceLedger
    reviewer = ReviewLoopPolicy(
        plan=types.SimpleNamespace(
            axes=(types.SimpleNamespace(name="mesh_quality", owner="engine:snappy", requires=()),),
            engine="snappy", purpose="external_cfd", required_gate_keys=frozenset(),
            required_metric_keys=frozenset(), required_render_artifacts=frozenset(),
            required_targets=()),
        ledger=EvidenceLedger(), runtime=None, limits_=LoopLimits(max_rounds=30))

    tally = LoopTally(rounds=1)
    assert intake.on_plaintext(tally).complete is True, "Intake stopped completing its turn"
    assert builder.on_plaintext(tally).complete is False
    assert reviewer.on_plaintext(tally).complete is False
    assert reviewer.on_plaintext(tally).message, "Reviewer stopped saying why it continued"


@pytest.mark.parametrize("role", [AgentRole.intake, AgentRole.builder, AgentRole.reviewer])
def test_the_loop_records_whichever_role_drove_it(role, captured):
    driver = _Driver(plaintext=RoundDecision(complete=True))
    driver.role = role
    _drive([_round(text="done")], driver)
    assert captured[0].role is role


def test_the_builder_runs_its_REAL_tools_through_the_loop(tmp_path, captured):
    from meshpipeline.agents.builder import tools as T
    from meshpipeline.agents.builder.tool_context import BuilderToolContext

    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = BuilderToolContext(workspace=ws, geometry=None, job_id="job-6d", execution_id="e",
                             execution_generation=1, engine="cfmesh", mesh_fidelity="standard")
    seen_tools: list = []

    async def _execute(inv):
        out = T._dispatch_tool(ctx, inv.tool, inv.parsed or {})
        return ToolOutcome(content=out, accepted="error" not in json.loads(out))

    driver = _Driver(execute=_execute, plaintext=RoundDecision(complete=True))

    async def _provider(**kw):
        seen_tools.append(kw["tools"])
        if len(seen_tools) == 1:
            return _round(_tc("write_file",
                              args=json.dumps({"path": "notes.txt", "content": "cell = 0.05"})))
        return _round(text="done")

    result, messages = _drive([], driver, provider=_provider, tools=T.TOOLS)

    assert (ws / "notes.txt").read_text() == "cell = 0.05", "the real tool never ran"
    assert result.exit is LoopExit.turn_complete
    assert [t["function"]["name"] for t in seen_tools[0]] == \
           [t["function"]["name"] for t in T.TOOLS], "the real registry was altered in transit"
    written = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
    assert written["written"] == "notes.txt"


def test_a_real_builder_tool_that_refuses_is_carried_back_as_a_refusal(tmp_path, captured):
    from meshpipeline.agents.builder import tools as T
    from meshpipeline.agents.builder.tool_context import BuilderToolContext

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "request.txt").write_text("the approved brief")
    ctx = BuilderToolContext(workspace=ws, geometry=None, job_id="j", execution_id="e",
                             execution_generation=1, engine="cfmesh", mesh_fidelity="standard")

    async def _execute(inv):
        out = T._dispatch_tool(ctx, inv.tool, inv.parsed or {})
        return ToolOutcome(content=out, accepted="error" not in json.loads(out))

    driver = _Driver(execute=_execute, plaintext=RoundDecision(complete=True))
    _drive([_round(_tc("write_file",
                       args=json.dumps({"path": "request.txt", "content": "overwritten"}))),
            _round(text="done")], driver, tools=T.TOOLS)

    assert (ws / "request.txt").read_text() == "the approved brief"
    assert captured[0].tool_calls[0].accepted is False, "a refused call was recorded as accepted"


# the boundaries are load-bearing

class TestTheBoundariesHold:

    LOOP = Path(__file__).resolve().parents[4] / "src/meshpipeline/agents/loop"

    def _src(self, name: str) -> str:
        return (self.LOOP / name).read_text()

    def test_the_runner_never_executes_a_tool_itself(self):
        assert "driver.execute(" not in self._src("runner.py")
        assert "driver.execute(" in self._src("tool_execution.py")

    def test_the_runner_never_calls_the_provider_itself(self):
        src = self._src("runner.py")
        assert "provider_call(" not in src, "the runner calls the provider directly again"
        assert "asyncio.wait_for" not in src, "the runner owns a timeout of its own again"

    def test_the_runner_parses_no_arguments_and_publishes_no_trace(self):
        src = self._src("runner.py")
        assert "json.loads" not in src, "argument parsing returned to the runner"
        for leaked in ("publisher", "reasoning_id", "tool_call(", "tool_result("):
            assert leaked not in src, f"trace publishing returned to the runner: {leaked}"

    def test_the_runner_does_no_deadline_arithmetic(self):
        assert "time.monotonic" not in self._src("runner.py")
        assert "time.monotonic" in self._src("budget.py")

    def test_the_record_authority_stayed_in_the_runner(self):
        assert "acct.report(" in self._src("runner.py")
        for module in ("provider_round.py", "tool_execution.py", "budget.py", "tracing.py"):
            src = self._src(module)
            assert ".report(" not in src, f"{module} writes a run record"
            assert "emit_superseded" not in src, f"{module} decides supersession reporting"

    @pytest.mark.parametrize("module", ["runner.py", "driver.py", "budget.py", "accounting.py",
                                        "tracing.py", "provider_round.py", "tool_execution.py",
                                        "progress.py", "diagnostics.py"])
    def test_the_loop_stays_role_neutral(self, module):
        src = self._src(module)
        for role in ("agents.intake", "agents.builder", "agents.reviewer", "meshpipeline.engines"):
            assert role not in src, f"{module} imports {role}"

    def test_no_loop_module_imports_the_application_or_api_layers(self):
        for path in self.LOOP.glob("*.py"):
            src = path.read_text()
            for banned in ("meshpipeline.api", "fastapi", "meshpipeline.application"):
                assert banned not in src, f"{path.name} imports {banned}"

    def test_no_dumping_ground_module_was_created(self):
        for banned in ("utils.py", "helpers.py", "common.py", "misc.py", "loop_helpers.py"):
            assert not (self.LOOP / banned).exists(), f"a dumping ground was created: {banned}"

    def test_the_extracted_modules_do_not_import_the_runner(self):
        for module in ("driver.py", "budget.py", "accounting.py", "tracing.py",
                       "provider_round.py", "tool_execution.py"):
            assert "loop.runner" not in self._src(module), f"{module} imports the runner"

    def test_nothing_re_exports_from_the_runner_for_compatibility(self):
        import meshpipeline.agents.loop.runner as R
        for moved in ("ToolInvocation", "RoundDecision", "UNCATEGORIZED", "ToolCallRequest"):
            assert not hasattr(R, moved), (
                f"{moved} is still reachable from the runner - the old import path survives")
        assert R.__all__ == ["run_agent_loop"]
