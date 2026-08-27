# Responsibility: Verify the deterministic snappy driver fences every authoritative operation and fabricates no round.
# Boundaries: a superseded driver writes no record, and a stale readiness marker cannot manufacture success.
from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import pytest

import meshpipeline.engines.snappy.drivers as drv
from meshpipeline.agents.builder.driver_run import (
    STRATEGY_CANONICAL_LOOP,
    STRATEGY_ENGINE_DRIVER,
    TERMINAL_SUCCESS,
    BuilderDriverRun,
)
from meshpipeline.application.execution_fence import StaleWorkerFenced
from meshpipeline.contracts.agent_loop import LoopExit
from meshpipeline.contracts.model_inference import ModelRoundResult, ProviderAttemptInfo


class _Trace:

    def __init__(self, *, fence_fails_at=None, mesh_good=True, rc=0):
        self.events: list[str] = []
        self.fence_fails_at = set(fence_fails_at or ())
        self.mesh_good = mesh_good
        self.rc = rc
        self.published: list[str] = []
        self.records: list = []
        self.superseded: list = []

    async def fence(self, where, **_kw):
        self.events.append(f"fence:{where}")
        for pat in self.fence_fails_at:
            if pat in where:
                raise StaleWorkerFenced(where, types.SimpleNamespace(
                    job_id="j", execution_generation=2, token_hash=lambda: "t"))

    def op(self, name):
        self.events.append(f"op:{name}")

    @property
    def ops(self):
        return [e.split(":", 1)[1] for e in self.events if e.startswith("op:")]

    @property
    def fences(self):
        return [e.split(":", 1)[1] for e in self.events if e.startswith("fence:")]


# The gated contract, spelled out. No catch-all: the rationale path probes the publisher with
# getattr for its once-per-run bookkeeping, and a stand-in that answers every name defeats it.
class _Publish:
    def __init__(self, trace):
        self._t = trace

    def _rec(self, name):
        self._t.published.append(name)

    async def anote(self, *_a, **_k): self._rec("note")
    async def awarn(self, *_a, **_k): self._rec("warn")
    async def aerror(self, *_a, **_k): self._rec("error")
    async def astage(self, *_a, **_k): self._rec("stage")
    async def aattempt(self, *_a, **_k): self._rec("attempt")
    async def acheck(self, *_a, **_k): self._rec("check")
    async def aaction(self, *_a, **_k): self._rec("action")
    async def asearch(self, *_a, **_k): self._rec("search")
    async def ascreenshot(self, *_a, **_k): self._rec("screenshot")
    async def afile(self, *_a, **_k): self._rec("file")
    async def areasoning(self, *_a, **_k): self._rec("reasoning")
    async def arationale(self, *_a, **_k): self._rec("rationale")
    async def atool_call(self, *_a, **_k): self._rec("tool_call")
    async def atool_result(self, *_a, **_k): self._rec("tool_result")
    async def ameshing(self, *_a, **_k): self._rec("meshing")
    async def ameshed(self, *_a, **_k): self._rec("meshed")
    async def averdict(self, *_a, **_k): self._rec("verdict")
    async def aclosing(self, *_a, **_k): self._rec("closing")


