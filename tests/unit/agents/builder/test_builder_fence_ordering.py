# Responsibility: Verify every mutating tool is fenced before dispatch, and a lost fence stops the rest of the round.
# Boundaries: the roster is asserted against the production tools, so a new mutating tool cannot escape the fence.
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import meshpipeline.agents.builder.executor as bx
import meshpipeline.agents.builder.loop as bl
from meshpipeline.agents.builder.executor import SIDE_EFFECTING_TOOLS
from meshpipeline.application.execution_fence import StaleWorkerFenced
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)

SIDE_EFFECTING = {"write_file", "configure_mesh", "run_mesh", "run_python", "submit_mesh"}
READ_ONLY = {"read_file", "list_directory", "geometry_report", "measure_scales"}


def _tc(name, **args):
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=json.dumps(args))


def _round(*calls, text="", finish="tool_calls"):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text=text,
                            finish_reason=finish if calls else "stop",
                            provider=ProviderAttemptInfo(1, "p", "m"))


class _Trace:

    def __init__(self):
        self.events: list[str] = []
        self.fence_fails_at: set[str] = set()

    async def fence(self, where: str, **_kw):
        self.events.append(f"fence:{where}")
        for pattern in self.fence_fails_at:
            if pattern in where:
                raise StaleWorkerFenced(where, types.SimpleNamespace(
                    job_id="j", execution_generation=1, token_hash=lambda: "t"))

    def dispatch(self, ctx, fn, args):
        self.events.append(f"dispatch:{fn}")
        return json.dumps(self.results.get(fn, {"success": True}))

    results: dict = {}

    @property
    def dispatched(self) -> list[str]:
        return [e.split(":", 1)[1] for e in self.events if e.startswith("dispatch:")]

    @property
    def fences(self) -> list[str]:
        return [e.split(":", 1)[1] for e in self.events if e.startswith("fence:")]


@pytest.fixture
def trace(monkeypatch, tmp_path):
    t = _Trace()
    t.results = {}
    monkeypatch.setattr(bx._fence, "assert_current_owner", t.fence)
    monkeypatch.setattr(bx, "_dispatch_tool", t.dispatch)
    # `run_mesh` is driven in two halves so its announcement can be authorized on the event
    # loop. A harness that stubs the dispatch seam must stub both halves through it.
    import meshpipeline.agents.builder.tools.meshing as _meshing_mod
    monkeypatch.setattr(bx, "prepare_mesh_run",
                        lambda ctx: _meshing_mod.PreparedMeshRun(engine=ctx.engine, cap=60))
    monkeypatch.setattr(bx, "dispatch_prepared_mesh",
                        lambda ctx, prepared: bx._dispatch_tool(ctx, "run_mesh", {}))
    monkeypatch.setattr(bx, "_compress_tool_output", lambda fn, a, r, **k: r)
    monkeypatch.setattr(bx, "get_spec_run_files", lambda _e: ("system/meshDict",))
    monkeypatch.setattr(bl, "_active_tools", lambda _e: [])
    monkeypatch.setattr("meshpipeline.agents.builder.context_prep._count_message_tokens",
                        lambda _m: 0)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit", lambda rec: {})
    return t


def _drive(trace, script, tmp_path, monkeypatch, *, max_rounds=6):
    it = iter(script)

    async def _provider(**_kw):
        try:
            return next(it)
        except StopIteration:
            return _round(text="<<BUILDER_DONE>>")

    monkeypatch.setattr("meshpipeline.contracts.model_inference.call_builder_model", _provider)
    return asyncio.run(bl._run_tool_loop(
        [{"role": "system", "content": "s"}], Path(tmp_path), job_id="j", user_id="u",
        max_rounds=max_rounds, engine="cfmesh", loop_timeout=60))


# which tools are fenced, and when
@pytest.mark.parametrize("tool", sorted(SIDE_EFFECTING))
def test_every_side_effecting_tool_is_fenced_before_dispatch(tool, trace, tmp_path, monkeypatch):
    _drive(trace, [_round(_tc(tool))], tmp_path, monkeypatch)
    assert trace.events[0] == f"fence:builder tool {tool}"
    assert trace.events[1] == f"dispatch:{tool}", "the fence must precede the side effect"


def test_the_fence_parametrization_covers_the_production_roster_exactly():
    covered = set(
        test_every_side_effecting_tool_is_fenced_before_dispatch.pytestmark[0].args[1])
    assert covered == SIDE_EFFECTING_TOOLS, (
        "the parametrized roster drifted from production:\n"
        f"  untested in production roster: {sorted(SIDE_EFFECTING_TOOLS - covered)}\n"
        f"  tested but not side-effecting: {sorted(covered - SIDE_EFFECTING_TOOLS)}")
    assert SIDE_EFFECTING == SIDE_EFFECTING_TOOLS
    assert not (READ_ONLY & SIDE_EFFECTING_TOOLS), "a tool cannot be both"


@pytest.mark.parametrize("tool", sorted(READ_ONLY))
def test_read_only_tools_are_not_fenced(tool, trace, tmp_path, monkeypatch):
    _drive(trace, [_round(_tc(tool))], tmp_path, monkeypatch)
    assert trace.dispatched == [tool]
    assert trace.fences == [], "fencing a read-only tool would add a DB read per round"


