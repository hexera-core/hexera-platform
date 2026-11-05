# Responsibility: Verify the builder's execution trace route publishes the same events, under ownership.
# Boundaries: the tracing boundary only - reviewer and intake keep the synchronous route.
from __future__ import annotations

import asyncio
import inspect
import uuid

import pytest

from meshpipeline.agents.loop import tracing as T
from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts.event_stream import ExecutionEventPublisher

#: (synchronous helper, execution helper, publisher method it must reach)
PAIRS = [("tool_call", "atool_call", "tool_call"),
         ("tool_result", "atool_result", "tool_result"),
         ("round_begin", "around_begin", "reasoning"),
         ("round_end", "around_end", "reasoning")]


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


def _args_for(name, t0):
    if name in ("tool_call",):
        return (("c1", "run_mesh", {"a": 1}), {"status": "started"})
    if name in ("tool_result",):
        return (("c1", "run_mesh", {"ok": True}, "success", t0), {})
    if name in ("round_begin",):
        return ((3,), {})
    return (("r:j:builder:1:3", t0, None), {"phase": "completed"})


# type separation


def test_the_execution_trace_context_admits_only_the_gated_contract():
    ann = T.ExecutionTraceContext.__dataclass_fields__["publisher"].type
    assert str(ann) in ("ExecutionEventPublisher", str(ExecutionEventPublisher)), ann
    assert T.ExecutionTraceContext.__dataclass_params__.frozen


def test_the_shared_trace_context_is_untouched():
    # reviewer and intake construct this one and must keep publishing synchronously
    assert T.TraceContext.__dataclass_fields__["publisher"].default is None
    assert str(T.TraceContext.__dataclass_fields__["publisher"].type) == "Any"


def test_intake_still_constructs_the_synchronous_context():
    import subprocess
    hits = subprocess.run(["grep", "-rln", "ExecutionTraceContext", "--include=*.py",
                           "src/meshpipeline"], capture_output=True, text=True).stdout
    # Intake is the one agent left on the synchronous route: it runs in the API request path,
    # outside any execution claim, so there is no ownership to check its publications against.
    strays = [f for f in hits.splitlines() if "/agents/intake/" in f]
    assert strays == [], f"intake reached for the execution trace context: {strays}"


@pytest.mark.parametrize("sync_name,exec_name,method", PAIRS)
def test_each_synchronous_helper_has_one_awaited_execution_counterpart(sync_name, exec_name,
                                                                      method):
    assert inspect.iscoroutinefunction(getattr(T, exec_name))
    assert not inspect.iscoroutinefunction(getattr(T, sync_name))
    sync_sig = inspect.signature(getattr(T, sync_name))
    exec_sig = inspect.signature(getattr(T, exec_name))
    drop = lambda s: [(n, p.kind, p.default) for n, p in list(s.parameters.items())[1:]]  # noqa: E731
    assert drop(exec_sig) == drop(sync_sig), (
        f"{exec_name} does not accept what {sync_name} accepts\n"
        f"  sync: {drop(sync_sig)}\n  exec: {drop(exec_sig)}")


# parity


@pytest.mark.parametrize("sync_name,exec_name,method", PAIRS)
def test_both_routes_publish_the_same_event(monkeypatch, sync_name, exec_name, method):
    import time
    t0 = time.monotonic()

    sync_inner = _Recorder()
    a, k = _args_for(sync_name, t0)
    getattr(T, sync_name)(T.TraceContext(publisher=sync_inner, job_id="j", role="builder",
                                         attempt=1), *a, **k)

    exec_inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=exec_inner.job_id))
    ctx = T.ExecutionTraceContext(publisher=OwnershipCheckedPublisher(exec_inner),
                                  job_id="j", role="builder", attempt=1)
    asyncio.run(getattr(T, exec_name)(ctx, *a, **k))

    assert len(sync_inner.calls) == 1 and len(exec_inner.calls) == 1
    s_name, s_a, s_k = sync_inner.calls[0]
    e_name, e_a, e_k = exec_inner.calls[0]
    assert s_name == e_name == method
    # duration_ms is measured; the gated wrapper also forwards protocol defaults explicitly,
    # so an omitted keyword and one passed as None are the same published payload.
    assert s_a[:3] == e_a[:3], f"{exec_name} changed the published arguments"
    norm = lambda d: {x: v for x, v in d.items()  # noqa: E731
                      if x != "duration_ms" and v is not None and v != ""}
    assert norm(s_k) == norm(e_k), (
        f"{exec_name} changed the published payload\n  sync: {s_k}\n  exec: {e_k}")


def test_round_begin_returns_the_same_reasoning_id_on_both_routes(monkeypatch):
    sync_rid = T.round_begin(T.TraceContext(publisher=_Recorder(), job_id="j", role="builder",
                                            attempt=1), 3)
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    exec_rid = asyncio.run(T.around_begin(
        T.ExecutionTraceContext(publisher=OwnershipCheckedPublisher(inner), job_id="j",
                                role="builder", attempt=1), 3))
    assert sync_rid == exec_rid != "", "the execution route changed the reasoning identity"


# authorization


@pytest.mark.parametrize("sync_name,exec_name,method", PAIRS)
@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_the_execution_route_refuses_without_ownership(monkeypatch, sync_name, exec_name,
                                                       method, case):
    import time
    inner = _Recorder()
    if case == "unbound":
        _bind(monkeypatch, own=None)
    elif case == "another job":
        _bind(monkeypatch, own=_Own(job_id="a-different-job"))
    else:
        _bind(monkeypatch, own=_Own(job_id=inner.job_id), current=False)

    ctx = T.ExecutionTraceContext(publisher=OwnershipCheckedPublisher(inner), job_id="j",
                                  role="builder", attempt=1)
    a, k = _args_for(sync_name, time.monotonic())
    with pytest.raises(StaleExecutionPublish):
        asyncio.run(getattr(T, exec_name)(ctx, *a, **k))
    assert inner.calls == [], f"{exec_name} published with {case} ownership"


@pytest.mark.parametrize("sync_name,exec_name,method", PAIRS)
def test_a_transport_failure_is_still_best_effort(monkeypatch, sync_name, exec_name, method):
    import time

    class _Broken(_Recorder):
        def tool_call(self, *a, **k):
            raise RuntimeError("redis is down")

        tool_result = reasoning = tool_call

    _bind(monkeypatch, own=_Own())
    ctx = T.ExecutionTraceContext(publisher=OwnershipCheckedPublisher(_Broken()), job_id="j",
                                  role="builder", attempt=1)
    a, k = _args_for(sync_name, time.monotonic())
    asyncio.run(getattr(T, exec_name)(ctx, *a, **k))      # must not raise


@pytest.mark.parametrize("sync_name,exec_name,method", PAIRS)
def test_the_execution_route_delegates_exactly_once(monkeypatch, sync_name, exec_name, method):
    import time
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    ctx = T.ExecutionTraceContext(publisher=OwnershipCheckedPublisher(inner), job_id="j",
                                  role="builder", attempt=1)
    a, k = _args_for(sync_name, time.monotonic())
    asyncio.run(getattr(T, exec_name)(ctx, *a, **k))
    assert len(inner.calls) == 1
