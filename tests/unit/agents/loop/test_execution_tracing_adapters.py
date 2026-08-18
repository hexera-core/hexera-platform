# Responsibility: Verify the builder's loop adapters trace the same run through ownership checks.
# Boundaries: the two adapters above the committed leaf helpers - the leaves own the authorization matrix.
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import uuid

import pytest

from meshpipeline.agents.loop import provider_round as PR
from meshpipeline.agents.loop import tool_execution as TE
from meshpipeline.agents.loop.accounting import AgentRunAccountant
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.agents.loop.tracing import ExecutionTraceContext, TraceContext
from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    ProgressObservation,
)
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest


class _Ext:
    def sanitized(self):
        return {"kind": "test"}


class _Driver:

    role = AgentRole.builder

    def __init__(self, *, raises=None):
        self._raises = raises
        self.executed: list[str] = []

    def limits(self): return LoopLimits(max_rounds=4)
    def category_of(self, tool): return "navigation"
    def observe(self, tally): return ProgressObservation(made_progress=True, signature="s")
    def correction(self, stage, tally, observation): return None
    def before_round(self, tally, messages): return None
    def forced_tool(self, tally): return None
    def on_plaintext(self, tally): return RoundDecision(message=None, complete=True)
    def is_supersession(self, exc): return False
    def extension(self): return _Ext()

    async def close_out(self, tally):
        return None

    async def execute(self, invocation):
        self.executed.append(invocation.tool)
        if self._raises:
            raise self._raises
        return ToolOutcome(content=f"{invocation.tool} ok", accepted=True)


class _Recorder:
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.calls: list = []

    def tool_call(self, *a, **k):
        self.calls.append(("tool_call", a, k))

    def tool_result(self, *a, **k):
        self.calls.append(("tool_result", a, k))

    def reasoning(self, *a, **k):
        self.calls.append(("reasoning", a, k))


class _Own:
    def __init__(self, job_id="job-1", generation=2):
        self.job_id = job_id
        self.execution_generation = generation
        self.worker_token = uuid.UUID(int=7)


def _bind(monkeypatch, *, own, current=True):
    import meshpipeline.application.execution_publisher as mod

    async def is_current_owner():
        return current

    monkeypatch.setattr(mod._fence, "current_ownership", lambda: own)
    monkeypatch.setattr(mod._fence, "is_current_owner", is_current_owner)


def _acct():
    return AgentRunAccountant(role=AgentRole.builder, job_id="j", limits=LoopLimits(max_rounds=4),
                              pipeline_attempt=1, agent_attempt=1)


def _sync_calls(driver=None):
    inner = _Recorder()
    calls = TE.ToolExecution(driver=driver or _Driver(), acct=_acct(),
                             append_tool_result=lambda m, c, r: m.append((c, r)),
                             trace=TraceContext(publisher=inner, job_id="j", role="builder",
                                                attempt=1))
    return inner, calls


def _exec_calls(monkeypatch, driver=None, *, current=True, job_id="job-1"):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=job_id), current=current)
    calls = TE.ExecutionToolExecution(
        driver=driver or _Driver(), acct=_acct(),
        append_tool_result=lambda m, c, r: m.append((c, r)),
        execution_trace=ExecutionTraceContext(publisher=OwnershipCheckedPublisher(inner),
                                              job_id="j", role="builder", attempt=1))
    return inner, calls


def _tc(name="run_mesh"):
    return ToolCallRequest(id="p-1", name=name, arguments="{}")


async def _provider(**kw):
    return ModelRoundResult(assistant_text="hello", finish_reason="stop")


# type separation


def test_each_adapter_is_a_frozen_execution_variant_of_the_shared_one():
    assert issubclass(TE.ExecutionToolExecution, TE.ToolExecution)
    assert issubclass(PR.ExecutionProviderRound, PR.ProviderRound)
    for cls in (TE.ExecutionToolExecution, PR.ExecutionProviderRound):
        assert cls.__dataclass_params__.frozen
        ann = cls.__dataclass_fields__["execution_trace"].type
        assert "ExecutionTraceContext" in str(ann), ann