@pytest.fixture
def trace(monkeypatch, tmp_path):
    t = _Trace()
    (tmp_path / "input.stl").write_text("solid x\nendsolid x\n")

    # the native/CAD toolchain, stubbed at the engine seam
    class _R:
        @staticmethod
        def detect_symmetry_plane(*_a, **_k):
            return None

        @staticmethod
        def domain_from_strategy(*_a, **_k):
            return ([0, 0, 0], [1, 1, 1])

        @staticmethod
        def prepare_surface(*_a, **_k):
            t.op("prepare_surface")
            return {"surface_name": "body", "feature_file": "body.eMesh"}

        @staticmethod
        def render_snappy_case(*_a, **_k):
            t.op("render_case")
            return {"surface_level": 2, "n_layers": 3}

        @staticmethod
        def run_snappy(*_a, **_k):
            t.op("run_native")
            return {"rc": t.rc}

        @staticmethod
        def check_mesh(*_a, **_k):
            t.op("check_mesh")
            return {"cells": 1000, "fatal": [] if t.mesh_good else ["x"],
                    "skew_fraction": 0.0, "skew_faces": 0}

        @staticmethod
        def _patch_face_counts(*_a, **_k):
            return {"body": 500 if t.mesh_good else 0, "farfield": 100}

    monkeypatch.setattr("meshpipeline.engines.snappy.snappy_runner.detect_symmetry_plane",
                        _R.detect_symmetry_plane, raising=False)
    import meshpipeline.engines.snappy.snappy_runner as _sr
    for name in ("domain_from_strategy", "prepare_surface", "render_snappy_case", "run_snappy",
                 "check_mesh", "_patch_face_counts", "detect_symmetry_plane"):
        monkeypatch.setattr(_sr, name, getattr(_R, name), raising=False)

    monkeypatch.setattr("meshpipeline.cad.analysis.analyze_surface",
                        lambda *_a, **_k: {"diag": 1.0, "extent": [1, 1, 1], "surface_area": 1.0,
                                           "min_feature": 0.01})
    monkeypatch.setattr("meshpipeline.cad.analysis.recommend_refinement",
                        lambda *_a, **_k: {"surface_level": 2, "feature_level": 3,
                                           "afford_level": 2})
    monkeypatch.setattr("meshpipeline.engines.workspace_facts.contract_wall_patch",
                        lambda *_a, **_k: "body")
    monkeypatch.setattr(drv, "read_purpose", lambda *_a, **_k: "external")
    monkeypatch.setattr("meshpipeline.engines.mesh_history.estimate", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr("meshpipeline.engines.mesh_history.record", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 1, raising=False)

    # diagnostics sinks
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit",
                        lambda rec: t.records.append(rec) or {})
    monkeypatch.setattr("meshpipeline.agents.loop.diagnostics.emit_superseded",
                        lambda **kw: t.superseded.append(kw) or {})
    return t


def _run_for(trace, tmp_path, monkeypatch, *, plan_rounds=1, plan_fails=False):
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial", deadline_s=60.0)
    monkeypatch.setattr(run, "fence", trace.fence)

    calls = {"n": 0}

    async def _plan(**_kw):
        calls["n"] += 1
        if calls["n"] > plan_rounds:
            return drv_planner.PlanOutcome()
        rr = ModelRoundResult(tool_calls=(), assistant_text='{"approach":"a","max_cells":100000}',
                              finish_reason="stop", provider=ProviderAttemptInfo(1, "p", "m"),
                              input_tokens=10, output_tokens=5,
                              failure_marker="planner_down" if plan_fails else "")
        if plan_fails:
            return drv_planner.PlanOutcome(None, rr, "planner_down")
        return drv_planner.PlanOutcome({"approach": "a", "max_cells": 100000}, rr)

    import meshpipeline.engines.snappy.planner as drv_planner
    monkeypatch.setattr(drv, "plan_with_accounting", _plan, raising=False)
    monkeypatch.setattr(drv_planner, "plan_with_accounting", _plan)
    monkeypatch.setattr("meshpipeline.engines.snappy.planner.clamp_cell_budget",
                        lambda v, ceiling=None: v or 100000)

    # The driver measures PHYSICAL size, so the run's geometry has to be present and has to say
    # what its coordinates mean. These fixtures write their surface directly in metres, so the
    # interpretation states metres from an .stl source - no conversion, and nothing assumed.
    from tests._geometry_support import geometry_state as _geometry_state

    from meshpipeline.contracts.geometry_units import LengthUnit
    state = {"builder_mode": "initial", "engine": "snappy", "request_txt": "r",
             "intake_patches": [], "dimensionality": "3D", "flow_topology": "external",
             "geometry": _geometry_state(Path(tmp_path) / "_src", unit=LengthUnit.metre,
                                         filename="body.stl")}
    return run, asyncio.run(drv.drive(Path(tmp_path), state, job_id="j",
                                      publish=_Publish(trace), run=run))


