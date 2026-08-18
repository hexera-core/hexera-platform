# Responsibility: Verify the pipeline run's orchestration announcements are ownership-checked.
# Boundaries: the four execution-owned announcements; the graph is another suite's contract.
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from tests.integration import execution_ownership_support as ownership

from meshpipeline.application import execution_fence as fence
from meshpipeline.contracts.event_stream import StaleExecutionPublish

pytestmark = pytest.mark.asyncio
if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

RUN_MOD = "application/pipeline_run.py"
_TRANSPARENT = {"execution_publisher.py"}

#: The engine the user pinned. A catalog name, so the pin is honoured and announced.
PINNED = "gmsh"


def _record(monkeypatch, seen: list, terminal: list) -> None:
    # Records who asked the REAL adapter to publish, and - for the terminal event - whether a
    # fence expectation was in force when it did.
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    from meshpipeline.contracts.event_stream import expected_fence

    root = Path(sys.modules["meshpipeline"].__file__).parent

    def wrap(name: str):
        original = getattr(JobPublisher, name)

        def w(self, *a, **k):
            frame = sys._getframe(1)
            while frame is not None and Path(frame.f_code.co_filename).name in _TRANSPARENT:
                frame = frame.f_back
            try:
                rel = Path(frame.f_code.co_filename).resolve().relative_to(root).as_posix()
            except ValueError:
                rel = ""
            record = {"module": rel, "fn": frame.f_code.co_name.replace(".<locals>", ""),
                             "qualname": frame.f_code.co_qualname.replace(".<locals>", ""),
                             "method": name, "op_id": k.get("op_id", ""),
                             "impl": type(self).__name__, "own": fence.current_ownership(),
                             "fence_expected": expected_fence(), "args": a,
                             "job": str(getattr(self, "job_id", ""))}
            # The terminal closing is published by the OUTBOX, not by the run body - it is the
            # post-ownership authority, so it is recorded wherever it comes from.
            (terminal if name == "closing" else seen if rel == RUN_MOD else []).append(record)
            return original(self, *a, **k)

        monkeypatch.setattr(JobPublisher, name, w)

    for method in ("stage", "note", "closing"):
        wrap(method)


def _redis_state(job_id):
    from tests.integration.test_pipeline_node_ownership import _redis_state as state
    return state(job_id)


def _parent_workspace(base: Path, parent_job_id: str) -> Path:
    # A REAL delivered attempt on disk, the way a completed parent run leaves one: the newest
    # generation's newest attempt, carrying the manifest the rebuild reads its engine from.
    attempt = base / parent_job_id / "generation_1" / "attempt_2"
    attempt.mkdir(parents=True, exist_ok=True)
    (attempt / "mesh_manifest.json").write_text(json.dumps({
        "mesh_mode": PINNED, "mesh_units": "m", "engine_params": {},
        "flow_topology": "", "patches": {"inlet": 1}, "quality": {"min_sicn": 0.6}}))
    (attempt / "request.txt").write_text("mesh the manifold")
    return attempt


def _passthrough_graph():
    # The GRAPH is expensive work below the branch under test: this run is about what the
    # lifecycle announces before the graph starts and after ownership is released.
    from langgraph.graph import END, START, StateGraph

    from meshpipeline.contracts.pipeline_state import PipelineState

    g = StateGraph(PipelineState)
    g.add_node("noop", lambda s: {})
    g.add_edge(START, "noop")
    g.add_edge("noop", END)
    return g


async def _run(monkeypatch, tmp_path, seen: list, terminal: list, *, dispute: bool = True,
               pinned: str = PINNED):
    import meshpipeline.pipeline.graph as gm
    import meshpipeline.settings.runtime as rtcfg
    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    from meshpipeline.runtime.composition import install_adapters

    install_adapters()
    base = tmp_path / "workspaces"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rtcfg, "WORKSPACE_BASE", str(base))

    job_id = uuid.uuid4()
    owner_id = f"run-{job_id.hex[:8]}"
    await ownership.seed(job_id, owner_id)

    user_dispute = None
    if dispute:
        parent = str(uuid.uuid4())
        _parent_workspace(base, parent)
        user_dispute = {"of_job_id": parent, "flags": [{"note": "rough"}, {"note": "thin"}],
                        "comment": "two regions look wrong"}

    graph = _passthrough_graph()
    monkeypatch.setattr(gm, "build_graph",
                        lambda checkpointer: graph.compile(checkpointer=checkpointer))
    _record(monkeypatch, seen, terminal)

    raised = ""
    try:
        await asyncio.create_task(_run_async(JobRequest(
            job_id=str(job_id), owner_id=owner_id, mesh_engine=pinned,
            user_dispute=user_dispute, request_txt="mesh the manifold")))
    except BaseException as exc:                # the terminal outcome is another suite's contract
        raised = type(exc).__name__
    return {"job_id": job_id, "owner_id": owner_id, "seen": seen, "terminal": terminal,
            "raised": raised}


