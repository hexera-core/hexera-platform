# Responsibility: Verify every event the builder's execution publisher reaches is authorized by the real durable claim.
# Boundaries: the 17 sites the Snappy driver suite does not own; PostgreSQL, Redis, ownership and the gate are all real.
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import meshpipeline.settings.providers as provcfg
from meshpipeline.application import execution_fence as fence
from meshpipeline.persistence.models import SimulationJob

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

#: The four engines whose builder runs take the shared loop. Only snappy/spec.py sets a build
#: driver, so these four reach `_run_shared_loop` and the tracing/executor sites with it.
LOOP_ENGINES = ("cfmesh", "gmsh", "snappy_multiregion", "vmtk")

#: Every gated method on the execution publisher. Recorded by name so a site cannot be counted
#: under a spelling that does not exist.
GATED = ("anote", "awarn", "aerror", "astage", "aattempt", "acheck", "aaction", "asearch",
         "ascreenshot", "afile", "areasoning", "arationale", "atool_call", "atool_result",
         "ameshing", "ameshed", "averdict", "aclosing")

#: What the inner adapter must be asked to do when a gated call is authorized.
INNER = ("note", "warn", "error", "stage", "attempt", "check", "action", "search",
         "screenshot", "file", "reasoning", "rationale", "tool_call", "tool_result",
         "meshing", "meshed", "verdict", "closing")

#: The 17 sites this checkpoint certifies, by canonical id.
AGENT_SITES = ("node_builder::anote#budget", "node_builder::aattempt#1",
               "node_builder::anote#designing", "node_builder::anote#built")
LOOP_SITES = ("_on_round::anote#1",)
EXECUTOR_SITES = ("_publish_for::ameshed#1", "_publish_for::awarn#1",
                  "_publish_for::afile#1", "_publish_for::asearch#1")
TRACING_SITES = ("atool_call::atool_call#1", "atool_result::atool_result#1",
                 "around_begin::areasoning#1", "around_end::areasoning#1",
                 # the live-reasoning sink: partials published while the round is still thinking
                 "_publish::areasoning#1")
PLANNER_SITES = ("_plan_trace_begin::areasoning#1", "_plan_trace_end::areasoning#1")
RATIONALE_SITES = ("_asay::arationale#1",)

CERTIFIED = (AGENT_SITES + LOOP_SITES + EXECUTOR_SITES + TRACING_SITES
             + PLANNER_SITES + RATIONALE_SITES)


# real PostgreSQL


def _sessions():
    engine = create_async_engine(provcfg.POSTGRES_DSN, pool_size=1, max_overflow=1,
                                 pool_pre_ping=True)
    return engine, async_sessionmaker(bind=engine, expire_on_commit=False)


async def _with_session(fn):
    engine, Session = _sessions()
    try:
        async with Session() as db:
            return await fn(db)
    finally:
        await engine.dispose()


NO_PROGRESS_FAILURE = {"gate": "manifest_valid", "section": "MANIFEST",
                       "summary": "these patches have zero faces: ['inlet_1']"}


async def _drive_no_progress(monkeypatch, tmp_path):
    """A retry caused by the SAME failure its sibling attempt already recorded: node_builder
    publishes the halt explanation and routes to the failure sink WITHOUT buying a model
    round - the combining-wye bought four identical rejections before this stop existed."""
    from meshpipeline.agents.builder import no_progress as NP
    from meshpipeline.agents.builder.workspace import _setup_workspace

    def _seed_sibling(job_id):
        sig = NP.failure_signature({"classifier_result": NO_PROGRESS_FAILURE})
        # the claimed execution decides the generation; cover both the fresh and claimed values
        for generation in (0, 1):
            prev = _setup_workspace(job_id=str(job_id), attempt_num=1, engine="cfmesh",
                                    generation=generation)
            NP.record_failure(prev, sig)

    result = await _drive(monkeypatch, tmp_path, engine="cfmesh", mode="retry",
                          state_extra={"retry_count": 1,
                                       "classifier_result": dict(NO_PROGRESS_FAILURE)},
                          before_run=_seed_sibling)
    assert result["rounds"] == [], "the stop must fire before any model round is bought"
    return result


async def _seed(job_id: uuid.UUID, owner_id: str) -> None:
    async def _q(db):
        db.add(SimulationJob(id=job_id, owner_id=owner_id))
        await db.commit()
    await _with_session(_q)