def test_the_shared_adapters_keep_the_synchronous_context():
    assert "ExecutionTraceContext" not in str(
        TE.ToolExecution.__dataclass_fields__["trace"].type)
    assert "ExecutionTraceContext" not in str(
        PR.ProviderRound.__dataclass_fields__["trace"].type)


# parity


def test_a_tool_call_publishes_the_same_two_events_on_both_routes(monkeypatch):
    sync_inner, sync_calls = _sync_calls()
    asyncio.run(sync_calls.run(_tc(), [], round_index=1, remaining=5.0))

    exec_inner, exec_calls = _exec_calls(monkeypatch)
    asyncio.run(exec_calls.run(_tc(), [], round_index=1, remaining=5.0))

    assert [c[0] for c in sync_inner.calls] == ["tool_call", "tool_result"]
    assert [c[0] for c in exec_inner.calls] == [c[0] for c in sync_inner.calls]
    # the correlation id and every payload argument, less the measured duration
    assert sync_inner.calls[0][1] == exec_inner.calls[0][1]
    assert sync_inner.calls[1][1][:5] == exec_inner.calls[1][1][:5]


def test_a_failed_tool_call_is_traced_the_same_way_on_both_routes(monkeypatch):
    boom = RuntimeError("tool exploded")
    sync_inner, sync_calls = _sync_calls(_Driver(raises=boom))
    sync_outcome = asyncio.run(sync_calls.run(_tc(), [], round_index=1, remaining=5.0))

    exec_inner, exec_calls = _exec_calls(monkeypatch, _Driver(raises=boom))
    exec_outcome = asyncio.run(exec_calls.run(_tc(), [], round_index=1, remaining=5.0))

    assert sync_outcome.raised is boom and exec_outcome.raised is boom
    assert [c[0] for c in exec_inner.calls] == [c[0] for c in sync_inner.calls]
    assert sync_inner.calls[1][1][4] == exec_inner.calls[1][1][4] == "failure"


def test_the_trace_brackets_the_tool_rather_than_following_it(monkeypatch):
    order: list[str] = []

    class _Watching(_Driver):
        async def execute(self, invocation):
            order.append("tool ran")
            return ToolOutcome(content="ok", accepted=True)

    inner, calls = _exec_calls(monkeypatch, _Watching())
    original = inner.tool_call

    def _seen(*a, **k):
        order.append("tool_call")
        original(*a, **k)

    inner.tool_call = _seen
    inner_result = inner.tool_result

    def _seen_result(*a, **k):
        order.append("tool_result")
        inner_result(*a, **k)

    inner.tool_result = _seen_result
    asyncio.run(calls.run(_tc(), [], round_index=1, remaining=5.0))
    assert order == ["tool_call", "tool ran", "tool_result"], order


def test_a_provider_round_publishes_the_same_reasoning_pair_on_both_routes(monkeypatch):
    sync_inner = _Recorder()
    sync_round = PR.ProviderRound(provider_call=_provider, tools=[], job_id="j",
                                  trace=TraceContext(publisher=sync_inner, job_id="j",
                                                     role="builder", attempt=1))
    asyncio.run(sync_round.invoke([], acct=_acct(), forced_tool=None, remaining=5.0, round_no=2))

    exec_inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=exec_inner.job_id))
    exec_round = PR.ExecutionProviderRound(
        provider_call=_provider, tools=[], job_id="j",
        execution_trace=ExecutionTraceContext(publisher=OwnershipCheckedPublisher(exec_inner),
                                              job_id="j", role="builder", attempt=1))
    asyncio.run(exec_round.invoke([], acct=_acct(), forced_tool=None, remaining=5.0, round_no=2))

    assert [c[0] for c in sync_inner.calls] == ["reasoning", "reasoning"]
    assert [c[0] for c in exec_inner.calls] == ["reasoning", "reasoning"]
    # same reasoning id and same phases - one identity across the two routes
    assert [c[1] for c in sync_inner.calls] == [c[1] for c in exec_inner.calls]


