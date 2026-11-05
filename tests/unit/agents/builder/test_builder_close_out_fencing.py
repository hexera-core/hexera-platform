# Responsibility: Verify automatic submission at close-out is fenced, so a superseded worker publishes no success.
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import meshpipeline.agents.builder.executor as bx
import meshpipeline.agents.builder.loop as bl
import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.application.execution_fence import StaleWorkerFenced
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)


def _tc(name, **args):
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=json.dumps(args))


def _round(*calls):
    return ModelRoundResult(tool_calls=tuple(calls), assistant_text="",
                            finish_reason="tool_calls" if calls else "stop",
                            provider=ProviderAttemptInfo(1, "p", "m"))


class _Owner:

    def __init__(self):
        self.superseded = False
        self.fenced_for: list[str] = []
        self.dispatched: list[str] = []
        self.published: list[str] = []
        self.records: list = []
        self.superseded_events: list = []

    async def fence(self, where, **_kw):
        if self.superseded:
            raise StaleWorkerFenced(where, types.SimpleNamespace(
                job_id="j", execution_generation=2, token_hash=lambda: "t"))
        self.fenced_for.append(where)

    def dispatch(self, ctx, fn, args):
        self.dispatched.append(fn)
        if fn == "run_mesh":
            return json.dumps({"mesh_ok": True, "cells": 10})
        if fn == "submit_mesh":
            return json.dumps({"success": True})
        return json.dumps({"content": "x"})


@pytest.fixture
def owner(monkeypatch):
    o = _Owner()
    monkeypatch.setattr(bx._fence, "assert_current_owner", o.fence)
    monkeypatch.setattr(bx, "_dispatch_tool", o.dispatch)
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
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: o.records.append(rec) or {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded",
                        lambda **kw: o.superseded_events.append(kw) or {})
    return o


def _drive(owner, script, tmp_path, monkeypatch, *, max_rounds=8, publish=None):
    it = iter(script)

    async def _provider(**_kw):
        try:
            return next(it)
        except StopIteration:
            return _round()

    monkeypatch.setattr("meshpipeline.contracts.model_inference.call_builder_model", _provider)
    return asyncio.run(bl._run_tool_loop(
        [{"role": "system", "content": "s"}], Path(tmp_path), job_id="j", user_id="u",
        max_rounds=max_rounds, engine="cfmesh", loop_timeout=60, publish=publish))


# the audit probe, as a test
def test_a_superseded_worker_cannot_auto_submit(owner, tmp_path, monkeypatch):
    assert bcfg.BUILDER_AUTO_SUBMIT_AFTER == 2
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

    def _lose_lease_after_second_mesh(ctx, fn, args):
        out = owner.dispatch(ctx, fn, args)
        if owner.dispatched.count("run_mesh") == 2:
            owner.superseded = True
        return out
    monkeypatch.setattr(bx, "_dispatch_tool", _lose_lease_after_second_mesh)

    with pytest.raises(StaleWorkerFenced):
        _drive(owner, [_round(_tc("run_mesh")), _round(_tc("run_mesh")),
                       _round(_tc("read_file", path="a"))],
               tmp_path, monkeypatch, publish=publisher)

    assert "submit_mesh" not in owner.dispatched, "a superseded worker must not submit"
    assert owner.dispatched.count("run_mesh") == 2
    assert "note" not in published or all("Auto-submitting" not in str(p) for p in published)


def test_a_superseded_close_out_reports_no_success_and_no_authoritative_record(
        owner, tmp_path, monkeypatch):
    def _lose_lease_after_second_mesh(ctx, fn, args):
        out = owner.dispatch(ctx, fn, args)
        if owner.dispatched.count("run_mesh") == 2:
            owner.superseded = True
        return out
    monkeypatch.setattr(bx, "_dispatch_tool", _lose_lease_after_second_mesh)

    with pytest.raises(StaleWorkerFenced):
        _drive(owner, [_round(_tc("run_mesh")), _round(_tc("run_mesh")),
                       _round(_tc("read_file", path="a"))], tmp_path, monkeypatch)

    assert owner.records == [], "a superseded generation writes no authoritative record"
    assert len(owner.superseded_events) == 1, "exactly one non-authoritative breadcrumb"
    ev = owner.superseded_events[0]
    assert ev["role"].value == "builder"
    assert ev["job_id"] == "j"


def test_the_owning_worker_still_auto_submits(owner, tmp_path, monkeypatch):
    out, _msgs = _drive(owner, [_round(_tc("run_mesh")), _round(_tc("run_mesh"))],
                        tmp_path, monkeypatch)
    assert out == "submit_mesh:success"
    assert owner.dispatched.count("submit_mesh") == 1, "exactly one submission"
    ext = owner.records[-1].extension
    assert ext.auto_submitted is True and ext.submitted is True


def test_automatic_submission_is_fenced_before_it_dispatches(owner, tmp_path, monkeypatch):
    _drive(owner, [_round(_tc("run_mesh")), _round(_tc("run_mesh"))], tmp_path, monkeypatch)
    submit_fence = owner.fenced_for.index("builder tool submit_mesh")
    # every fence recorded before the submit fence belongs to an earlier action
    assert owner.dispatched.index("submit_mesh") == 2
    assert submit_fence == len(owner.fenced_for) - 1


def test_explicit_and_automatic_submission_use_the_same_contract(owner, tmp_path, monkeypatch):
    explicit, _ = _drive(owner, [_round(_tc("run_mesh")), _round(_tc("submit_mesh"))],
                         tmp_path, monkeypatch)
    explicit_fences = list(owner.fenced_for)

    o2 = _Owner()
    monkeypatch.setattr(bx._fence, "assert_current_owner", o2.fence)
    monkeypatch.setattr(bx, "_dispatch_tool", o2.dispatch)
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: o2.records.append(rec) or {})
    automatic, _ = _drive(o2, [_round(_tc("run_mesh")), _round(_tc("run_mesh"))],
                          tmp_path, monkeypatch)

    assert explicit == automatic == "submit_mesh:success"
    assert "builder tool submit_mesh" in explicit_fences
    assert "builder tool submit_mesh" in o2.fenced_for
    assert o2.dispatched.count("submit_mesh") == 1


def test_a_failed_deliverable_is_not_auto_submitted_even_when_owned(
        owner, tmp_path, monkeypatch):
    def _no_deliverable(ctx, fn, args):
        owner.dispatched.append(fn)
        if fn == "run_mesh":
            return json.dumps({"mesh_ok": True})
        if fn == "submit_mesh":
            return json.dumps({"success": False, "error": "no polyMesh"})
        return json.dumps({})
    monkeypatch.setattr(bx, "_dispatch_tool", _no_deliverable)

    out, _ = _drive(owner, [_round(_tc("run_mesh"))] * 4, tmp_path, monkeypatch, max_rounds=5)
    assert out == "", "an unready deliverable never produces a terminal success"
    assert owner.records[-1].extension.auto_submitted is False