# strategy selection
def test_only_snappy_selects_the_deterministic_driver():
    from meshpipeline.engines.registry import get_spec
    resolved = {e: bool(get_spec(e).build_driver)
                for e in ("cfmesh", "snappy", "gmsh", "vmtk", "snappy_multiregion")}
    assert resolved == {"cfmesh": False, "snappy": True, "gmsh": False,
                        "vmtk": False, "snappy_multiregion": False}


def test_the_two_strategies_are_mutually_exclusive_and_labelled():
    assert STRATEGY_ENGINE_DRIVER != STRATEGY_CANONICAL_LOOP
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial")
    assert run.extension().strategy == STRATEGY_ENGINE_DRIVER
    from meshpipeline.agents.builder.loop_policy import BuilderLoopPolicy
    from meshpipeline.contracts.agent_loop import LoopLimits
    pol = BuilderLoopPolicy(engine="cfmesh", mode="initial", limits_=LoopLimits(), executor=None)
    assert pol.extension().strategy == STRATEGY_CANONICAL_LOOP


def test_the_driver_contains_no_fallback_to_the_canonical_loop():
    import inspect
    src = inspect.getsource(drv)
    assert "run_agent_loop" not in src, "the deterministic strategy must not call the agent loop"
    assert "_run_tool_loop" not in src


# fence ordering
def test_the_fence_sequence_covers_every_authoritative_operation(trace, tmp_path, monkeypatch):
    run, (ok, value, outcome) = _run_for(trace, tmp_path, monkeypatch)
    assert ok is True and value == TERMINAL_SUCCESS
    assert trace.fences == [
        "write plan memory",
        "author mesh specification",
        "start native mesh",
        "accept native mesh output",
        "deliver builder outcome",
    ]


def test_the_native_run_is_bracketed_by_fences(trace, tmp_path, monkeypatch):
    _run_for(trace, tmp_path, monkeypatch)
    seq = [e for e in trace.events
           if e in ("fence:start native mesh", "op:run_native", "fence:accept native mesh output",
                    "op:check_mesh")]
    assert seq == ["fence:start native mesh", "op:run_native",
                   "fence:accept native mesh output", "op:check_mesh"]


def test_the_plan_memory_write_is_fenced_before_it_happens(trace, tmp_path, monkeypatch):
    _run_for(trace, tmp_path, monkeypatch)
    assert trace.fences[0] == "write plan memory"
    assert (tmp_path / ".last_plan.json").exists()


# lost ownership stops work
def test_lost_ownership_before_the_plan_write_prevents_the_write(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"write plan memory"}
    with pytest.raises(StaleWorkerFenced):
        _run_for(trace, tmp_path, monkeypatch)
    assert not (tmp_path / ".last_plan.json").exists()
    assert trace.ops == [], "no domain operation may run after ownership is lost"


def test_lost_ownership_before_native_execution_prevents_it(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"start native mesh"}
    with pytest.raises(StaleWorkerFenced):
        _run_for(trace, tmp_path, monkeypatch)
    assert "run_native" not in trace.ops, "a superseded worker must not start the mesher"


def test_lost_ownership_after_native_execution_prevents_acceptance(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"accept native mesh output"}
    with pytest.raises(StaleWorkerFenced):
        _run_for(trace, tmp_path, monkeypatch)
    assert "run_native" in trace.ops
    assert "check_mesh" not in trace.ops, "the result must not be judged"
    assert "meshed" not in trace.published, "no mesh-success event"


def test_lost_ownership_prevents_terminal_builder_success(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"deliver builder outcome"}
    with pytest.raises(StaleWorkerFenced):
        _run_for(trace, tmp_path, monkeypatch)
    # the work completed, but the outcome is not this generation's to return
    assert "run_native" in trace.ops


