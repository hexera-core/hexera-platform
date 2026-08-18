# Responsibility: Verify the mesh run announces itself under ownership, on the loop, before the mesher starts.
# Boundaries: the run_mesh announcement boundary only - the rest of the builder closure is proven elsewhere.
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import threading
import uuid

import pytest

import meshpipeline.agents.builder.executor as bx
from meshpipeline.agents.builder.tools import meshing as M
from meshpipeline.application.execution_publisher import (
    OwnershipCheckedPublisher,
    StaleExecutionPublish,
)


class _Recorder:
    def __init__(self, job_id="job-1"):
        self.job_id = job_id
        self.calls: list = []

    def meshing(self, *a, **k):
        self.calls.append(("meshing", a, k))


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


def _executor(monkeypatch, publisher, *, prepared=None, refusal=None, ran=None,
              threads=None):
    ctx = type("Ctx", (), {"job_id": "job-1", "engine": "cfmesh", "workspace": None,
                           "loop_deadline": None})()

    def _prepare(_ctx):
        if threads is not None:
            threads.append(("prepare", threading.current_thread() is threading.main_thread()))
        if refusal is not None:
            return M.PreparedMeshRun(refusal=refusal)
        return prepared or M.PreparedMeshRun(engine="cfmesh", cap=600, purpose="external",
                                             history={"p50": 420})

    def _execute(_ctx, _prepared):
        if threads is not None:
            threads.append(("execute", threading.current_thread() is threading.main_thread()))
        if ran is not None:
            ran.append(_prepared)
        return json.dumps({"success": True, "cells": 10})

    monkeypatch.setattr(bx, "prepare_mesh_run", _prepare)
    monkeypatch.setattr(bx, "dispatch_prepared_mesh", _execute)
    ex = bx.BuilderToolExecutor(context=ctx, publish=publisher)
    return ex


# the announcement