def test_the_correlation_id_is_identical_on_both_routes(monkeypatch):
    from meshpipeline.agents.loop import tracing as T
    sync = T.call_id(TraceContext(publisher=object(), job_id="j", role="builder", attempt=3),
                     2, 4, "prov-9")
    execution = T.execution_call_id(
        ExecutionTraceContext(publisher=OwnershipCheckedPublisher(_Recorder()), job_id="j",
                              role="builder", attempt=3), 2, 4, "prov-9")
    assert sync == execution != ""


# refusal


@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_a_lost_claim_stops_the_tool_adapter_before_the_tool_runs(monkeypatch, case):
    driver = _Driver()
    if case == "unbound":
        inner, calls = _exec_calls(monkeypatch, driver)
        _bind(monkeypatch, own=None)
    elif case == "another job":
        inner, calls = _exec_calls(monkeypatch, driver, job_id="a-different-job")
    else:
        inner, calls = _exec_calls(monkeypatch, driver, current=False)

    with pytest.raises(StaleExecutionPublish):
        asyncio.run(calls.run(_tc(), [], round_index=1, remaining=5.0))
    assert inner.calls == [], "a stale generation published a tool trace"
    assert driver.executed == [], "a stale generation ran the tool anyway"


def test_a_lost_claim_stops_the_round_adapter(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=None)
    rounds = PR.ExecutionProviderRound(
        provider_call=_provider, tools=[], job_id="j",
        execution_trace=ExecutionTraceContext(publisher=OwnershipCheckedPublisher(inner),
                                              job_id="j", role="builder", attempt=1))
    with pytest.raises(StaleExecutionPublish):
        asyncio.run(rounds.invoke([], acct=_acct(), forced_tool=None, remaining=5.0, round_no=1))
    assert inner.calls == []


def test_a_transport_failure_still_never_costs_the_builder_a_tool_call(monkeypatch):
    class _Broken(_Recorder):
        def tool_call(self, *a, **k):
            raise RuntimeError("redis is down")

        tool_result = reasoning = tool_call

    _bind(monkeypatch, own=_Own())
    driver = _Driver()
    calls = TE.ExecutionToolExecution(
        driver=driver, acct=_acct(), append_tool_result=lambda m, c, r: None,
        execution_trace=ExecutionTraceContext(publisher=OwnershipCheckedPublisher(_Broken()),
                                              job_id="j", role="builder", attempt=1))
    outcome = asyncio.run(calls.run(_tc(), [], round_index=1, remaining=5.0))
    assert driver.executed == ["run_mesh"] and outcome.raised is None


# structure


SOURCES = {"tool_execution": pathlib.Path(TE.__file__).read_text(),
           "provider_round": pathlib.Path(PR.__file__).read_text()}


def _cls(module: str, name: str):
    return next(n for n in ast.walk(ast.parse(SOURCES[module]))
                if isinstance(n, ast.ClassDef) and n.name == name)


@pytest.mark.parametrize("module,name", [("tool_execution", "ExecutionToolExecution"),
                                         ("provider_round", "ExecutionProviderRound")])
def test_every_trace_call_in_the_adapter_is_awaited_and_gated(module, name):
    node = _cls(module, name)
    reached = [n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
               for n in ast.walk(node) if isinstance(n, ast.Call)
               and isinstance(n.func, (ast.Attribute, ast.Name))]
    traced = [r for r in reached
              if r in {"tool_call", "tool_result", "round_begin", "round_end", "call_id",
                       "atool_call", "atool_result", "around_begin", "around_end",
                       "execution_call_id"}]
    assert traced and all(t.startswith("a") or t == "execution_call_id" for t in traced), (
        f"{name} reaches a synchronous trace helper: {traced}")

    gated = {"atool_call", "atool_result", "around_begin", "around_end"}
    awaited = {id(n.value) for n in ast.walk(node) if isinstance(n, ast.Await)}
    publications = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                    and ((isinstance(n.func, ast.Attribute) and n.func.attr in gated)
                         or (isinstance(n.func, ast.Name) and n.func.id in gated))]
    assert publications, f"{name} publishes nothing"
    assert all(id(c) in awaited for c in publications), f"{name} leaves a publication unawaited"


