# Responsibility: Verify the driver protocol's hooks are declared and called, and its three outcomes stay exclusive.
from __future__ import annotations

import asyncio
import inspect

import pytest

from meshpipeline.agents.loop.driver import LoopDriver, RoundDecision, ToolOutcome
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
        return {"k": 1}


class _Driver:

    role = AgentRole.builder

    def __init__(self, *, raises=None, supersession_type=None, close=None):
        self._raises = raises
        self._supersession_type = supersession_type
        self._close = close
        self.close_calls = 0

    def limits(self):
        return LoopLimits(max_rounds=3)

    def category_of(self, tool):
        return "x"

    async def execute(self, invocation):
        if self._raises is not None:
            raise self._raises
        return ToolOutcome(content="ok", accepted=True)

    def observe(self, tally):
        return ProgressObservation(made_progress=True, signature="s", detail="d")

    def correction(self, stage, tally, observation):
        return None

    def before_round(self, tally, messages):
        pass

    def forced_tool(self, tally):
        return None

    def on_plaintext(self, tally):
        return RoundDecision(message="call a tool", complete=False)

    def is_supersession(self, exc):
        return (self._supersession_type is not None
                and isinstance(exc, self._supersession_type))

    async def close_out(self, tally):
        self.close_calls += 1
        return self._close

    def extension(self):
        return _Ext()


class _Superseded(RuntimeError):
    pass


class _Broken(RuntimeError):
    pass


def _round(*names):
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=f"c{n}", name=n, arguments="{}") for n in names),
        assistant_text="", finish_reason="tool_calls" if names else "stop",
        provider=ProviderAttemptInfo(1, "p", "m"))


def _run(driver, monkeypatch, *, rounds=None):
    emitted, superseded = [], []
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: emitted.append(rec) or {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded",
                        lambda **kw: superseded.append(kw) or {})
    script = iter(rounds if rounds is not None else [_round("t")])

    async def _provider(**_kw):
        try:
            return next(script)
        except StopIteration:
            return _round()

    result = asyncio.run(run_agent_loop(
        driver=driver, provider_call=_provider, messages=[{"role": "system", "content": "s"}],
        tools=[], job_id="j", deadline_s=30.0,
        append_tool_result=lambda m, cid, c: m.append(
            {"role": "tool", "tool_call_id": cid, "content": c})))
    return result, emitted, superseded


# the hook is TYPED
def test_close_out_is_declared_on_the_protocol():
    assert "close_out" in LoopDriver.__dict__, "close_out must be part of the driver contract"
    sig = inspect.signature(LoopDriver.close_out)
    assert list(sig.parameters) == ["self", "tally"]
    assert inspect.iscoroutinefunction(LoopDriver.close_out)


def test_is_supersession_is_declared_on_the_protocol():
    assert "is_supersession" in LoopDriver.__dict__
    assert list(inspect.signature(LoopDriver.is_supersession).parameters) == ["self", "exc"]


def test_the_runner_calls_close_out_every_round(monkeypatch):
    d = _Driver()
    _run(d, monkeypatch, rounds=[_round("t"), _round("t")])
    assert d.close_calls == 2


def test_a_driver_close_out_can_end_the_run(monkeypatch):
    d = _Driver(close=ToolOutcome(content="done", accepted=True, terminal=True, payload="done"))
    result, emitted, _ = _run(d, monkeypatch)
    assert result.exit is LoopExit.terminal_action and result.payload == "done"
    assert len(emitted) == 1


def test_the_real_drivers_implement_the_hook_explicitly():
    from meshpipeline.agents.builder.loop_policy import BuilderLoopPolicy
    from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
    for policy in (BuilderLoopPolicy, ReviewLoopPolicy):
        assert "close_out" in policy.__dict__, f"{policy.__name__} must own its close-out"
        assert inspect.iscoroutinefunction(policy.close_out)


# three failures, told apart
def test_supersession_leaves_no_authoritative_record_and_propagates(monkeypatch):
    d = _Driver(raises=_Superseded("lost"), supersession_type=_Superseded)
    with pytest.raises(_Superseded):
        _run(d, monkeypatch)


def test_supersession_emits_exactly_one_non_authoritative_breadcrumb(monkeypatch):
    d = _Driver(raises=_Superseded("lost"), supersession_type=_Superseded)
    emitted, superseded = [], []
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: emitted.append(rec) or {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded",
                        lambda **kw: superseded.append(kw) or {})

    async def _provider(**_kw):
        return _round("t")

    with pytest.raises(_Superseded):
        asyncio.run(run_agent_loop(
            driver=d, provider_call=_provider, messages=[], tools=[], job_id="j",
            deadline_s=30.0, append_tool_result=lambda m, c, x: None))
    assert emitted == [], "no AgentRunRecord may describe a superseded generation"
    assert len(superseded) == 1
    assert superseded[0]["role"] is AgentRole.builder


def test_an_unexpected_executor_failure_still_produces_one_canonical_record(monkeypatch):
    d = _Driver(raises=_Broken("boom"), supersession_type=_Superseded)
    emitted, superseded = [], []
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: emitted.append(rec) or {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded",
                        lambda **kw: superseded.append(kw) or {})

    async def _provider(**_kw):
        return _round("t")

    with pytest.raises(_Broken):
        asyncio.run(run_agent_loop(
            driver=d, provider_call=_provider, messages=[], tools=[], job_id="j",
            deadline_s=30.0, append_tool_result=lambda m, c, x: None))
    assert len(emitted) == 1, "exactly one canonical record"
    assert emitted[0].exit is LoopExit.executor_failed
    assert superseded == [], "a broken tool is not a supersession"


def test_a_normal_policy_failure_is_neither_of_the_two(monkeypatch):
    class _Stuck(_Driver):
        def limits(self):
            return LoopLimits(max_rounds=6, no_progress_threshold=2)

        def observe(self, tally):
            return ProgressObservation(made_progress=False, signature="same", detail="stuck")

    d = _Stuck()
    result, emitted, superseded = _run(
        d, monkeypatch, rounds=[_round("t"), _round("t"), _round("t")])
    assert result.exit is LoopExit.no_progress
    assert len(emitted) == 1 and emitted[0].exit is LoopExit.no_progress
    assert superseded == []


def test_the_three_outcomes_are_mutually_exclusive(monkeypatch):
    seen = set()
    for drv, exc in [(_Driver(raises=_Broken("b"), supersession_type=_Superseded), _Broken)]:
        emitted: list = []
        monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                            lambda rec: emitted.append(rec) or {})
        monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded",
                            lambda **kw: {})

        async def _provider(**_kw):
            return _round("t")

        with pytest.raises(exc):
            asyncio.run(run_agent_loop(
                driver=drv, provider_call=_provider, messages=[], tools=[], job_id="j",
                deadline_s=30.0, append_tool_result=lambda m, c, x: None))
        seen.add(emitted[0].exit)
    assert seen == {LoopExit.executor_failed}
    assert LoopExit.executor_failed is not LoopExit.no_progress
    assert not hasattr(LoopExit, "superseded"), (
        "supersession must NOT be an exit reason - it produces no authoritative record")