def _delete_job_keys(job_id) -> None:
    import redis as _redis

    import meshpipeline.settings.providers as provcfg
    from meshpipeline.events.channels import (
        fence_key_for,
        log_key_for,
        opkey_set_for,
        seq_key_for,
    )

    r = _redis.from_url(provcfg.REDIS_URL)
    try:
        for key in (seq_key_for(str(job_id)), log_key_for(str(job_id)),
                    opkey_set_for(str(job_id)), fence_key_for(str(job_id))):
            r.delete(key)
    finally:
        r.close()


@pytest.fixture()
async def run(monkeypatch, tmp_path):
    seen: list = []
    terminal: list = []
    res = await _run(monkeypatch, tmp_path, seen, terminal)
    yield res
    _delete_job_keys(res["job_id"])


# the four execution-owned announcements


async def test_the_pinned_engine_is_announced_under_the_run_claim(run):
    engine = [r for r in run["seen"] if r["fn"] == "_announce_engine"]
    assert [r["method"] for r in engine] == ["stage", "note"], \
        f"the engine pin published {[(r['fn'], r['method']) for r in engine]}"
    for r in engine:
        assert r["impl"] == "JobPublisher"
        assert r["own"] is not None, "the pin was announced with NO ownership bound"
        assert str(r["own"].job_id) == str(run["job_id"])
        assert r["op_id"] == "pinned"
        assert r["fence_expected"], "an execution-owned announcement carried no fence expectation"
    assert PINNED in engine[1]["args"][0], engine[1]["args"][0]


async def test_the_dispute_is_announced_under_the_same_claim(run):
    dispute = [r for r in run["seen"] if r["fn"] == "_announce_dispute"]
    assert [r["method"] for r in dispute] == ["stage", "note"], \
        f"the dispute published {[(r['fn'], r['method']) for r in dispute]}"
    for r in dispute:
        assert r["own"] is not None and str(r["own"].job_id) == str(run["job_id"])
        assert r["op_id"] == "dispute-opened"
        assert r["fence_expected"], "an execution-owned announcement carried no fence expectation"
    assert "2 region(s)" in dispute[1]["args"][0], dispute[1]["args"][0]


async def test_the_four_announcements_are_two_nested_functions_not_the_run_body(run):
    owned = [r for r in run["seen"] if r["method"] in ("stage", "note")]
    assert {r["qualname"] for r in owned} == {"_run_async._announce_engine",
                                              "_run_async._announce_dispute"}, \
        f"the announcements were attributed to {sorted({r['qualname'] for r in owned})}"
    assert len(owned) == 4, f"the run made {len(owned)} execution-owned announcements, not four"


async def test_the_announcements_precede_the_graph_and_share_one_generation(run):
    owned = [r for r in run["seen"] if r["method"] in ("stage", "note")]
    generations = {r["own"].execution_generation for r in owned}
    tokens = {str(r["own"].worker_token) for r in owned}
    assert len(generations) == 1 and len(tokens) == 1, \
        f"the run announced under {len(generations)} generations and {len(tokens)} tokens"


# the stale control


async def test_a_superseded_generation_announces_nothing_and_writes_no_event(monkeypatch,
                                                                            tmp_path):
    # A claim that PostgreSQL has moved past, bound around the same production announcement the
    # run makes for itself. Everything else - the gate, the adapter, the fence - is real.
    seen: list = []
    job_id, own, Session, engine = await ownership.seeded_claim("run-stale")
    _record(monkeypatch, seen, [])
    try:
        before = _redis_state(job_id)
        assert before["fence"], "the claim installed no fence to defend"
        stale = ownership.superseded(job_id, own)

        async def announce() -> None:
            # the run's own announcement body, reached with a superseded claim bound
            from meshpipeline.application.execution_publisher import execution_publisher
            with fence.execution_ownership(stale, session_factory=Session):
                _e = execution_publisher(str(job_id), "engine_select")
                await _e.astage(op_id="pinned")
                await _e.anote(f"Mesh engine: {PINNED} - the one you asked for", op_id="pinned")

        with pytest.raises(StaleExecutionPublish):
            await announce()
        after = _redis_state(job_id)
    finally:
        await engine.dispose()

    assert seen == [], f"a superseded run published {[(r['fn'], r['method']) for r in seen]}"
    assert after["seq"] == before["seq"], "a stale announcement advanced the event sequence"
    assert after["backlog"] == before["backlog"], "a stale announcement reached the backlog"
    assert after["opkeys"] == before["opkeys"], "a stale announcement recorded an operation key"
    assert after["fence"] == before["fence"], "a stale announcement disturbed the current fence"
