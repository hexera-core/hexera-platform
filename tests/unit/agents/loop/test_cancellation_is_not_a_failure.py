# Responsibility: Verify a cancelled invocation records nothing, wherever in the loop the cancellation arrives.
# Boundaries: cancellation must not be reported as a tool failure, a supersession or a terminal status.
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from meshpipeline.agents.loop import diagnostics
from meshpipeline.agents.loop.driver import RoundDecision, ToolOutcome
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.contracts.agent_loop import (
    AgentRole,
    LoopExit,
    LoopLimits,
    ProgressObservation,
)
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


class _Ext:
    def sanitized(self):
        return {"kind": "cancellation-test"}


@dataclass
class Probe:

    provider_invocations: int = 0
    tool_invocations: int = 0
    records: list = field(default_factory=list)          # authoritative run records
    breadcrumbs: list = field(default_factory=list)      # non-authoritative supersession notes
    published: list = field(default_factory=list)        # public-trace events
    rounds_started: int = 0
    cleanups: list = field(default_factory=list)
    propagated: BaseException | None = None
    tasks_after: int = 0
    #: how many `failure` tool results this scenario legitimately expects (a real
    #: tool that ran and said no). Cancelled calls must never add to it.
    expected_failure_results: int = 0

    @property
    def record_types(self) -> list:
        return [getattr(r, "exit", None) for r in self.records]

    def summary(self) -> dict:
        return {"provider_invocations": self.provider_invocations,
                "tool_invocations": self.tool_invocations,
                "authoritative_records": len(self.records),
                "record_types": [str(x) for x in self.record_types],
                "breadcrumbs": len(self.breadcrumbs),
                "published_events": len(self.published),
                "rounds_started": self.rounds_started,
                "propagated": type(self.propagated).__name__ if self.propagated else None,
                "live_tasks_after_teardown": self.tasks_after}


@pytest.fixture
def probe(monkeypatch):
    p = Probe()
    monkeypatch.setattr(diagnostics, "emit", lambda rec: p.records.append(rec) or rec)
    monkeypatch.setattr(diagnostics, "emit_superseded", lambda **kw: p.breadcrumbs.append(kw))
    return p


class _Publisher:

    def __init__(self, probe: Probe):
        self._p = probe

    def reasoning(self, *a, **k): self._p.published.append(("reasoning", a, k))
    def tool_call(self, *a, **k): self._p.published.append(("tool_call", a, k))
    def tool_result(self, *a, **k): self._p.published.append(("tool_result", a, k))


class _Driver:
    role = AgentRole.reviewer

    def __init__(self, probe, *, execute=None, limits=None, supersede=(), close_out=None,
                 on_before_round=None):
        self._p = probe
        self._execute = execute
        self._limits = limits or LoopLimits(max_rounds=8)
        self._supersede = tuple(supersede)
        self._close_out = close_out
        self._on_before_round = on_before_round

    def limits(self): return self._limits
    def category_of(self, tool): return "navigation"
    def observe(self, tally): return ProgressObservation(made_progress=True, signature="s")
    def correction(self, stage, tally, observation): return None
    def forced_tool(self, tally): return None
    def is_supersession(self, exc): return isinstance(exc, self._supersede)
    def extension(self): return _Ext()
    def on_plaintext(self, tally): return RoundDecision(complete=True, payload="done")

    def before_round(self, tally, messages):
        self._p.rounds_started += 1
        if self._on_before_round:
            self._on_before_round()

    async def close_out(self, tally):
        return await self._close_out(tally) if self._close_out else None

    async def execute(self, invocation):
        self._p.tool_invocations += 1
        return await self._execute(invocation)


def _tc(name, args="{}", cid=None):
    return ToolCallRequest(id=cid or f"c-{name}", name=name, arguments=args)


def _round(*calls, text=""):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason="tool_calls" if calls else "stop",
                            provider=ProviderAttemptInfo(1, "p", "m"))


def _append(messages, call_id, content):
    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


async def _cancel_when(started: asyncio.Event, coro) -> BaseException:
    task = asyncio.create_task(coro)
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    try:
        await task
    except BaseException as exc:
        return exc
    raise AssertionError("the cancelled task completed normally")


def _drive(probe, driver, script, *, messages=None, tools=None, trace=None, **kw):
    it = iter(script)

    async def provider(**_kw):
        probe.provider_invocations += 1
        try:
            nxt = next(it)
        except StopIteration:
            return _round(text="nothing more")
        return await nxt(**_kw) if callable(nxt) else nxt

    return run_agent_loop(
        driver=driver, provider_call=provider,
        messages=messages if messages is not None else [],
        tools=tools if tools is not None else [], job_id="job-cancel",
        append_tool_result=_append, trace=trace, deadline_s=30.0, **kw)