def test_the_run_announces_itself_once_before_the_mesher_starts(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    ran: list = []
    ex = _executor(monkeypatch, OwnershipCheckedPublisher(inner), ran=ran)

    raw = asyncio.run(ex._run_announced_mesh())

    assert len(inner.calls) == 1, f"announced {len(inner.calls)} times, not once"
    assert len(ran) == 1, "the mesher did not run exactly once"
    assert json.loads(raw)["success"] is True


def test_the_announcement_carries_the_prepared_engine_cap_and_estimate(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    prepared = M.PreparedMeshRun(engine="snappy", cap=1234, purpose="internal",
                                 history={"p50": 99})
    ex = _executor(monkeypatch, OwnershipCheckedPublisher(inner), prepared=prepared)
    asyncio.run(ex._run_announced_mesh())

    _name, args, _kw = inner.calls[0]
    assert args[0] == "snappy", "the announced engine is not the prepared one"
    assert args[1] == 1234, "the announced cap is not the prepared one"
    assert args[2] == {"p50": 99}, "the announced estimate is not the prepared one"


@pytest.mark.parametrize("history", [None, {}, {"p50": 420}])
def test_an_absent_history_is_announced_as_absent_never_invented(monkeypatch, history):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    ex = _executor(monkeypatch, OwnershipCheckedPublisher(inner),
                   prepared=M.PreparedMeshRun(engine="cfmesh", cap=60, history=history))
    asyncio.run(ex._run_announced_mesh())
    assert inner.calls[0][1][2] == history


# the boundary


def test_a_refusal_before_the_boundary_announces_nothing_and_runs_nothing(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    ran: list = []
    ex = _executor(monkeypatch, OwnershipCheckedPublisher(inner), ran=ran,
                   refusal={"success": False, "error": "missing system/meshDict"})

    raw = asyncio.run(ex._run_announced_mesh())

    assert inner.calls == [], "a run refused before the boundary still announced itself"
    assert ran == [], "a run refused before the boundary still reached the mesher"
    assert json.loads(raw)["error"] == "missing system/meshDict"


@pytest.mark.parametrize("case", ["unbound", "another job", "no longer the owner"])
def test_a_lost_claim_refuses_before_the_mesher_is_submitted(monkeypatch, case):
    inner = _Recorder()
    if case == "unbound":
        _bind(monkeypatch, own=None)
    elif case == "another job":
        _bind(monkeypatch, own=_Own(job_id="a-different-job"))
    else:
        _bind(monkeypatch, own=_Own(job_id=inner.job_id), current=False)

    ran: list = []
    ex = _executor(monkeypatch, OwnershipCheckedPublisher(inner), ran=ran)

    with pytest.raises(StaleExecutionPublish):
        asyncio.run(ex._run_announced_mesh())

    assert inner.calls == [], f"{case}: the announcement reached the publisher"
    assert ran == [], f"{case}: the mesher was submitted after a refused announcement"


def test_a_run_without_a_publisher_still_meshes(monkeypatch):
    ran: list = []
    ex = _executor(monkeypatch, None, ran=ran)
    asyncio.run(ex._run_announced_mesh())
    assert len(ran) == 1


# threading


def test_both_halves_stay_off_the_event_loop_and_the_announcement_stays_on_it(monkeypatch):
    inner = _Recorder()
    _bind(monkeypatch, own=_Own(job_id=inner.job_id))
    threads: list = []
    ex = _executor(monkeypatch, OwnershipCheckedPublisher(inner), threads=threads)

    asyncio.run(ex._run_announced_mesh())

    assert [n for n, _ in threads] == ["prepare", "execute"], threads
    assert all(is_main is False for _n, is_main in threads), (
        "a blocking half ran on the event-loop thread - the mesher would block the worker")


def test_run_mesh_is_still_offloaded():
    assert "run_mesh" in bx._OFFLOADED_TOOLS


# structure


SRC = pathlib.Path(bx.__file__).resolve().parents[3] / "meshpipeline"


def test_the_meshing_tool_no_longer_constructs_or_publishes_anything():
    tree = ast.parse((SRC / "agents/builder/tools/meshing.py").read_text())
    built = [n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ("publisher", "execution_publisher")]
    assert built == [], f"the meshing tool still constructs a publisher at {built}"
    published = [f"{n.func.attr}@{n.lineno}" for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr in ("meshing", "ameshing")]
    assert published == [], f"the meshing tool still publishes: {published}"


def test_the_executor_awaits_the_announcement_between_the_two_offloads():
    tree = ast.parse((SRC / "agents/builder/executor.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_announced_mesh")
    marks = []
    for node in ast.walk(fn):          # ast.walk is breadth-first, so order by position
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "to_thread":
                marks.append((node.lineno, ast.unparse(node.args[0]) if node.args
                              else "to_thread"))
            elif node.func.attr == "ameshing":
                marks.append((node.lineno, "ameshing"))
    order = [name for _line, name in sorted(marks)]
    assert order == ["prepare_mesh_run", "ameshing", "dispatch_prepared_mesh"], (
        f"the announcement is not between the two offloads: {order}")

    awaited = {id(n.value) for n in ast.walk(fn) if isinstance(n, ast.Await)}
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "ameshing"]
    assert len(calls) == 1 and id(calls[0]) in awaited, "the announcement is not awaited once"


# payload parity against the REAL preparer - a stub cannot prove the policy is unchanged


def _real_context(tmp_path, engine="cfmesh"):
    from meshpipeline.agents.builder.tool_context import BuilderToolContext
    from meshpipeline.engines.registry import get_spec
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for rel in get_spec(engine).run_policy.required_files:
        f = ws / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    return BuilderToolContext(workspace=ws, geometry=object(), job_id="job-1",
                              execution_id="e", execution_generation=1, engine=engine,
                              mesh_fidelity="", loop_deadline=None)


@pytest.mark.parametrize("engine", ["cfmesh", "snappy"])
def test_the_prepared_values_are_the_engine_policy_not_a_restatement(monkeypatch, tmp_path,
                                                                     engine):
    from meshpipeline.engines.registry import get_spec
    from meshpipeline.engines.workspace_facts import read_purpose
    monkeypatch.setattr(M, "input_contract_rejection", lambda *a, **k: "")

    ctx = _real_context(tmp_path, engine)
    prepared = M.prepare_mesh_run(ctx)

    assert prepared.refusal is None, prepared.refusal
    assert prepared.engine == ctx.engine, "the announcement would name a different engine"
    assert prepared.cap == get_spec(engine).run_policy.run_timeout(), (
        "the announced cap is not the engine-declared run budget")
    assert prepared.purpose == read_purpose(ctx.workspace)
    from meshpipeline.engines.mesh_history import estimate
    assert prepared.history == estimate(prepared.engine, prepared.purpose), (
        "the announced estimate is not the measured history for this engine and purpose")


def test_the_prepared_cap_is_what_the_run_is_actually_given(monkeypatch, tmp_path):
    # the same value reaches the event and the mesher, so the two can never disagree
    monkeypatch.setattr(M, "input_contract_rejection", lambda *a, **k: "")
    ctx = _real_context(tmp_path)
    prepared = M.prepare_mesh_run(ctx)

    seen: dict = {}

    class _R:
        name = "cfmesh"

        @staticmethod
        def run_cartesian_mesh(workspace, *, timeout, context):
            seen["timeout"] = timeout
            return {"rc": 0, "timed_out": False, "log_tail": ""}

        @staticmethod
        def check_mesh(workspace):
            return {"cells": 1, "mesh_ok": True, "fatal": []}

    monkeypatch.setattr("meshpipeline.engines.runtime.get_engine", lambda _e: _R)
    M.execute_prepared_mesh_run(ctx, prepared)
    assert seen["timeout"] == prepared.cap
