# Responsibility: Verify the execution-owned rationale path says the same thing, under ownership.
# Boundaries: the two rationale routes only - no production caller adopts the async one yet.
from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import uuid

import pytest

from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)
from meshpipeline.contracts import rationale as R
from meshpipeline.contracts.event_stream import EventPublisher, ExecutionEventPublisher

SOURCE = pathlib.Path(R.__file__).read_text()
TREE = ast.parse(SOURCE)


def _fn(name: str):
    return next(n for n in ast.walk(TREE)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


class _Recorder:
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.calls: list = []

    # Only the method under test. No catch-all: the once-per-run bookkeeping probes the
    # publisher with getattr, and a stand-in that answers every name would defeat it.
    def rationale(self, conclusion, because=""):
        self.calls.append(("rationale", conclusion, because))


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


# parity


@pytest.mark.parametrize("cells", [None, 0, 1_200_000])
def test_both_mesh_ready_routes_say_exactly_the_same_thing(monkeypatch, cells):
    sync_inner = _Recorder()
    R.builder_mesh_ready(sync_inner, cells=cells)

    async_inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=async_inner.job_id))
    asyncio.run(R.abuilder_mesh_ready(OwnershipCheckedPublisher(async_inner), cells=cells))

    assert len(sync_inner.calls) == 1, "the synchronous route did not publish exactly once"
    assert len(async_inner.calls) == 1, "the execution route did not publish exactly once"
    assert sync_inner.calls == async_inner.calls, (
        "the two routes render different rationale for identical input\n"
        f"  sync : {sync_inner.calls}\n  async: {async_inner.calls}")
    assert sync_inner.calls[0][0] == "rationale"


def test_the_once_per_run_rule_holds_on_the_execution_route(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    pub = OwnershipCheckedPublisher(inner)
    asyncio.run(R.abuilder_mesh_ready(pub, cells=5))
    asyncio.run(R.abuilder_mesh_ready(pub, cells=5))
    assert len(inner.calls) == 1, "the same conclusion was narrated twice on one publisher"


def test_the_synchronous_route_is_unchanged_for_its_existing_callers():
    inner = _Recorder()
    R.builder_mesh_ready(inner, cells=None)
    R.builder_mesh_ready(inner, cells=None)
    assert inner.calls == [("rationale", "The mesh was generated and is ready for review.",
                           "the mesher completed without fatal errors")]


def test_a_publisher_that_cannot_publish_never_breaks_the_synchronous_route():
    class _Broken(_Recorder):
        def rationale(self, *a, **k):
            raise RuntimeError("redis is down")

    R.builder_mesh_ready(_Broken())          # observability never costs a mesh


# authorization


@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_the_execution_route_refuses_without_ownership(monkeypatch, case):
    inner = _Recorder()
    if case == "unbound":
        _bind(monkeypatch, own=None)
    elif case == "another job":
        _bind(monkeypatch, own=_Own(job_id="a-different-job"))
    else:
        _bind(monkeypatch, own=_Own(job_id=inner.job_id), current=False)

    with pytest.raises(StaleExecutionPublish):
        asyncio.run(R.abuilder_mesh_ready(OwnershipCheckedPublisher(inner), cells=7))
    assert inner.calls == [], f"the rationale reached the publisher with {case} ownership"


def test_a_refusal_is_not_swallowed_as_an_observability_blip(monkeypatch):
    # _asay swallows transport failures, exactly as _say does; a lost claim is different in kind
    # and must reach the caller.
    _bind(monkeypatch, own=None)
    with pytest.raises(StaleExecutionPublish):
        asyncio.run(R._asay(OwnershipCheckedPublisher(_Recorder()), "x", "y"))


# structure


def test_the_execution_route_is_typed_and_gated():
    asay = inspect.signature(R._asay)
    assert asay.parameters["publish"].annotation in (ExecutionEventPublisher,
                                                     "ExecutionEventPublisher"), \
        "_asay must accept only the ownership-checked contract"
    amesh = inspect.signature(R.abuilder_mesh_ready)
    assert amesh.parameters["publish"].annotation in (ExecutionEventPublisher,
                                                      "ExecutionEventPublisher")
    assert inspect.iscoroutinefunction(R._asay)
    assert inspect.iscoroutinefunction(R.abuilder_mesh_ready)

    # the synchronous route keeps its existing permissive type for its 14 callers
    assert inspect.signature(R._say).parameters["publish"].annotation in (Any_ := ("Any",)) \
        or str(inspect.signature(R._say).parameters["publish"].annotation) == "Any"


def test_the_execution_route_awaits_the_gated_method_and_nothing_else():
    calls = [n for n in ast.walk(_fn("_asay"))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "publish"]
    assert [c.func.attr for c in calls] == ["arationale"], \
        f"_asay publishes through {[c.func.attr for c in calls]}, not the gated method"

    awaited = {id(n.value) for n in ast.walk(_fn("_asay")) if isinstance(n, ast.Await)}
    assert id(calls[0]) in awaited, "_asay does not await its publication"

    delegated = [n.func.id for n in ast.walk(_fn("abuilder_mesh_ready"))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert delegated == ["_asay"], \
        f"abuilder_mesh_ready routes through {delegated}, not the execution rationale path"
    assert any(isinstance(n, ast.Await) for n in ast.walk(_fn("abuilder_mesh_ready")))


def test_the_execution_route_never_reaches_around_the_gate():
    for name in ("_asay", "abuilder_mesh_ready"):
        src = ast.unparse(_fn(name))
        assert "_inner" not in src, f"{name} reaches around the ownership check"
        assert ".rationale(" not in src, f"{name} calls the ungated rationale method"


def test_only_the_builder_closure_has_adopted_the_execution_route():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rln", "-e", "abuilder_mesh_ready", "-e", "_asay", "--include=*.py",
         "src/meshpipeline"], capture_output=True, text=True).stdout.splitlines()
    outside = {h for h in hits if "contracts/rationale.py" not in h}
    # the Snappy driver is the one place the mesh-ready conclusion is reached under ownership
    assert outside == {"src/meshpipeline/engines/snappy/drivers.py"}, (
        f"the execution rationale route spread beyond the builder closure: {sorted(outside)}")