async def _finish(probe: Probe, exc: BaseException) -> None:
    probe.propagated = exc
    await asyncio.sleep(0)          # let any cancelled inner task finish unwinding
    probe.tasks_after = len([t for t in asyncio.all_tasks() if t is not asyncio.current_task()])


def _assert_nothing_recorded(probe: Probe, *, allow_breadcrumb: bool = False) -> None:
    assert isinstance(probe.propagated, asyncio.CancelledError), (
        f"cancellation was converted into {type(probe.propagated).__name__}")
    assert probe.records == [], (
        f"a cancelled execution wrote {len(probe.records)} authoritative record(s): "
        f"{[str(t) for t in probe.record_types]}")
    if not allow_breadcrumb:
        assert probe.breadcrumbs == [], "a cancelled execution left a supersession breadcrumb"
    # `publisher.tool_result(result_id, cid, name, result, status, ms)` - status is args[4].
    failures = [a for kind, a, _kw in probe.published
                if kind == "tool_result" and len(a) > 4 and a[4] == "failure"]
    assert len(failures) == probe.expected_failure_results, (
        f"{len(failures)} failure result(s) published; expected "
        f"{probe.expected_failure_results}. A cancelled call must publish none.")
    assert probe.tasks_after == 0, f"{probe.tasks_after} asyncio task(s) leaked after teardown"


# provider invocation pending

async def test_cancellation_while_the_provider_call_is_pending(probe):
    started = asyncio.Event()

    async def _hang(**kw):
        started.set()
        await asyncio.Event().wait()          # never resolves; the test cancels it

    exc = await _cancel_when(started, _drive(probe, _Driver(probe), [_hang],
                                             trace=None))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.provider_invocations == 1
    assert probe.tool_invocations == 0
    assert probe.summary()["authoritative_records"] == 0


# tool invocation pending