def test_a_superseded_driver_writes_no_authoritative_record(trace, tmp_path, monkeypatch):
    trace.fence_fails_at = {"accept native mesh output"}
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="initial")
    monkeypatch.setattr(run, "fence", trace.fence)
    with pytest.raises(StaleWorkerFenced):
        _run_for(trace, tmp_path, monkeypatch)
    assert trace.records == [], "a superseded generation appends no authoritative state"


# diagnostics
def test_a_successful_build_emits_one_truthful_record(trace, tmp_path, monkeypatch):
    run, (ok, value, outcome) = _run_for(trace, tmp_path, monkeypatch)
    record = run.finish(exit=outcome.exit, failure_marker=outcome.failure_marker)
    assert len(trace.records) == 1 and trace.records[0] is record
    ext = record.extension
    assert ext.strategy == STRATEGY_ENGINE_DRIVER and ext.engine == "snappy"
    assert ext.authored_spec is True
    assert ext.run_mesh_calls == 1 and ext.run_mesh_successes == 1
    assert ext.submitted is True
    assert record.exit is LoopExit.terminal_action


def test_a_failed_native_build_emits_one_record_and_no_delivery(trace, tmp_path, monkeypatch):
    trace.mesh_good = False
    run, (ok, value, outcome) = _run_for(trace, tmp_path, monkeypatch)
    assert ok is False and value != TERMINAL_SUCCESS
    run.finish(exit=outcome.exit, failure_marker=outcome.failure_marker)
    assert len(trace.records) == 1
    rec = trace.records[0]
    assert rec.exit is LoopExit.attempts_exhausted
    assert rec.extension.submitted is False
    assert rec.extension.run_mesh_calls == 1 and rec.extension.run_mesh_successes == 0


def test_interactive_loop_concepts_are_never_fabricated(trace, tmp_path, monkeypatch):
    run, (_ok, _v, outcome) = _run_for(trace, tmp_path, monkeypatch)
    ext = run.extension()
    assert ext.forced_tools == (), "a deterministic driver forces no tools"
    assert ext.repeated_tool_signature == ""
    assert ext.truncated_rounds == 0
    assert ext.checkpoint_recoveries == 0
    assert ext.auto_submitted is False


def test_the_record_carries_no_prompt_reasoning_or_geometry(trace, tmp_path, monkeypatch):
    from meshpipeline.agents.loop.diagnostics import sanitized
    run, (_ok, _v, outcome) = _run_for(trace, tmp_path, monkeypatch)
    payload = sanitized(run.finish(exit=outcome.exit))
    blob = json.dumps(payload)
    for leak in ("PLANNER_SYSTEM", "approach", "reasoning", "stl", "solid", "signed"):
        assert leak not in blob, f"the deterministic record leaked {leak}"


# model-call accounting
def test_real_planner_rounds_are_counted_and_tool_calls_are_not_fabricated(
        trace, tmp_path, monkeypatch):
    run, (_ok, _v, outcome) = _run_for(trace, tmp_path, monkeypatch)
    record = run.finish(exit=outcome.exit)
    assert record.tally.rounds == 1, "exactly the planning call that really happened"
    assert record.tally.tool_calls == 0, "the deterministic path executes no model tool calls"
    assert record.tally.provider_attempts == 1
    assert record.extension.plan_calls == 1
    assert record.rounds[0].input_tokens == 10 and record.rounds[0].output_tokens == 5
    assert record.rounds[0].finish_reason == "stop"


def test_a_path_with_no_provider_call_reports_zero_rounds(tmp_path):
    run = BuilderDriverRun(job_id="j", engine="snappy", mode="retry")
    record = run.finish(exit=LoopExit.terminal_action)
    assert record.tally.rounds == 0 and record.tally.tool_calls == 0
    assert record.extension.plan_calls == 0


def test_a_planner_failure_is_still_a_counted_real_round(trace, tmp_path, monkeypatch):
    run, (_ok, _v, outcome) = _run_for(trace, tmp_path, monkeypatch, plan_fails=True)
    record = run.finish(exit=outcome.exit)
    assert record.tally.rounds == 1, "the call happened - it is accounted, not hidden"
    assert record.extension.plan_calls == 1


