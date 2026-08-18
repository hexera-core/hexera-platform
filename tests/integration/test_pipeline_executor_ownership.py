# Responsibility: Verify every pipeline-executor publication runs under the claimed execution ownership.
# Boundaries: the executor node's emissions - the graph boundary and artifact delivery are other suites.
from __future__ import annotations

import asyncio
import json
import os
import sys
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

EXEC_MOD = "pipeline/executor.py"
SEEN: list = []
THREAD: dict = {}

class _Absent:
    created_at=None; next=(); values={}

BUILT: list = []
POST: dict = {}


# Count real publisher constructions on the executor path; delegate to the real factory. The
# executor builds an OWNERSHIP-CHECKED publisher - that factory is the seam, and what it returns
# is passed through untouched, so every publication below still runs the real authorization.
def _count_constructions(monkeypatch):
    import meshpipeline.pipeline.executor as ex
    real = ex.execution_publisher

    def counting(job_id, agent=None):
        BUILT.append(agent)
        return real(job_id, agent)                # the production OwnershipCheckedPublisher

    monkeypatch.setattr(ex, "execution_publisher", counting)


# Read ownership INSIDE _run_async at the first production seam past the graph span.
def _watch_post_graph(monkeypatch):
    from meshpipeline.application import terminal_finalize as tf
    from meshpipeline.persistence.lease import LeaseRepository
    real_owner = LeaseRepository.is_current_owner
    real_crash = tf.finalize_crash

    async def owner(self, db, own):
        # is_current_owner serves BOTH the in-graph fence and the pre-finalize check. The last
        # call in a run is the post-graph one, which is the seam this contract is about.
        POST.setdefault("owner_calls", []).append(fence.current_ownership())
        return await real_owner(self, db, own)

    async def crash(*a, **k):
        POST.setdefault("crash_seam", fence.current_ownership())
        return await real_crash(*a, **k)

    monkeypatch.setattr(LeaseRepository, "is_current_owner", owner)
    monkeypatch.setattr(tf, "finalize_crash", crash)


#: How an event TRAVELS, never where it was decided. Every executor publication now reaches the
#: adapter through the ownership gate, so the immediate caller is always the gated wrapper.
_TRANSPARENT = {"execution_publisher.py"}


def _rec(monkeypatch):
    from meshpipeline.adapters.event_stream.redis import JobPublisher
    root = Path(sys.modules["meshpipeline"].__file__).parent
    def wrap(name):
        orig = getattr(JobPublisher, name)
        def w(self,*a,**k):
            f=sys._getframe(1)
            while f is not None and Path(f.f_code.co_filename).name in _TRANSPARENT:
                f=f.f_back
            try: rel=Path(f.f_code.co_filename).resolve().relative_to(root).as_posix()
            except ValueError: rel=""
            if rel==EXEC_MOD:
                own=fence.current_ownership()
                SEEN.append({"line":f.f_lineno,"method":name,"op_id":k.get("op_id",""),
                             "impl":type(self).__name__,"job":str(getattr(self,"job_id","")),
                             "own":own,"fn":f.f_code.co_name})
            return orig(self,*a,**k)
        monkeypatch.setattr(JobPublisher,name,w)
    for m in ("stage","note","check"): wrap(m)

def _S():
    e=create_async_engine(provcfg.POSTGRES_DSN,pool_size=1,max_overflow=1)
    return e, async_sessionmaker(bind=e,expire_on_commit=False)

async def _row(j):
    e,S=_S()
    try:
        async with S() as db: return (await db.execute(select(SimulationJob).where(SimulationJob.id==j))).scalar_one()
    finally: await e.dispose()

async def _seed(j,o):
    e,S=_S()
    try:
        async with S() as db: db.add(SimulationJob(id=j,owner_id=o)); await db.commit()
    finally: await e.dispose()

class _Eng:
    def __init__(self, fin_ok, extents, solv): self._f,self._e,self._s=fin_ok,extents,solv
    def finalize(self,*a,**k):
        THREAD["in_thread"]=fence.current_ownership()
        if self._f == "boom":
            raise RuntimeError("controlled finalize failure")
        return {"success":self._f,"output":"finalize"}
    def check_domain_extents(self,*a,**k): return self._e
    def check_solvability(self,*a,**k): return self._s