def test_run_mesh_is_fenced_again_after_it_returns(trace, tmp_path, monkeypatch):
    trace.results = {"run_mesh": {"mesh_ok": False}}
    _drive(trace, [_round(_tc("run_mesh"))], tmp_path, monkeypatch)
    assert trace.events[:3] == ["fence:builder tool run_mesh", "dispatch:run_mesh",
                                "fence:accept native run_mesh output"]


def test_the_side_effecting_roster_is_exactly_the_mutating_tools():
    assert SIDE_EFFECTING_TOOLS == SIDE_EFFECTING


# losing the fence stops everything
def test_a_lost_fence_before_dispatch_stops_the_tool(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"builder tool run_mesh"}
    with pytest.raises(StaleWorkerFenced):
        _drive(trace, [_round(_tc("run_mesh"))], tmp_path, monkeypatch)
    assert trace.dispatched == [], "a superseded worker must not launch the native mesher"


def test_a_lost_fence_stops_every_later_call_in_the_same_round(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"builder tool configure_mesh"}
    with pytest.raises(StaleWorkerFenced):
        _drive(trace, [_round(_tc("configure_mesh"), _tc("run_mesh"), _tc("submit_mesh"))],
               tmp_path, monkeypatch)
    assert trace.dispatched == []


def test_a_lost_fence_after_a_native_run_stops_acceptance_and_later_calls(
        trace, tmp_path, monkeypatch):
    trace.results = {"run_mesh": {"mesh_ok": True}}
    trace.fence_fails_at = {"accept native run_mesh output"}
    with pytest.raises(StaleWorkerFenced):
        _drive(trace, [_round(_tc("run_mesh"), _tc("submit_mesh"))], tmp_path, monkeypatch)
    assert trace.dispatched == ["run_mesh"], "submit_mesh must not run after the fence is lost"


def test_a_stale_worker_publishes_no_success_after_losing_the_fence(
        trace, tmp_path, monkeypatch):
    published: list[str] = []
    # the gated contract the executor actually holds; every method is awaited
    class _Pub:
        def __init__(self, seen):
            self._seen = seen

        async def _rec(self, name):
            self._seen.append(name)

        async def aaction(self, *_a, **_k): await self._rec("action")
        async def ameshed(self, *_a, **_k): await self._rec("meshed")
        async def ameshing(self, *_a, **_k): await self._rec("meshing")
        async def awarn(self, *_a, **_k): await self._rec("warn")
        async def anote(self, *_a, **_k): await self._rec("note")
        async def afile(self, *_a, **_k): await self._rec("file")
        async def asearch(self, *_a, **_k): await self._rec("search")

    publisher = _Pub(published)
    trace.results = {"run_mesh": {"mesh_ok": True, "cells": 10}}
    trace.fence_fails_at = {"accept native run_mesh output"}

    async def _provider(**_kw):
        return _round(_tc("run_mesh"))
    monkeypatch.setattr("meshpipeline.contracts.model_inference.call_builder_model", _provider)
    with pytest.raises(StaleWorkerFenced):
        asyncio.run(bl._run_tool_loop(
            [{"role": "system", "content": "s"}], Path(tmp_path), job_id="j", user_id="u",
            max_rounds=4, engine="cfmesh", loop_timeout=60, publish=publisher))
    assert "meshed" not in published, "a fenced worker must not announce a completed mesh"


# terminal submission is final
def test_a_successful_submission_is_terminal_and_stops_later_calls(
        trace, tmp_path, monkeypatch):
    trace.results = {"submit_mesh": {"success": True}}
    out, _msgs = _drive(trace, [_round(_tc("submit_mesh"), _tc("run_mesh"))],
                        tmp_path, monkeypatch)
    assert out == "submit_mesh:success"
    assert trace.dispatched == ["submit_mesh"], "nothing runs after terminal submission"


def test_a_failed_submission_is_not_terminal(trace, tmp_path, monkeypatch):
    trace.results = {"submit_mesh": {"success": False}, "run_mesh": {"mesh_ok": False}}
    _drive(trace, [_round(_tc("submit_mesh"), _tc("run_mesh"))], tmp_path, monkeypatch)
    assert trace.dispatched == ["submit_mesh", "run_mesh"]


# ordering within one provider round
def test_calls_execute_in_provider_order_each_with_its_own_fence(
        trace, tmp_path, monkeypatch):
    trace.results = {"configure_mesh": {"success": True}, "run_mesh": {"mesh_ok": False}}
    _drive(trace, [_round(_tc("configure_mesh"), _tc("read_file"), _tc("run_mesh"))],
           tmp_path, monkeypatch)
    assert trace.dispatched == ["configure_mesh", "read_file", "run_mesh"]
    # each side-effecting call fenced individually; the read-only one not at all
    assert trace.fences == ["builder tool configure_mesh", "builder tool run_mesh",
                            "accept native run_mesh output"]


def test_one_provider_response_is_one_round_however_many_calls_it_carries(
        trace, tmp_path, monkeypatch):
    records: list = []
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: records.append(rec) or {})
    trace.results = {"run_mesh": {"mesh_ok": False}}
    _drive(trace, [_round(_tc("read_file"), _tc("list_directory"), _tc("run_mesh"))],
           tmp_path, monkeypatch, max_rounds=2)
    assert records, "a canonical run record must be emitted on exit"
    tally = records[-1].tally
    assert tally.tool_calls >= 3, "every call is counted"
    assert tally.rounds <= 2, "calls are not counted as rounds"