# terminal convergence
def test_the_deterministic_success_value_is_the_canonical_terminal_contract(
        trace, tmp_path, monkeypatch):
    from meshpipeline.agents.builder.executor import BuilderToolResult
    _run, (ok, value, _o) = _run_for(trace, tmp_path, monkeypatch)
    canonical = BuilderToolResult(tool="submit_mesh", terminal=True,
                                  terminal_value=TERMINAL_SUCCESS)
    assert value == canonical.terminal_value == "submit_mesh:success"


def test_an_exhausted_build_never_returns_the_success_value(trace, tmp_path, monkeypatch):
    trace.mesh_good = False
    _run, (ok, value, outcome) = _run_for(trace, tmp_path, monkeypatch)
    assert ok is False
    assert value != TERMINAL_SUCCESS and outcome.terminal_value == ""


# marker / readiness integrity
def test_the_deterministic_driver_never_writes_the_canonical_readiness_marker(
        trace, tmp_path, monkeypatch):
    _run_for(trace, tmp_path, monkeypatch)
    assert not (tmp_path / ".mesh_ok").exists()


def test_a_stale_readiness_marker_cannot_manufacture_success(trace, tmp_path, monkeypatch):
    (tmp_path / ".mesh_ok").write_text("production-grade")
    trace.mesh_good = False
    _run, (ok, value, _o) = _run_for(trace, tmp_path, monkeypatch)
    assert ok is False and value != TERMINAL_SUCCESS


def test_the_plan_memory_write_is_atomic(tmp_path):
    drv._write_plan_memory(tmp_path, {"approach": "a"})
    assert json.loads((tmp_path / ".last_plan.json").read_text()) == {"approach": "a"}
    assert not list(tmp_path.glob(".last_plan.json.tmp")), "no temp file is left behind"


def test_every_plan_round_is_handed_the_publisher():
    # A plan is a real model round, and the driver is the only caller that can give it the public
    # lifecycle every other round gets. A call that omits `publish` still plans correctly and
    # traces nothing, so the round reaches the page as silence with no failure anywhere.
    import ast
    import inspect

    from meshpipeline.engines.snappy import drivers

    src = inspect.getsource(drivers)
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", getattr(n.func, "attr", "")) == "plan_with_accounting"]
    assert calls, "no plan_with_accounting call found; this guard is watching the wrong name"
    unpublished = [n.lineno for n in calls
                   if "publish" not in {kw.arg for kw in n.keywords}]
    assert unpublished == [], (
        f"plan_with_accounting is called without `publish` at line(s) {unpublished} - "
        "that round will stream its reasoning into nothing")


# submission identity: the pass fact
def test_each_native_dispatch_records_its_pass_before_the_workspace_is_read(
        trace, tmp_path, monkeypatch):
    # The submission authority derives the operation identity from the workspace alone, so the
    # pass fact must be ON DISK when the executor reads the workspace - otherwise pass 2's
    # revised case is a conflicting replay of pass 1's claim and dies before dispatch.
    import meshpipeline.engines.snappy.snappy_runner as _sr
    from meshpipeline.contracts.mesh_execution import NATIVE_PASS_FACT

    seen: list[str] = []

    def _snap(workspace, **_kw):
        seen.append((Path(workspace) / NATIVE_PASS_FACT).read_text())
        return {"rc": 0}

    monkeypatch.setattr(_sr, "run_snappy", _snap)
    monkeypatch.setattr(drv.scfg, "MAX_SNAPPY_ATTEMPTS", 2, raising=False)
    trace.mesh_good = False                     # every pass falls short, so the driver re-plans
    _run_for(trace, tmp_path, monkeypatch, plan_rounds=2)

    assert seen == ["1", "2"], (
        "each planned pass must record its own index for the submission identity; "
        f"the dispatches saw {seen}")