@pytest.mark.parametrize("module,name", [("tool_execution", "ExecutionToolExecution"),
                                         ("provider_round", "ExecutionProviderRound")])
def test_the_adapter_never_reaches_around_the_gate(module, name):
    src = ast.unparse(_cls(module, name))
    assert "_inner" not in src, f"{name} reaches around the ownership check"
    assert "TraceContext(" not in src.replace("ExecutionTraceContext", ""), \
        f"{name} constructs a plain trace context"
    assert "hasattr" not in src and "isinstance" not in src, \
        f"{name} inspects its route at runtime instead of being typed"


@pytest.mark.parametrize("module,name,seams",
                         [("tool_execution", "ExecutionToolExecution", {"_cid", "_began",
                                                                        "_ended"}),
                          # _sink joins the round seams: reasoning now publishes as it arrives,
                          # against the id _began opened, so the route that decides where a round
                          # is traced decides where its reasoning goes too.
                          ("provider_round", "ExecutionProviderRound", {"_began", "_ended",
                                                                        "_sink"})])
def test_the_adapter_overrides_only_the_trace_seams(module, name, seams):
    overridden = {n.name for n in _cls(module, name).body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert overridden == seams, (
        f"{name} overrides {overridden}, not only the trace seams - the run mechanics must stay "
        "in one place for every agent")


# runner selection


def test_the_runner_builds_the_execution_adapters_only_for_an_execution_trace(monkeypatch):
    built: list = []
    monkeypatch.setattr(TE.ExecutionToolExecution, "__post_init__",
                        lambda self: built.append("exec"), raising=False)

    seen: list[str] = []
    real_exec_init = PR.ExecutionProviderRound.__init__
    real_plain_init = PR.ProviderRound.__init__

    def _exec_init(self, *a, **k):
        seen.append("execution")
        real_exec_init(self, *a, **k)

    def _plain_init(self, *a, **k):
        seen.append("plain")
        real_plain_init(self, *a, **k)

    monkeypatch.setattr(PR.ExecutionProviderRound, "__init__", _exec_init)
    monkeypatch.setattr(PR.ProviderRound, "__init__", _plain_init)

    _bind(monkeypatch, own=_Own())
    asyncio.run(run_agent_loop(
        driver=_Driver(), provider_call=_provider, messages=[], tools=[], job_id="j",
        append_tool_result=lambda m, c, r: None, record_sink=lambda r: None,
        execution_trace=ExecutionTraceContext(publisher=OwnershipCheckedPublisher(_Recorder()),
                                              job_id="j", role="builder", attempt=1)))
    assert seen[:1] == ["execution"], seen

    seen.clear()
    asyncio.run(run_agent_loop(
        driver=_Driver(), provider_call=_provider, messages=[], tools=[], job_id="j",
        append_tool_result=lambda m, c, r: None, record_sink=lambda r: None,
        trace=TraceContext(publisher=_Recorder(), job_id="j", role="reviewer", attempt=1)))
    assert seen[:1] == ["plain"], seen


def test_a_runner_given_no_execution_trace_is_unchanged_for_reviewer_and_intake(monkeypatch):
    inner = _Recorder()
    result = asyncio.run(run_agent_loop(
        driver=_Driver(), provider_call=_provider, messages=[], tools=[], job_id="j",
        append_tool_result=lambda m, c, r: None, record_sink=lambda r: None,
        trace=TraceContext(publisher=inner, job_id="j", role="reviewer", attempt=1)))
    assert result.exit is LoopExit.turn_complete
    assert [c[0] for c in inner.calls] == ["reasoning", "reasoning"]


def test_the_runner_keeps_both_trace_parameters_explicitly_typed():
    params = inspect.signature(run_agent_loop).parameters
    assert str(params["trace"].annotation) == "TraceContext | None"
    assert str(params["execution_trace"].annotation) == "ExecutionTraceContext | None"
    assert params["execution_trace"].default is None