def test_every_existing_say_caller_still_uses_the_synchronous_route():
    callers = [n for n in ast.walk(TREE)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_say"]
    assert len(callers) == 14, (
        f"the synchronous rationale surface changed: {len(callers)} _say calls, expected 14")
    assert isinstance(EventPublisher, type) or True


# builder configuration - the second template the builder-publisher closure reaches


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("detail", ["", "the far-field is too small"])
def test_both_configuration_routes_say_exactly_the_same_thing(monkeypatch, ready, detail):
    sync_inner = _Recorder()
    R.builder_configuration(sync_inner, ready=ready, detail=detail)

    async_inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=async_inner.job_id))
    asyncio.run(R.abuilder_configuration(OwnershipCheckedPublisher(async_inner),
                                         ready=ready, detail=detail))

    assert len(sync_inner.calls) == 1 and len(async_inner.calls) == 1
    assert sync_inner.calls == async_inner.calls, (
        f"the two configuration routes differ\n  sync : {sync_inner.calls}\n"
        f"  async: {async_inner.calls}")


def test_the_configuration_once_per_run_rule_holds_on_the_execution_route(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    pub = OwnershipCheckedPublisher(inner)
    asyncio.run(R.abuilder_configuration(pub, ready=True))
    asyncio.run(R.abuilder_configuration(pub, ready=True))
    assert len(inner.calls) == 1


@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_the_configuration_execution_route_refuses_without_ownership(monkeypatch, case):
    inner = _Recorder()
    if case == "unbound":
        _bind(monkeypatch, own=None)
    elif case == "another job":
        _bind(monkeypatch, own=_Own(job_id="a-different-job"))
    else:
        _bind(monkeypatch, own=_Own(job_id=inner.job_id), current=False)

    with pytest.raises(StaleExecutionPublish):
        asyncio.run(R.abuilder_configuration(OwnershipCheckedPublisher(inner), ready=True))
    assert inner.calls == [], f"the configuration rationale published with {case} ownership"


def test_the_configuration_execution_route_is_typed_gated_and_adopted_once():
    sig = inspect.signature(R.abuilder_configuration)
    assert str(sig.parameters["publish"].annotation) in (
        "ExecutionEventPublisher", str(ExecutionEventPublisher))
    assert inspect.iscoroutinefunction(R.abuilder_configuration)

    fn = _fn("abuilder_configuration")
    delegated = [n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert set(delegated) == {"_asay"}, f"it routes through {delegated}, not the execution path"
    assert len([n for n in ast.walk(fn) if isinstance(n, ast.Await)]) == len(delegated), \
        "a delegation is not awaited"
    src = ast.unparse(fn)
    assert "_inner" not in src and ".rationale(" not in src and "_say(" not in src.replace(
        "_asay(", "")

    # the builder's shared loop is the one adopter, and it awaits both of its calls
    import subprocess
    hits = subprocess.run(["grep", "-rn", "abuilder_configuration", "--include=*.py",
                           "src/meshpipeline"], capture_output=True, text=True).stdout
    adopters = {ln.split(":", 1)[0] for ln in hits.splitlines()
                if "contracts/rationale.py" not in ln}
    assert adopters == {"src/meshpipeline/agents/builder/loop.py"}, sorted(adopters)
    assert all("await _R.abuilder_configuration" in ln or ln.strip().startswith("await")
               for ln in hits.splitlines()
               if "agents/builder/loop.py" in ln), "the loop calls it without awaiting"