async def test_cancellation_while_one_tool_invocation_is_pending(probe):
    started = asyncio.Event()

    async def _hang(inv):
        started.set()
        await asyncio.Event().wait()

    exc = await _cancel_when(started,
                             _drive(probe, _Driver(probe, execute=_hang), [_round(_tc("t"))]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.tool_invocations == 1, "the tool was not actually entered"
    assert LoopExit.executor_failed not in probe.record_types


async def test_the_cancelled_call_publishes_no_result_at_all(probe):
    started = asyncio.Event()

    async def _hang(inv):
        started.set()
        await asyncio.Event().wait()

    from meshpipeline.agents.loop.tracing import TraceContext
    trace = TraceContext(publisher=_Publisher(probe), job_id="j", role="reviewer", attempt=1)
    exc = await _cancel_when(started, _drive(probe, _Driver(probe, execute=_hang),
                                             [_round(_tc("t"))], trace=trace))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    kinds = [k for k, _, _ in probe.published]
    assert "tool_call" in kinds, "the call was never announced"
    assert "tool_result" not in kinds, (
        "a cancelled call published a terminal result - a reader would think it landed")


# before tool dispatch

async def test_cancellation_delivered_before_any_tool_is_dispatched(probe):
    started = asyncio.Event()

    async def _second_round_hangs(**kw):
        started.set()
        await asyncio.Event().wait()

    async def _never(inv):
        raise AssertionError("a tool ran after cancellation")

    exc = await _cancel_when(
        started, _drive(probe, _Driver(probe, execute=_never), [_second_round_hangs]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.tool_invocations == 0, "a tool was dispatched despite cancellation"


# after a completed tool

async def test_cancellation_after_one_completed_tool_but_before_the_next(probe):
    started = asyncio.Event()
    messages: list = []

    async def _execute(inv):
        if inv.tool == "first":
            return ToolOutcome(content="first ok")
        started.set()
        await asyncio.Event().wait()

    exc = await _cancel_when(
        started, _drive(probe, _Driver(probe, execute=_execute),
                        [_round(_tc("first"), _tc("second"))], messages=messages))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.tool_invocations == 2

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, "the cancelled call was appended to the transcript"
    assert tool_msgs[0]["tool_call_id"] == "c-first"
    assert tool_msgs[0]["content"] == "first ok", "completed work was rolled back or rewritten"
    assert not any("second" in str(m.get("content", "")) for m in tool_msgs), (
        "the conversation was rolled forward as though the cancelled tool succeeded")


# multi-tool response

async def test_cancellation_during_a_multi_tool_response_runs_no_further_tools(probe):
    started = asyncio.Event()
    order: list = []

    async def _execute(inv):
        order.append(inv.tool)
        if inv.tool == "a":
            return ToolOutcome(content="a ok")
        if inv.tool == "b":
            started.set()
            await asyncio.Event().wait()
        raise AssertionError(f"{inv.tool} ran after cancellation")

    exc = await _cancel_when(
        started, _drive(probe, _Driver(probe, execute=_execute),
                        [_round(_tc("a"), _tc("b"), _tc("c"))]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert order == ["a", "b"], f"tools ran past the cancellation point: {order}"
    assert probe.rounds_started == 1, "another round began after cancellation"
    assert probe.provider_invocations == 1, "the provider was called again after cancellation"


# retry backoff (router-owned)

async def test_cancellation_during_provider_retry_backoff_is_never_swallowed():
    import inspect

    from meshpipeline.adapters.model_inference import routing

    src = inspect.getsource(routing)
    cancel_at = src.index("except asyncio.CancelledError")
    base_at = src.index("except BaseException")
    assert cancel_at < base_at, (
        "CancelledError is classified as a provider failure before it can be re-raised")
    handler = src[cancel_at:src.index("except TimeoutError", cancel_at)]
    assert "raise" in handler and "classify" not in handler


# racing supersession

async def test_a_cancelled_stale_generation_writes_nothing_at_all(probe):
    class _Fenced(Exception):
        pass

    started = asyncio.Event()

    async def _hang(inv):
        started.set()
        await asyncio.Event().wait()

    driver = _Driver(probe, execute=_hang, supersede=(_Fenced,))
    exc = await _cancel_when(started, _drive(probe, driver, [_round(_tc("t"))]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.breadcrumbs == [], "a cancelled generation claimed it had been superseded"


async def test_a_genuine_supersession_is_still_reported_as_supersession(probe):
    class _Fenced(Exception):
        pass

    async def _fenced(inv):
        raise _Fenced()

    driver = _Driver(probe, execute=_fenced, supersede=(_Fenced,))
    with pytest.raises(_Fenced):
        await _drive(probe, driver, [_round(_tc("t"))])
    assert probe.records == [], "supersession wrote an authoritative record"
    assert len(probe.breadcrumbs) == 1, "supersession stopped leaving its breadcrumb"


async def test_an_ordinary_tool_failure_is_still_recorded(probe):
    async def _boom(inv):
        raise RuntimeError("the tool broke")

    with pytest.raises(RuntimeError, match="the tool broke"):
        await _drive(probe, _Driver(probe, execute=_boom), [_round(_tc("t"))])
    assert len(probe.records) == 1
    assert probe.records[0].exit is LoopExit.executor_failed


# cleanup

async def test_tool_cleanup_runs_on_cancellation_and_cancellation_still_wins(probe):
    started = asyncio.Event()

    async def _with_cleanup(inv):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            probe.cleanups.append("tool")

    exc = await _cancel_when(started,
                             _drive(probe, _Driver(probe, execute=_with_cleanup),
                                    [_round(_tc("t"))]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.cleanups == ["tool"], "teardown did not run"


async def test_a_failing_cleanup_never_replaces_the_cancellation(probe):
    started = asyncio.Event()

    async def _broken_cleanup(inv):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            probe.cleanups.append("attempted")
            raise RuntimeError("cleanup itself failed")   # noqa: B012

    exc = await _cancel_when(started,
                             _drive(probe, _Driver(probe, execute=_broken_cleanup),
                                    [_round(_tc("t"))]))
    await _finish(probe, exc)
    assert isinstance(probe.propagated, asyncio.CancelledError), (
        f"a failing cleanup replaced the cancellation with {type(probe.propagated).__name__} - "
        "the loop would then record it as an executor failure")
    assert probe.cleanups == ["attempted"], "cleanup did not run"
    assert probe.records == [], "a broken cleanup produced an authoritative record"
    assert probe.tasks_after == 0


async def test_cancellation_during_close_out_records_nothing(probe):
    started = asyncio.Event()

    async def _ok(inv):
        return ToolOutcome(content="ok")

    async def _close(tally):
        started.set()
        await asyncio.Event().wait()

    exc = await _cancel_when(
        started, _drive(probe, _Driver(probe, execute=_ok, close_out=_close),
                        [_round(_tc("t"))]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert probe.tool_invocations == 1, "close-out was reached without the round's tool running"


# the three role consumers

@pytest.mark.parametrize("role", [AgentRole.intake, AgentRole.builder, AgentRole.reviewer])
async def test_no_role_records_a_cancelled_invocation(role, probe):
    started = asyncio.Event()

    async def _hang(inv):
        started.set()
        await asyncio.Event().wait()

    driver = _Driver(probe, execute=_hang)
    driver.role = role
    exc = await _cancel_when(started, _drive(probe, driver, [_round(_tc("t"))]))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)


async def test_the_builder_records_nothing_when_its_REAL_tools_are_cancelled(tmp_path, probe):
    from meshpipeline.agents.builder import tools as T
    from meshpipeline.agents.builder.tool_context import BuilderToolContext

    ws = tmp_path / "ws"
    ws.mkdir()
    ctx = BuilderToolContext(workspace=ws, geometry=None, job_id="job-cancel", execution_id="e",
                             execution_generation=1, engine="cfmesh", mesh_fidelity="standard")
    started = asyncio.Event()
    messages: list = []

    async def _execute(inv):
        if inv.tool == "write_file":
            return ToolOutcome(content=T._dispatch_tool(ctx, inv.tool, inv.parsed or {}))
        started.set()
        await asyncio.Event().wait()

    driver = _Driver(probe, execute=_execute)
    driver.role = AgentRole.builder
    exc = await _cancel_when(started, _drive(
        probe, driver,
        [_round(_tc("write_file", args=json.dumps({"path": "sizing.txt", "content": "0.05"})),
                _tc("run_mesh"))],
        messages=messages, tools=T.TOOLS))
    await _finish(probe, exc)
    _assert_nothing_recorded(probe)
    assert (ws / "sizing.txt").read_text() == "0.05", "completed real work was lost"
    written = json.loads(next(m["content"] for m in messages if m.get("role") == "tool"))
    assert written["written"] == "sizing.txt"
    assert len([m for m in messages if m.get("role") == "tool"]) == 1


async def test_the_builder_driver_envelope_records_nothing_on_cancellation(probe):
    import inspect

    from meshpipeline.agents.builder import agent as builder_agent
    from meshpipeline.agents.builder import invoke as builder_invoke

    src = inspect.getsource(builder_invoke)
    cancel_at = src.index("except asyncio.CancelledError")
    base_at = src.index("except BaseException")
    assert cancel_at < base_at, (
        "the Builder driver envelope classifies cancellation before re-raising it")
    handler = src[cancel_at:base_at]
    assert "raise" in handler and "finish(" not in handler, (
        "the Builder driver envelope writes a record for a cancelled driver")

    node = inspect.getsource(builder_agent.node_builder)
    for owned_elsewhere in ("CancelledError", "BuilderDriverRun", "executor_failed", "finish("):
        assert owned_elsewhere not in node, (
            f"the graph node re-acquired the invocation envelope: {owned_elsewhere}")


# preservation across invocations

async def test_a_completed_invocations_record_survives_a_later_cancellation(probe):
    async def _ok(inv):
        return ToolOutcome(content="ok", terminal=True, payload="P")

    first = await _drive(probe, _Driver(probe, execute=_ok), [_round(_tc("t"))])
    assert first.exit is LoopExit.terminal_action
    assert len(probe.records) == 1
    before = list(probe.records)

    started = asyncio.Event()

    async def _hang(inv):
        started.set()
        await asyncio.Event().wait()

    exc = await _cancel_when(started, _drive(probe, _Driver(probe, execute=_hang),
                                             [_round(_tc("t"))]))
    await _finish(probe, exc)
    assert probe.records == before, "the cancelled invocation added or altered a record"
    assert len(probe.records) == 1
    assert probe.records[0].exit is LoopExit.terminal_action


async def test_the_cancelled_exit_has_no_terminal_status_of_its_own():
    assert not any("cancel" in m.name.lower() for m in LoopExit), (
        f"a cancellation exit was invented: {[m.name for m in LoopExit]}")


async def test_a_tool_that_raises_cancellation_itself_is_not_an_executor_failure(probe):
    async def _raises_cancel(inv):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _drive(probe, _Driver(probe, execute=_raises_cancel), [_round(_tc("t"))])

    assert probe.tool_invocations == 1
    assert probe.records == [], (
        "a tool that raised CancelledError was recorded as an executor failure")
    assert probe.breadcrumbs == []


async def test_a_close_out_that_raises_cancellation_itself_records_nothing(probe):
    async def _ok(inv):
        return ToolOutcome(content="ok")

    async def _close(tally):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _drive(probe, _Driver(probe, execute=_ok, close_out=_close), [_round(_tc("t"))])
    assert probe.records == [], "a cancelled close-out was recorded as an executor failure"