def _graph(seed_state):
    from langgraph.graph import END, START, StateGraph

    from meshpipeline.contracts.pipeline_state import PipelineState
    from meshpipeline.pipeline.executor import node_executor
    g=StateGraph(PipelineState)
    g.add_node("seed", lambda s: dict(seed_state))
    g.add_node("node_executor", node_executor)
    g.add_edge(START,"seed"); g.add_edge("seed","node_executor"); g.add_edge("node_executor",END)
    return g

async def _run(monkeypatch, ws, *, reject="", fin_ok=True, gates=(True,"",""), gate_results=(("mesh_ok",True),),
               extents=None, solv=None, retry=0):
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()
    j=uuid.uuid4(); o=f"exec-{j.hex[:8]}"; await _seed(j,o)
    SEEN.clear(); THREAD.clear(); BUILT.clear(); POST.clear()
    _rec(monkeypatch); _count_constructions(monkeypatch); _watch_post_graph(monkeypatch)
    import meshpipeline.pipeline.executor as ex
    monkeypatch.setattr(ex,"get_engine", lambda e: _Eng(fin_ok,extents,solv))
    import meshpipeline.engines.gates as gt
    def fake_run_gates(gl, ctx, on_result=None):
        for k,ok in gate_results:
            if on_result: on_result(k,ok,"fb")
        return gates
    monkeypatch.setattr(gt,"run_gates",fake_run_gates)
    monkeypatch.setattr("meshpipeline.pipeline.executor.run_gates", fake_run_gates, raising=False)
    import meshpipeline.pipeline.graph as gm
    # The node builds its publisher FOR THE JOB IN STATE, and the gate compares that against the
    # bound claim. An empty id was harmless when the executor published unchecked; now it means
    # publishing for a different job than the one this worker owns, which is refused.
    built=_graph({"job_id":str(j),"geometry_unsuitable_reason":reject,"openfoam_workspace":str(ws),
                  "engine":"gmsh","retry_count":retry,"flow_topology":"internal","request_txt":"far field 10m"})
    monkeypatch.setattr(gm,"build_graph", lambda checkpointer: built.compile(checkpointer=checkpointer))
    from meshpipeline.application.pipeline_run import JobRequest, _run_async
    t=asyncio.create_task(_run_async(JobRequest(job_id=str(j),owner_id=o)))
    try: await t
    except Exception: pass
    return j

def _ids(): return sorted({(r["fn"],r["method"],r["line"]) for r in SEEN})

async def _assert_bound(j):
    row=await _row(j)
    for r in SEEN:
        assert r["own"] is not None, f"L{r['line']} .{r['method']} UNBOUND"
        assert r["impl"]=="JobPublisher"
        assert str(r["own"].job_id)==str(j)
        assert r["own"].execution_generation==row.execution_generation
        assert r["own"].claim_epoch==row.execution_claim_epoch
        assert (r["own"].worker_token==row.active_worker_token) is True


def _ws(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "mesh_manifest.json").write_text(json.dumps({"mesh_units": "m", "cell_count": 10}))
    return ws


#: The executor's complete publication surface: owning function, publisher method, and how many
#: DISTINCT source calls each pair has. Line numbers appear nowhere in this contract.
#:
#: `_report_gate` is a RECORDER now, not a publisher: `run_gates` wraps its callback in
#: `except Exception`, which would swallow a lost claim, so the gate results are published from
#: `node_executor` itself where a refusal can propagate. That moved one `check` up, not away.
CANONICAL = {
    ("node_executor", "stage"): 1,
    ("node_executor", "note"): 4,
    ("node_executor", "check"): 6,
}
SCENARIOS = [
    ("early_reject",  {"reject": "bad geometry"}),
    ("gate_fail",     {"gates": (False, "patchx", "fb")}),
    ("all_pass",      {"solv": (True, "ok")}),
    ("extent_reject", {"extents": (False, "too small")}),
    ("solv_fail",     {"solv": (False, "no converge"), "retry": 2}),
]


async def test_every_executor_publication_runs_under_the_claimed_ownership(monkeypatch, tmp_path):
    ws = _ws(tmp_path)
    union = set()
    for name, kw in SCENARIOS:
        job_id = await _run(monkeypatch, ws, **kw)
        assert SEEN, f"{name} reached no executor publication"
        await _assert_bound(job_id)
        union |= set(_ids())

    counts = {}
    for fn, method, _line in union:
        counts[(fn, method)] = counts.get((fn, method), 0) + 1
    assert counts == CANONICAL, (
        f"the executor publication surface changed: expected {CANONICAL}, observed {counts}")
    assert len(union) == 11, f"expected 11 distinct executor calls, observed {len(union)}"