async def _row(job_id: uuid.UUID):
    async def _q(db):
        return (await db.execute(
            select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
    return await _with_session(_q)


# A REAL takeover by a newer generation, written to PostgreSQL exactly as a competing worker
# would: the durable claim moves, and nothing in the process under test is told.
async def _take_over(job_id: uuid.UUID) -> None:
    async def _q(db):
        row = (await db.execute(
            select(SimulationJob).where(SimulationJob.id == job_id))).scalar_one()
        row.execution_generation = int(row.execution_generation or 0) + 1
        row.active_worker_token = uuid.uuid4()      # a genuinely different worker
        await db.commit()
    await _with_session(_q)


# observation - the real methods always run; this reads the context they ran in


def _rel(frame) -> str:
    root = Path(sys.modules["meshpipeline"].__file__).parent
    try:
        return Path(frame.f_code.co_filename).resolve().relative_to(root).as_posix()
    except ValueError:
        return frame.f_code.co_filename


#: This module observes by wrapping; its own frames are never the publisher of an event.
_SELF = str(Path(__file__).resolve())


def _production_frame():
    frame = sys._getframe(1)
    while frame is not None and str(Path(frame.f_code.co_filename).resolve()) == _SELF:
        frame = frame.f_back
    return frame


def _record_gated(monkeypatch, seen: list) -> None:
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher as P

    def wrap(name: str):
        original = getattr(P, name)

        async def w(self, *a, **k):
            frame = _production_frame()
            rec = {"module": _rel(frame), "fn": frame.f_code.co_name, "method": name,
                   "args": a, "kwargs": dict(k), "op_id": k.get("op_id", ""),
                   "publisher": id(self), "inner": id(self._inner),
                   "own": fence.current_ownership(),
                   # WHERE the publication was authorized. The mesh announcement was moved onto
                   # the loop precisely because the awaited ownership check cannot run in the
                   # worker thread the mesher uses, so the thread is part of that contract.
                   "thread": threading.get_ident(),
                   "order": len(seen), "raised": ""}
            seen.append(rec)
            try:
                return await original(self, *a, **k)
            except BaseException as exc:
                rec["raised"] = type(exc).__name__
                raise

        monkeypatch.setattr(P, name, w)

    for m in GATED:
        wrap(m)


def _record_inner(monkeypatch, delivered: list) -> None:
    from meshpipeline.adapters.event_stream.redis import JobPublisher

    def wrap(name: str):
        original = getattr(JobPublisher, name)

        def w(self, *a, **k):
            delivered.append({"method": name, "op_id": k.get("op_id", ""),
                              "job_id": str(getattr(self, "job_id", "")),
                              "publisher": id(self), "order": len(delivered)})
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, name, w)

    for m in INNER:
        wrap(m)


# deterministic doubles at the external boundaries only


#: What a streaming provider does to a round's reasoning before it answers. Both routes stream in
#: production, so a fake that returns a finished round and nothing else leaves the live-reasoning
#: publication unreachable and silently uncertified.
_REASONING_DELTAS = ("weighing the ", "weighing the far-field margin")


async def _stream_reasoning(on_reasoning) -> None:
    import inspect
    if on_reasoning is None:
        return
    for delta in _REASONING_DELTAS:
        emitted = on_reasoning(delta)
        if inspect.isawaitable(emitted):
            await emitted


def _install_builder_model(monkeypatch, rounds: list, script) -> None:
    from meshpipeline.contracts import model_inference as llm_router

    it = iter(script)

    async def call_builder_model(messages, tools=None, tool_choice="auto", job_id="",
                                 user_id="", parallel_tool_calls=None, on_reasoning=None):
        rounds.append({"job_id": job_id, "messages": len(messages)})
        await _stream_reasoning(on_reasoning)
        try:
            return next(it)
        except StopIteration:
            from meshpipeline.contracts.model_inference import ModelRoundResult
            return ModelRoundResult(assistant_text="", finish_reason="stop")

    monkeypatch.setattr(llm_router, "call_builder_model", call_builder_model)


def _install_planner_model(monkeypatch, rounds: list) -> None:
    from meshpipeline.contracts import model_inference as llm_router
    from meshpipeline.contracts.model_inference import ModelRoundResult

    plan = ('{"approach": "octree hex with prism layers", "max_cells": 2000000, '
            '"quality": "strict", "domain_margin": {"up": 2.0, "down": 4.0}}')

    async def call_planner_model(messages, tools=None, tool_choice="auto", job_id="",
                                 user_id="", parallel_tool_calls=None, on_reasoning=None):
        rounds.append({"job_id": job_id, "messages": len(messages)})
        await _stream_reasoning(on_reasoning)
        return ModelRoundResult(assistant_text=plan, finish_reason="stop",
                                input_tokens=800, output_tokens=64, reasoning_tokens=32,
                                reasoning_text="sized the far-field from the body length")

    monkeypatch.setattr(llm_router, "call_planner_model", call_planner_model)


#: The tool RESULTS the executor publishes from. The executor's own publication logic stays
#: real - this replaces only what the tools would have run (a mesher, a search backend, a disk
#: write), so no external service is contacted and no native command executes.
TOOL_RESULTS = {
    "write_file": {"written": "system/meshDict", "bytes": 2048, "operation": "created"},
    "web_search": {"results": 3},
    "run_mesh": {"success": False, "cells": 1_240_000, "mesh_ok": False},
    "submit_mesh": {"success": True},
}


def _install_tool_dispatch(monkeypatch, dispatched: list) -> None:
    import meshpipeline.agents.builder.executor as bx

    def dispatch(ctx, name, args):
        dispatched.append({"tool": name, "args": dict(args)})
        return json.dumps(TOOL_RESULTS.get(name, {"success": True}))

    monkeypatch.setattr(bx, "_dispatch_tool", dispatch)
    monkeypatch.setattr(bx, "_compress_tool_output", lambda fn, a, r, **k: r)


def _install_mesh_run_seam(monkeypatch, dispatched: list, mesh_runs: list,
                           mesh_result: dict | None = None) -> None:
    # `run_mesh` does NOT travel through `_dispatch_tool`. It is prepared off the event loop,
    # ANNOUNCED on the loop - where the ownership check the announcement needs can be awaited -
    # and only then executed off the loop again. Doubling both halves is what lets the real
    # announcement in between run: a double at the old seam skips it silently.
    import meshpipeline.agents.builder.executor as bx
    from meshpipeline.agents.builder.tools import _serialized
    from meshpipeline.agents.builder.tools.meshing import PreparedMeshRun

    def prepare(ctx):
        dispatched.append({"tool": "run_mesh", "args": {}})
        mesh_runs.append({"engine": ctx.engine, "prepared_own": fence.current_ownership(),
                          "prepared_thread": threading.get_ident(), "executed": False})
        return PreparedMeshRun(engine=ctx.engine, cap=1800, purpose="mesh",
                               history={"attempt": len(mesh_runs)})

    def dispatch_prepared(ctx, prepared):
        mesh_runs[-1].update(executed=True, executed_own=fence.current_ownership(),
                             executed_thread=threading.get_ident(),
                             engine_announced=prepared.engine, cap=prepared.cap)
        return _serialized("run_mesh", ctx.job_id, mesh_result or TOOL_RESULTS["run_mesh"])

    monkeypatch.setattr(bx, "prepare_mesh_run", prepare)
    monkeypatch.setattr(bx, "dispatch_prepared_mesh", dispatch_prepared)


def _install_native_double(monkeypatch, native: list) -> None:
    from meshpipeline.engines.snappy import snappy_runner as R

    def run_snappy(workspace, *, context=None, bashrc="", timeout=1800):
        ws = Path(workspace)
        native.append({"workspace": str(ws), "own": fence.current_ownership()})
        poly = ws / "constant" / "polyMesh"
        poly.mkdir(parents=True, exist_ok=True)
        (poly / "boundary").write_text(_BOUNDARY)
        return {"rc": 0, "timed_out": False, "layer_coverage": 0.95,
                "per_patch_layers": {"body": 0.95}}

    def check_mesh(workspace, *, bashrc="", region=""):
        return {"mesh_ok": True, "cells": 1_200_000, "faces": 3_600_000,
                "hexahedra": 1_150_000, "polyhedra": 0, "max_non_ortho": 42.0,
                "skew_faces": 0, "skew_fraction": 0.0, "fatal": []}

    monkeypatch.setattr(R, "run_snappy", run_snappy)
    monkeypatch.setattr(R, "check_mesh", check_mesh)


_BOUNDARY = """FoamFile{ version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
2
(
    body    { type wall;  nFaces 1200; startFace 0;    }
    farfield{ type patch; nFaces 800;  startFace 1200; }
)
"""


# the production route


def _graph(seed_state: dict):
    from langgraph.graph import END, START, StateGraph

    from meshpipeline.agents.builder.agent import node_builder
    from meshpipeline.contracts.pipeline_state import PipelineState

    g = StateGraph(PipelineState)
    g.add_node("seed", lambda s: dict(seed_state))
    g.add_node("node_builder", node_builder)
    g.add_edge(START, "seed")
    g.add_edge("seed", "node_builder")
    g.add_edge("node_builder", END)
    return g


def _geometry(tmp_path: Path, owner_id: str):
    from tests._geometry_support import interpretation_ref, source_ref

    from meshpipeline.contracts.geometry_source import MaterializedGeometry
    from meshpipeline.contracts.geometry_units import LengthUnit

    stl = tmp_path / "body.stl"
    stl.parent.mkdir(parents=True, exist_ok=True)
    _box(stl)
    ref = source_ref(local_file=stl, tmp_path=None, owner_id=owner_id, filename=stl.name)
    return MaterializedGeometry(
        ref=ref,
        interpretation=interpretation_ref(geometry_source_id=ref.source_id,
                                          unit=LengthUnit.metre),
        local_path=stl).to_state()


def _box(path: Path, half: float = 0.5) -> Path:
    c = [(-half, -half, -half), (half, -half, -half), (half, half, -half), (-half, half, -half),
         (-half, -half, half), (half, -half, half), (half, half, half), (-half, half, half)]
    faces = [(0, 1, 2), (0, 2, 3), (4, 6, 5), (4, 7, 6), (0, 4, 5), (0, 5, 1),
             (1, 5, 6), (1, 6, 2), (2, 6, 7), (2, 7, 3), (3, 7, 4), (3, 4, 0)]
    out = ["solid body"]
    for a, b, cc in faces:
        out.append("facet normal 0 0 0")
        out.append("  outer loop")
        out.extend(f"    vertex {c[i][0]:.6f} {c[i][1]:.6f} {c[i][2]:.6f}" for i in (a, b, cc))
        out.append("  endloop")
        out.append("endfacet")
    out.append("endsolid body")
    path.write_text("\n".join(out) + "\n")
    return path


def _state(tmp_path: Path, owner_id: str, engine: str, *, mode: str = "initial",
           deadline_epoch: float | None = None) -> dict:
    prev = tmp_path / "prev"
    prev.mkdir(parents=True, exist_ok=True)
    _box(prev / "input.stl")
    st = {
        "geometry": _geometry(tmp_path, owner_id),
        "engine": engine,
        "builder_mode": mode,
        "retry_count": 0,
        "openfoam_workspace": str(prev),
        "flow_topology": "external",
        "dimensionality": "3D",
        "intake_patches": [],
        "request_txt": "external aerodynamics over a blunt body",
    }
    if deadline_epoch is not None:
        st["builder_deadline_epoch"] = deadline_epoch
    return st


def _round(*calls, text="", finish=None):
    from meshpipeline.contracts.model_inference import (
        ModelRoundResult,
        ProviderAttemptInfo,
        ToolCallRequest,
    )
    return ModelRoundResult(
        tool_calls=tuple(ToolCallRequest(id=f"p-{n}", name=n, arguments=json.dumps(a))
                         for n, a in calls),
        assistant_text=text, reasoning_text="chose a coarser far-field", reasoning_tokens=17,
        finish_reason=finish or ("tool_calls" if calls else "stop"),
        provider=ProviderAttemptInfo(1, "p", "m"), input_tokens=500, output_tokens=40)


#: One shared-loop run that reaches every executor and tracing site: a round that speaks to the
#: user and writes a file, a search, a mesh that came back with defects, then a submission.
LOOP_SCRIPT = [
    _round(("write_file", {"path": "system/meshDict", "content": "x"}),
           text="Writing the mesh dictionary now."),
    _round(("web_search", {"query": "cfMesh boundary layer settings"})),
    _round(("run_mesh", {})),
    _round(("submit_mesh", {})),
]


async def _drive(monkeypatch, tmp_path, *, engine: str, mode: str = "initial",
                 deadline_epoch: float | None = None, script=None,
                 take_over_before: str = "", mesh_result: dict | None = None,
                 state_extra: dict | None = None, before_run=None):
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()

    job_id = uuid.uuid4()
    owner_id = f"cert-{job_id.hex[:8]}"
    await _seed(job_id, owner_id)
    if before_run is not None:
        before_run(job_id)

    seen: list = []
    delivered: list = []
    rounds: list = []
    dispatched: list = []
    native: list = []
    mesh_runs: list = []
    _record_gated(monkeypatch, seen)
    _record_inner(monkeypatch, delivered)
    _install_planner_model(monkeypatch, rounds)
    _install_builder_model(monkeypatch, rounds, script if script is not None else LOOP_SCRIPT)
    _install_tool_dispatch(monkeypatch, dispatched)
    _install_mesh_run_seam(monkeypatch, dispatched, mesh_runs, mesh_result)
    _install_native_double(monkeypatch, native)

    # A DETERMINISTIC BARRIER, not a sleep: the takeover is written to PostgreSQL the moment the
    # named site is about to publish, so the next authorization reads a claim that really moved.
    fired: list = []
    if take_over_before:
        _arm_takeover(monkeypatch, job_id, take_over_before, fired)

    import meshpipeline.pipeline.graph as gm
    root = tmp_path / f"run-{job_id.hex[:8]}"
    st = _state(root, owner_id, engine, mode=mode, deadline_epoch=deadline_epoch)
    if state_extra:
        st.update(state_extra)
    graph = _graph(st)
    monkeypatch.setattr(gm, "build_graph",
                        lambda checkpointer: graph.compile(checkpointer=checkpointer))

    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    raised = ""
    try:
        await asyncio.create_task(_run_async(JobRequest(job_id=str(job_id), owner_id=owner_id)))
    except BaseException as exc:                # the terminal outcome is another suite's contract
        raised = type(exc).__name__
    return {"job_id": job_id, "owner_id": owner_id, "seen": seen, "delivered": delivered,
            "rounds": rounds, "dispatched": dispatched, "native": native, "raised": raised,
            "fired": fired, "mesh_runs": mesh_runs}


# The barrier: the first time the named gated method is entered, move the durable claim, then let
# the real authorization run. No sleeping, no polling, no retry-until-green.
def _arm_takeover(monkeypatch, job_id, method: str, fired: list) -> None:
    from meshpipeline.application.execution_publisher import OwnershipCheckedPublisher as P

    original = getattr(P, method)

    async def w(self, *a, **k):
        if not fired:
            fired.append({"method": method})
            # The takeover must NOT be attempted from inside the publication path's own
            # try-block semantics: a failure here would be indistinguishable from a transport
            # blip and would silently disarm the barrier. It is recorded and re-raised loudly.
            try:
                await _take_over(job_id)
                fired[0]["took_over"] = True
            except BaseException as exc:
                fired[0]["took_over"] = False
                fired[0]["error"] = f"{type(exc).__name__}: {exc}"
        return await original(self, *a, **k)

    monkeypatch.setattr(P, method, w)


# canonical site identity


def _canonical(rec: dict) -> str:
    fn, method, op = rec["fn"], rec["method"], rec["op_id"]
    if fn == "node_builder":
        for prefix, name in (("budget-exhausted:", "anote#budget"),
                             ("designing:", "anote#designing"),
                             ("built:", "anote#built")):
            if op.startswith(prefix):
                return f"node_builder::{name}"
        return f"node_builder::{method}#1"
    return f"{fn}::{method}#1"


def _sites(res: dict) -> list[str]:
    return [_canonical(r) for r in res["seen"]]


def _counts(res: dict) -> dict:
    out: dict[str, int] = {}
    for s in _sites(res):
        out[s] = out.get(s, 0) + 1
    return out


# Redis, read on its own connection after the graph has returned


def _backlog(job_id) -> list[dict]:
    import redis as _redis

    from meshpipeline.events.channels import log_key_for

    r = _redis.from_url(provcfg.REDIS_URL)
    try:
        return [json.loads(x) for x in r.lrange(log_key_for(str(job_id)), 0, -1)]
    finally:
        r.close()


def _job_keys(job_id) -> list[str]:
    from meshpipeline.events.channels import log_key_for, opkey_set_for, seq_key_for
    return [seq_key_for(str(job_id)), log_key_for(str(job_id)), opkey_set_for(str(job_id))]


def _snapshot(job_id) -> dict:
    import hashlib

    import redis as _redis

    r = _redis.from_url(provcfg.REDIS_URL)
    try:
        snap = {}
        for key in _job_keys(job_id):
            raw = r.dump(key)
            snap[key] = {
                "exists": bool(r.exists(key)),
                "type": r.type(key).decode() if r.exists(key) else "none",
                "pttl": r.pttl(key),
                "sha256": hashlib.sha256(raw).hexdigest() if raw else "",
            }
        return snap
    finally:
        r.close()


def _delete_job_keys(job_id) -> list[str]:
    import redis as _redis

    r = _redis.from_url(provcfg.REDIS_URL)
    try:
        removed = [k for k in _job_keys(job_id) if r.delete(k)]
        return removed
    finally:
        r.close()


# fixtures - one run per engine, plus the budget and rejection scenarios


@pytest.fixture(scope="function")
async def loop_run(monkeypatch, tmp_path, request):
    engine = getattr(request, "param", "cfmesh")
    res = await _drive(monkeypatch, tmp_path, engine=engine)
    yield res
    _delete_job_keys(res["job_id"])


# `retry` is the mode a reviewer-driven rebuild arrives in: `drive` performs no planner round of
# its own, so the driver's own `plan_with_accounting` runs - and that is the call production hands
# the publisher, so it is where the planner's public trace is actually produced.
@pytest.fixture()
async def driver_run(monkeypatch, tmp_path):
    res = await _drive(monkeypatch, tmp_path, engine="snappy", mode="retry")
    yield res
    _delete_job_keys(res["job_id"])


@pytest.fixture()
async def budget_run(monkeypatch, tmp_path):
    res = await _drive(monkeypatch, tmp_path, engine="cfmesh",
                       deadline_epoch=time.time() - 5.0)
    yield res
    _delete_job_keys(res["job_id"])


# the durable claim behind every publication


async def _assert_owned(res: dict, label: str) -> None:
    row = await _row(res["job_id"])
    assert res["seen"], f"{label} published nothing at all"
    for rec in res["seen"]:
        own = rec["own"]
        site = _canonical(rec)
        assert own is not None, f"{label}/{site} published with no ownership bound"
        assert str(own.job_id) == str(res["job_id"]), \
            f"{label}/{site} published under another job's claim"
        assert own.execution_generation == row.execution_generation, \
            f"{label}/{site} ran on generation {own.execution_generation}, durable is " \
            f"{row.execution_generation}"
        assert own.worker_token == row.active_worker_token, \
            f"{label}/{site} did not match the durable worker token"
        assert rec["raised"] == "", f"{label}/{site} was refused: {rec['raised']}"


# 1. the shared-loop branch: four engines, twelve of the sixteen sites


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_a_shared_loop_engine_publishes_every_certified_site_under_its_own_claim(loop_run):
    res, counts = loop_run, _counts(loop_run)
    await _assert_owned(res, res["seen"][0]["module"] if res["seen"] else "shared loop")

    for site in AGENT_SITES[1:] + LOOP_SITES + EXECUTOR_SITES + TRACING_SITES + RATIONALE_SITES:
        assert counts.get(site, 0) >= 1, (
            f"{site} was never published on this shared-loop run\n  observed: {sorted(counts)}")


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_the_tool_executor_publishes_each_of_its_four_events_exactly_once(loop_run):
    counts = _counts(loop_run)
    for site in EXECUTOR_SITES:
        assert counts.get(site, 0) == 1, (
            f"{site} reached the gated publisher {counts.get(site, 0)} times, not once - these "
            "four were the ones an except-clause could have swallowed")
    tools = [d["tool"] for d in loop_run["dispatched"]]
    assert tools == ["write_file", "web_search", "run_mesh", "submit_mesh"], tools


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_the_mesh_run_announces_itself_on_the_loop_between_its_two_offloaded_halves(
        loop_run):
    # THE RELOCATED ANNOUNCEMENT. Preparing and executing the mesh are blocking, so both run in a
    # worker thread; the announcement between them needs an awaited PostgreSQL ownership check,
    # which a worker thread cannot do - so it must run on the loop, in the main thread.
    runs = loop_run["mesh_runs"]
    assert len(runs) == 1, f"run_mesh was prepared {len(runs)} times, not once"
    run = runs[0]
    assert run["executed"], "the mesh run was prepared but its execute half never ran"

    announced = [r for r in loop_run["seen"] if r["method"] == "ameshing"]
    assert len(announced) == 1, (
        f"the mesh run announced itself {len(announced)} times, not once - "
        f"observed {[ (r['fn'], r['method']) for r in loop_run['seen'] ]}")
    rec = announced[0]
    assert rec["fn"] == "_run_announced_mesh" and rec["module"] == "agents/builder/executor.py", (
        f"the announcement came from {rec['module']}::{rec['fn']}")
    assert not rec["raised"] and rec["own"] is not None, \
        "the announcement was not authorized by a bound claim"
    assert rec["args"][:2] == (run["engine"], run["cap"]), \
        "the announcement described a different run from the one that executed"

    main = threading.main_thread().ident
    assert rec["thread"] == main, \
        "the announcement was published off the loop - the awaited ownership check cannot run there"
    assert run["prepared_thread"] != main and run["executed_thread"] != main, (
        "the blocking halves ran on the loop; the mesher must stay offloaded "
        f"(prepare={run['prepared_thread']} execute={run['executed_thread']} main={main})")


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
def _closure_delivered(res: dict) -> list:
    # Only what THIS publisher object delegated. The pipeline's terminal authority publishes
    # through its own root when a run ends, and that is not in this closure.
    inner = {r["inner"] for r in res["seen"]}
    return [d for d in res["delivered"] if d["publisher"] in inner]


async def test_every_authorized_publication_delegates_to_the_inner_adapter_exactly_once(loop_run):
    authorized = [r for r in loop_run["seen"] if not r["raised"]]
    delivered = _closure_delivered(loop_run)
    assert len(delivered) == len(authorized), (
        f"{len(authorized)} authorized publications produced {len(delivered)} inner "
        "delegations - the gate either dropped or duplicated an event")
    assert [r["method"][1:] for r in authorized] == [d["method"] for d in delivered], (
        "the inner adapter was asked for a different sequence of events than was authorized")


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_the_whole_run_publishes_through_one_publisher_object(loop_run):
    objects = {r["publisher"] for r in loop_run["seen"]}
    assert len(objects) == 1, f"the run used {len(objects)} publisher objects, not one"


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_the_trace_sites_carry_the_call_and_round_identities(loop_run):
    seen = loop_run["seen"]
    calls = [r for r in seen if r["fn"] == "atool_call"]
    results = [r for r in seen if r["fn"] == "atool_result"]
    begins = [r for r in seen if r["fn"] == "around_begin"]
    ends = [r for r in seen if r["fn"] == "around_end"]
    assert len(calls) == len(results) == 4, (len(calls), len(results))
    assert len(begins) == len(ends) >= 4, (len(begins), len(ends))
    # every result names the call it closes, and every round-end names the round it opened
    assert [r["args"][1] for r in results] == [c["args"][0] for c in calls], \
        "a tool result was published against a different call id"
    assert [e["args"][0] for e in ends] == [b["args"][0] for b in begins], \
        "a round ended under a different reasoning id than it began"
    assert all(b["args"][1] == "started" for b in begins)


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_the_shared_loop_reaches_the_one_rationale_authority(loop_run):
    rationale = [r for r in loop_run["seen"] if r["method"] == "arationale"]
    assert len(rationale) == 1, f"the run narrated {len(rationale)} conclusions, not one"
    assert rationale[0]["fn"] == "_asay", \
        "the configuration conclusion did not go through the execution rationale authority"
    assert "configuration" in str(rationale[0]["args"][0]).lower(), rationale[0]["args"]


@pytest.mark.parametrize("loop_run", LOOP_ENGINES, indirect=True)
async def test_each_authorized_event_reached_the_replayable_backlog(loop_run):
    backlog = _backlog(loop_run["job_id"])
    authorized = [r for r in loop_run["seen"] if not r["raised"]]  # noqa: F841
    assert len(backlog) >= len(authorized), (
        f"{len(authorized)} events were authorized but the backlog holds {len(backlog)}")
    assert [e.get("seq") for e in backlog] == sorted(e.get("seq") for e in backlog), \
        "the backlog is not in sequence order"


# 2. the Snappy driver branch: the planner and mesh-ready sites


async def test_the_driver_branch_publishes_both_planner_reasoning_sites(driver_run):
    counts = _counts(driver_run)
    await _assert_owned(driver_run, "snappy driver")
    for site in PLANNER_SITES:
        assert counts.get(site, 0) >= 1, (
            f"{site} was never published on the driver branch\n  observed: {sorted(counts)}")
    begins = [r for r in driver_run["seen"] if r["fn"] == "_plan_trace_begin"]
    ends = [r for r in driver_run["seen"] if r["fn"] == "_plan_trace_end"]
    assert len(begins) == len(ends) >= 1
    assert [e["args"][0] for e in ends] == [b["args"][0] for b in begins], \
        "a plan round ended under a different reasoning id than it began"
    # the attempt identity the planner publishes under is the run's, not a default
    assert all(":builder:" in str(b["args"][0]) for b in begins), \
        [b["args"][0] for b in begins]


async def test_the_driver_branch_reaches_the_same_rationale_authority(driver_run):
    rationale = [r for r in driver_run["seen"] if r["method"] == "arationale"]
    assert rationale, "the driver branch never narrated its mesh-ready conclusion"
    assert {r["fn"] for r in rationale} == {"_asay"}, \
        "the mesh-ready conclusion bypassed the execution rationale authority"
    assert any("mesh was generated" in str(r["args"][0]).lower() for r in rationale), \
        [r["args"][0] for r in rationale]


async def test_the_two_rationale_entries_share_one_semantic_site(driver_run, loop_run):
    # abuilder_mesh_ready and abuilder_configuration are two production entries to ONE
    # publication authority; they must not become two ledger sites.
    driver = {_canonical(r) for r in driver_run["seen"] if r["method"] == "arationale"}
    loop = {_canonical(r) for r in loop_run["seen"] if r["method"] == "arationale"}
    assert driver == loop == {"_asay::arationale#1"}, (driver, loop)


# 3. the builder agent's own four sites


async def test_the_node_publishes_its_attempt_and_its_two_progress_notes(loop_run):
    counts = _counts(loop_run)
    for site in ("node_builder::aattempt#1", "node_builder::anote#designing",
                 "node_builder::anote#built"):
        assert counts.get(site, 0) == 1, (
            f"{site} was published {counts.get(site, 0)} times, not once")
    attempt = next(r for r in loop_run["seen"] if r["method"] == "aattempt")
    assert attempt["args"][1] > 0, "the attempt denominator is not the true ceiling"


async def test_an_exhausted_overall_budget_publishes_its_note_and_starts_no_attempt(budget_run):
    counts = _counts(budget_run)
    await _assert_owned(budget_run, "budget exhausted")
    assert counts.get("node_builder::anote#budget", 0) == 1, (
        f"the overall-budget note was not published\n  observed: {sorted(counts)}")
    assert counts.get("node_builder::aattempt#1", 0) == 0, \
        "an attempt was announced after the budget was already spent"
    assert budget_run["rounds"] == [], "a provider round ran on an exhausted budget"

# 4. rejection


def _factory():
    engine, Session = _sessions()
    return Session


# The REAL claim, taken through the production lease repository - the same call the pipeline
# makes. Nothing about the claim, the generation or the token is fabricated.
async def _claim(job_id: uuid.UUID):
    from meshpipeline.persistence.lease import LeaseRepository

    repo = LeaseRepository()
    Session = _factory()
    async with Session() as db:
        _claim_result, ownership = await repo.claim_execution(
            db, job_id=job_id, worker_token=uuid.uuid4(), backend="local",
            backend_execution_id=f"cert-{uuid.uuid4().hex[:8]}")
        await db.commit()
    return ownership


async def _refused(monkeypatch, tmp_path, *, case: str):
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()

    job_id = uuid.uuid4()
    owner_id = f"cert-{job_id.hex[:8]}"
    await _seed(job_id, owner_id)

    seen: list = []
    delivered: list = []
    rounds: list = []
    dispatched: list = []
    native: list = []
    _record_gated(monkeypatch, seen)
    _record_inner(monkeypatch, delivered)
    _install_planner_model(monkeypatch, rounds)
    _install_builder_model(monkeypatch, rounds, LOOP_SCRIPT)
    _install_tool_dispatch(monkeypatch, dispatched)
    _install_native_double(monkeypatch, native)

    before = _snapshot(job_id)

    state = _state(tmp_path / f"run-{job_id.hex[:8]}", owner_id, "cfmesh")
    state["job_id"] = str(job_id)

    from meshpipeline.agents.builder.agent import node_builder

    ctx = None
    other_id = None
    if case == "another job":
        other_id = uuid.uuid4()
        await _seed(other_id, f"other-{other_id.hex[:8]}")
        own = await _claim(other_id)
        ctx = fence.execution_ownership(own, session_factory=_factory())
        ctx.__enter__()
    try:
        raised = ""
        try:
            await node_builder(state)
        except BaseException as exc:
            raised = type(exc).__name__
    finally:
        if ctx is not None:
            ctx.__exit__(None, None, None)

    after = _snapshot(job_id)
    return {"job_id": job_id, "other_id": other_id, "seen": seen, "delivered": delivered,
            "rounds": rounds, "dispatched": dispatched, "native": native, "raised": raised,
            "before": before, "after": after}


@pytest.fixture(params=["unbound", "another job"])
async def early(monkeypatch, tmp_path, request):
    res = await _refused(monkeypatch, tmp_path, case=request.param)
    res["case"] = request.param
    yield res
    _delete_job_keys(res["job_id"])
    if res["other_id"]:
        _delete_job_keys(res["other_id"])


async def test_an_unauthorized_builder_is_refused_before_any_downstream_work(early):
    assert early["raised"] == "StaleExecutionPublish", (
        f"{early['case']}: the node ended with {early['raised'] or 'no exception'}; a refusal "
        "must reach the caller, never be translated into a retry or a silent loss")
    assert early["rounds"] == [], f"{early['case']}: a provider round ran anyway"
    assert early["dispatched"] == [], f"{early['case']}: a tool ran anyway"
    assert early["native"] == [], f"{early['case']}: the native mesher ran anyway"


async def test_an_unauthorized_publication_never_reaches_the_inner_adapter(early):
    assert early["delivered"] == [], (
        f"{early['case']}: {len(early['delivered'])} events reached JobPublisher - they would "
        "have been written to Redis")
    refused = [r for r in early["seen"] if r["raised"]]
    assert refused, f"{early['case']}: nothing was refused, so nothing was gated"
    assert all(r["raised"] == "StaleExecutionPublish" for r in refused), \
        [r["raised"] for r in refused]


async def test_an_unauthorized_run_mutates_no_redis_key(early):
    assert early["before"] == early["after"], (
        f"{early['case']}: Redis changed under a refused run\n"
        f"  before: {early['before']}\n  after:  {early['after']}")
    for key, state in early["after"].items():
        assert state["exists"] is False, f"{early['case']}: {key} was created by a refused run"
        assert state["pttl"] == -2, f"{early['case']}: {key} has a TTL"
        assert state["sha256"] == "", f"{early['case']}: {key} holds content"


# late: a real takeover between authorization boundaries


@pytest.fixture(params=["ameshed", "atool_call"])
async def late(monkeypatch, tmp_path, request):
    res = await _drive(monkeypatch, tmp_path, engine="cfmesh",
                       take_over_before=request.param)
    res["case"] = request.param
    yield res
    _delete_job_keys(res["job_id"])


@pytest.fixture()
async def late_driver(monkeypatch, tmp_path):
    res = await _drive(monkeypatch, tmp_path, engine="snappy", mode="retry",
                       take_over_before="ameshing")
    res["case"] = "snappy ameshing"
    yield res
    _delete_job_keys(res["job_id"])


def _assert_stale_after_takeover(res: dict) -> None:
    assert res["fired"], f"{res['case']}: the barrier never fired - the site was never reached"
    assert res["fired"][0].get("took_over"), (
        f"{res['case']}: the takeover itself failed, so nothing was actually stale: "
        f"{res['fired'][0].get('error')}")
    refused = [r for r in res["seen"] if r["raised"]]
    assert refused, (
        f"{res['case']}: the takeover was written to PostgreSQL and nothing was refused - the "
        "stale worker went on publishing")
    assert all(r["raised"] == "StaleExecutionPublish" for r in refused), \
        [r["raised"] for r in refused]
    first = min(r["order"] for r in refused)
    # nothing the stale generation attempted after losing the claim was delivered
    late_authorized = [r for r in res["seen"] if r["order"] > first and not r["raised"]]
    assert late_authorized == [], (
        f"{res['case']}: {len(late_authorized)} events were published after the claim moved: "
        f"{[_canonical(r) for r in late_authorized]}")
    assert len(_closure_delivered(res)) == len([r for r in res["seen"] if not r["raised"]]), \
        f"{res['case']}: inner delegations do not match authorized publications"


async def test_a_stale_shared_loop_worker_cannot_publish_after_takeover(late):
    _assert_stale_after_takeover(late)


async def test_a_stale_driver_worker_cannot_publish_after_takeover(late_driver):
    _assert_stale_after_takeover(late_driver)


@pytest.fixture()
async def late_mesh_run(monkeypatch, tmp_path):
    # The claim moves the instant the mesh announcement is entered - which is the announcement
    # the relocation put in front of the mesher.
    res = await _drive(monkeypatch, tmp_path, engine="cfmesh", take_over_before="ameshing")
    res["case"] = "builder ameshing"
    yield res
    _delete_job_keys(res["job_id"])


async def test_a_stale_worker_announces_no_mesh_run_and_never_starts_one(late_mesh_run):
    # THE POINT OF THE RELOCATION. The announcement is the ownership boundary the mesh run sits
    # behind, so a worker that lost its claim there must not go on to submit a 25-minute job.
    _assert_stale_after_takeover(late_mesh_run)
    runs = late_mesh_run["mesh_runs"]
    assert runs and runs[0]["prepared_own"] is not None, "the mesh run was never prepared"
    assert not runs[0]["executed"], (
        "a superseded worker started the mesher after its announcement was refused")
    refused = [r for r in late_mesh_run["seen"] if r["raised"]]
    assert refused[0]["method"] == "ameshing", (
        f"the first refusal was {refused[0]['method']}, not the mesh announcement")
    assert "submit_mesh" not in [d["tool"] for d in late_mesh_run["dispatched"]]


async def test_a_late_refusal_stops_the_run_rather_than_being_replanned(late):
    # How the PIPELINE classifies the ended run is another suite's contract. What this one owns
    # is that the builder stopped: after the claim moved, the stale generation neither published
    # nor took another authoritative action.
    refused_at = min(r["order"] for r in late["seen"] if r["raised"])
    after = [_canonical(r) for r in late["seen"] if r["order"] > refused_at and not r["raised"]]
    assert after == [], f"the stale generation published {after} after losing its claim"
    tools = [d["tool"] for d in late["dispatched"]]
    assert "submit_mesh" not in tools, \
        f"a stale generation submitted a mesh after the claim had moved: {tools}"


# the loop policy's own close-out


#: Two valid meshes and no submission. The loop ends with the deliverable present and nothing
#: submitted, which is the ONLY state that opens the policy's auto-submit close-out.
AUTO_SUBMIT_SCRIPT = [
    _round(("write_file", {"path": "system/meshDict", "content": "x"})),
    _round(("run_mesh", {})),
    _round(("run_mesh", {})),
    _round(text="I think that is enough."),
]

#: A mesh the engine judged sound - `mesh_ok` is what the policy counts, and the committed
#: `run_mesh` result deliberately carries defects instead.
VALID_MESH = {"success": True, "cells": 1_240_000, "mesh_ok": True}


@pytest.fixture()
async def close_out_run(monkeypatch, tmp_path):
    res = await _drive(monkeypatch, tmp_path, engine="cfmesh", script=AUTO_SUBMIT_SCRIPT,
                       mesh_result=VALID_MESH)
    yield res
    _delete_job_keys(res["job_id"])


async def test_the_policy_announces_its_own_auto_submission_under_the_claim(close_out_run):
    # The application-controlled close-out: the loop ended without a submission, the policy made
    # one itself, and it says so through the executor's typed publication property.
    notes = [r for r in close_out_run["seen"] if r["fn"] == "close_out"]
    assert [r["method"] for r in notes] == ["anote"], (
        f"the close-out published {[(r['fn'], r['method']) for r in notes]}")
    rec = notes[0]
    assert rec["op_id"] == "auto-submit", rec["op_id"]
    assert rec["module"] == "agents/builder/loop_policy.py", rec["module"]
    assert not rec["raised"] and rec["own"] is not None, "the close-out published unauthorized"
    assert "Auto-submitting" in rec["args"][0]
    # it really submitted: the fenced dispatch ran before the announcement
    tools = [d["tool"] for d in close_out_run["dispatched"]]
    assert tools == ["write_file", "run_mesh", "run_mesh", "submit_mesh"], tools


async def test_a_submitted_run_never_reaches_the_close_out_announcement(loop_run):
    # The shared-loop script submits, so the policy has nothing to close out. At most one
    # submission, ever - the site must not fire a second time.
    assert [r for r in loop_run["seen"] if r["fn"] == "close_out"] == []


@pytest.fixture()
async def late_close_out(monkeypatch, tmp_path):
    res = await _drive(monkeypatch, tmp_path, engine="cfmesh", script=AUTO_SUBMIT_SCRIPT,
                       mesh_result=VALID_MESH, take_over_before="anote")
    res["case"] = "loop policy close-out"
    yield res
    _delete_job_keys(res["job_id"])


async def test_a_stale_worker_cannot_announce_an_auto_submission(late_close_out):
    # The claim moves at the first gated note of the run; by the close-out the worker is
    # superseded, and its announcement is refused like every other publication.
    _assert_stale_after_takeover(late_close_out)
    closing = [r for r in late_close_out["seen"] if r["fn"] == "close_out"]
    if closing:
        assert all(r["raised"] == "StaleExecutionPublish" for r in closing), \
            f"a superseded worker announced its auto-submission: {closing}"


# 5. the certified inventory


async def test_every_certified_site_is_behaviourally_reached(loop_run, driver_run, budget_run):
    reached: set[str] = set()
    for res in (loop_run, driver_run, budget_run):
        reached |= set(_sites(res))
    missing = [s for s in CERTIFIED if s not in reached]
    assert missing == [], f"these certified sites were never behaviourally reached: {missing}"
    # This literal EARNS its churn, unlike the inventory counts elsewhere: deleting an entry from
    # CERTIFIED leaves `missing` empty, so the check above would keep passing over quietly reduced
    # coverage. Comparing against the publication manifest would say it structurally, but this tier
    # runs inside the worker image, which does not ship the manifest. Raise it when you add a site.
    assert len(CERTIFIED) == 17, len(CERTIFIED)


async def test_the_leading_a_does_not_create_a_second_semantic_identity(loop_run):
    # every gated method maps onto exactly one inner event name
    pairs = {(r["method"], r["method"][1:]) for r in loop_run["seen"] if not r["raised"]}
    assert all(inner in INNER for _gated, inner in pairs), sorted(pairs)
    assert len({g for g, _ in pairs}) == len({i for _, i in pairs}), \
        "a gated method mapped onto more than one inner event"