#: Operation identity per (owning function, method) and retry count, as PRODUCTION emitted it.
#: Recorded from the publisher call, never recomputed from the production expression.
async def test_operation_identities_follow_the_retry_count(monkeypatch, tmp_path):
    ws = _ws(tmp_path)
    await _run(monkeypatch, ws, solv=(True, "ok"), retry=0)
    zero = {(r["fn"], r["method"], r["line"]): r["op_id"] for r in SEEN}
    await _run(monkeypatch, ws, solv=(True, "ok"), retry=3)
    three = {(r["fn"], r["method"], r["line"]): r["op_id"] for r in SEEN}

    assert zero and three and set(zero) == set(three)
    retry_derived = {k: (zero[k], three[k]) for k in zero if zero[k] != three[k]}
    assert retry_derived, (
        "no executor operation identity varied with retry_count - the retry-derived op_id "
        "collapsed to a fixed literal")
    for k, (z, t) in retry_derived.items():
        assert z.endswith(":0") and t.endswith(":3"), \
            f"{k} produced {z!r}/{t!r}; the identity no longer carries the retry count"
    for k, v in zero.items():
        assert v == "" or v.endswith(":0"), f"{k} emitted an unexpected identity {v!r}"


async def test_the_executor_builds_its_publisher_when_it_legitimately_owns_the_run(monkeypatch, tmp_path):
    await _run(monkeypatch, _ws(tmp_path), solv=(True, "ok"))
    assert BUILT.count("executor") >= 1, \
        "the executor never constructed its production publisher on a legitimately owned run"


async def test_the_finalize_worker_thread_inherits_the_claimed_ownership(monkeypatch, tmp_path):
    job_id = await _run(monkeypatch, _ws(tmp_path), solv=(True, "ok"))
    own = THREAD.get("in_thread")
    assert own is not None, "the finalize worker thread ran with NO execution ownership"
    row = await _row(job_id)
    assert str(own.job_id) == str(job_id)
    assert own.execution_generation == row.execution_generation
    assert own.claim_epoch == row.execution_claim_epoch
    assert (own.worker_token == row.active_worker_token) is True


async def test_ownership_is_cleared_inside_the_run_after_the_graph_returns(monkeypatch, tmp_path):
    await _run(monkeypatch, _ws(tmp_path), solv=(True, "ok"))
    calls = POST.get("owner_calls", [])
    assert calls, "the post-graph production seam was never reached"
    assert calls[-1] is None, (
        "execution ownership was still bound at the first production seam after the graph span - "
        "read inside _run_async, not from the test's own context")


async def test_ownership_is_cleared_inside_the_run_after_a_graph_exception(monkeypatch, tmp_path):
    # A crashing finalize routes through the existing crash path; its seam must see no ownership.
    await _run(monkeypatch, _ws(tmp_path), fin_ok="boom")
    seam = POST.get("crash_seam", (POST.get("owner_calls") or ["absent"])[-1])
    assert seam is None, f"ownership survived a graph exception into the terminal path ({seam!r})"


async def test_a_stale_owner_is_refused_before_the_executor_publishes(monkeypatch, tmp_path):
    # assert_current_owner rejects a bound identity that no longer owns the job. It runs before the
    # publisher is constructed, so a superseded worker reaches no executor publication.
    from meshpipeline.application.execution_fence import StaleWorkerFenced
    from meshpipeline.persistence.lease import ExecutionOwnership
    from meshpipeline.pipeline.executor import node_executor

    job_id = uuid.uuid4()
    await _seed(job_id, f"stale-{job_id.hex[:8]}")
    SEEN.clear(); BUILT.clear()
    _rec(monkeypatch); _count_constructions(monkeypatch)
    superseded = ExecutionOwnership(job_id=job_id, execution_generation=99,
                                    worker_token=uuid.uuid4(), backend="test",
                                    pipeline_deadline_at=None, claim_epoch=99)
    e, S = _S()
    try:
        with fence.execution_ownership(superseded, session_factory=S):
            # the production fence raises StaleWorkerFenced for a superseded bound identity
            with pytest.raises(StaleWorkerFenced):
                await node_executor({"job_id": str(job_id), "openfoam_workspace": str(tmp_path),
                                     "engine": "gmsh", "retry_count": 0})
    finally:
        await e.dispose()
    assert SEEN == [], "a superseded worker published an executor event"
    assert BUILT == [], (
        "a superseded worker constructed its publisher before the fence refused it - the fence "
        "must precede publisher construction, not merely precede publication")
