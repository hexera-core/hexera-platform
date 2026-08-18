# Responsibility: Verify each checkpoint disposition materialises geometry at most once and runs only what remains.
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from tests._geometry_support import persisted_geometry

from meshpipeline.contracts.geometry_units import LengthUnit, ResolutionBasis

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT"):
    pytest.skip("real PostgreSQL and MinIO endpoints are required", allow_module_level=True)

pytest.importorskip("langgraph.checkpoint.postgres.aio")

_OWNER = "tenant-counts"

_CHILD = r"""
import hashlib, json, os, sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

job_id, workspace, kwargs_json = sys.argv[1:4]
os.environ["WORKSPACE_BASE"] = workspace
TALLY = Path(os.environ["TALLY"])
FORBID = os.environ.get("FORBID_SOURCE") == "1"
DIE_AT = os.environ.get("DIE_AT", "")

from meshpipeline.runtime.composition import install_adapters
install_adapters()

import meshpipeline.application.geometry_materializer as gm
import meshpipeline.contracts.object_storage as osmod
import meshpipeline.pipeline.graph as graph_module
from meshpipeline.pipeline.geometry_state import geometry_path, geometry_ref

T = {"build_graph": 0, "compiled": 0, "aget_state": 0, "aupdate_state": 0, "ainvoke": 0,
     "nodes": [], "materializer": 0, "exists": 0, "download_file": 0, "object_checksum": 0,
     "create_download_url": 0, "paths": [], "source_ids": [], "fenced_update": 0}


def _flush():
    TALLY.write_text(json.dumps(T))


class _CountingStore:
    # Wraps the REAL store installed by composition. Under FORBID every SOURCE read raises, so a
    # zero-access disposition fails at the access rather than in a later assertion. Non-source
    # keys (artifacts) are left alone - this is a source-access sentinel, not a global one.
    def __init__(self, inner): self._inner = inner

    def _guard(self, op, key):
        if FORBID and str(key).startswith("sources/"):
            raise AssertionError("source ObjectStore.%s was called for %s" % (op, key))

    def exists(self, *, object_key):
        self._guard("exists", object_key); T["exists"] += 1; _flush()
        return self._inner.exists(object_key=object_key)

    def download_file(self, *, object_key, destination):
        self._guard("download_file", object_key); T["download_file"] += 1; _flush()
        return self._inner.download_file(object_key=object_key, destination=destination)

    def object_checksum(self, *, object_key):
        self._guard("object_checksum", object_key); T["object_checksum"] += 1; _flush()
        return self._inner.object_checksum(object_key=object_key)

    def create_download_url(self, *, object_key, expires_in):
        self._guard("create_download_url", object_key); T["create_download_url"] += 1; _flush()
        return self._inner.create_download_url(object_key=object_key, expires_in=expires_in)

    def __getattr__(self, n): return getattr(self._inner, n)


osmod.set_object_store(_CountingStore(osmod.get_object_store()))

# Patch the symbol pipeline_run actually resolves: it imports this name from the module at call
# time, so rebinding the module attribute is what the run will see.
_real_prepare = gm.prepare_execution_geometry


async def _counted_prepare(ref, interpretation=None, *, job_id):
    if FORBID:
        raise AssertionError("the geometry materializer was called")
    T["materializer"] += 1; _flush()
    return await _real_prepare(ref, interpretation, job_id=job_id)


gm.prepare_execution_geometry = _counted_prepare


class _S(TypedDict, total=False):
    geometry: dict
    hops: int
    execution_generation: int
    reviewer_verdict: str
    outcome_message: str


async def _wait_durable(completed: int):
    # An INDEPENDENT connection must see a checkpoint that records the COMPLETED nodes before the
    # crash, or the kill destroys work the resume is then expected to skip.
    #
    # `step >= 0` was too weak and made this test fail about one run in three, in isolation - not
    # from ordering, which is what it looked like. LangGraph writes step -1 for the input and step
    # 0 for the superstep in which the first node is still PENDING; the node's completion lands at
    # step 1. Waiting for step >= 0 therefore returned while `first` was still unfinished, the kill
    # destroyed it, and the resume correctly replayed a node the assertion expected to be done.
    #
    # `completed` is how many nodes have actually finished, so the wait is for the checkpoint that
    # records them: at `second`'s entry that is step >= 1. Must await - LangGraph's write is a
    # coroutine on this same loop.
    import asyncio as _a
    import psycopg
    import meshpipeline.settings.providers as _pc
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as _V
    dsn = _pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    loop = _a.get_running_loop(); deadline = loop.time() + 60
    while loop.time() < deadline:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute("select count(*) from checkpoints where thread_id like %s "
                        "and (metadata->>'step')::int >= %s",
                        ("%s:s%%" % job_id, completed))
            if (cur.fetchone() or [0])[0] > 0:
                return
        await _a.sleep(0.02)
    raise AssertionError("no durable checkpoint appeared before the crash window closed")


def _node(name, last=False):
    async def _f(state):
        if DIE_AT == name:
            await _wait_durable(len(T["nodes"])); _flush(); os._exit(9)
        p = geometry_path(state)
        ref = geometry_ref(state)
        T["nodes"].append(name)
        T["paths"].append(p)
        T["source_ids"].append(ref.source_id if ref else None)
        for _ in range(3):          # repeated reads inside one entry must not refetch
            geometry_path(state)
        _flush()
        out = {"hops": int(state.get("hops", 0)) + 1}
        if last:
            out.update(reviewer_verdict="PASS", outcome_message="done")
        return out
    return _f


def _build(checkpointer=None):
    T["build_graph"] += 1
    # the fenced wrapper is what production hands us; record that the update goes through it
    T["fenced_checkpointer"] = type(checkpointer).__name__ if checkpointer is not None else None
    g = StateGraph(_S)
    g.add_node("first", _node("first"))
    g.add_node("second", _node("second", last=True))
    g.add_edge(START, "first")
    g.add_edge("first", "second")
    g.add_edge("second", END)
    compiled = g.compile(checkpointer=checkpointer)
    T["compiled"] += 1
    _ainvoke, _aget, _aupdate = compiled.ainvoke, compiled.aget_state, compiled.aupdate_state

    async def ai(*a, **k):
        T["ainvoke"] += 1; _flush(); return await _ainvoke(*a, **k)

    async def ag(*a, **k):
        T["aget_state"] += 1; _flush(); return await _aget(*a, **k)

    async def au(*a, **k):
        T["aupdate_state"] += 1
        if type(checkpointer).__name__ == "FencedCheckpointer":
            T["fenced_update"] += 1
        _flush(); return await _aupdate(*a, **k)

    compiled.ainvoke, compiled.aget_state, compiled.aupdate_state = ai, ag, au
    _flush()
    return compiled


graph_module.build_graph = _build
_flush()

from meshpipeline.application.pipeline_run import run_pipeline

T["result"] = run_pipeline(**json.loads(kwargs_json))
_flush()
print("DONE")
"""


async def _seed(marker: str, tmp_path, *, size: int = 4096,
                unit: LengthUnit = LengthUnit.millimetre,
                basis: ResolutionBasis = ResolutionBasis.file_declared):
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.contracts import object_storage
    from meshpipeline.persistence.models import SimulationJob
    from meshpipeline.persistence.session import dispose_engine, get_db

    object_storage.set_object_store(build_object_store())
    jid = uuid.uuid4()
    async with get_db() as db:
        geom = await persisted_geometry(db, tmp_path=tmp_path / f"seed-{marker}", owner_id=_OWNER,
                                        filename="duct.step", marker=marker, size=size,
                                        unit=unit, basis=basis)
        db.add(SimulationJob(id=jid, owner_id=_OWNER,
                             geometry_source_id=uuid.UUID(geom.source_id)))
        await db.commit()
    await dispose_engine()
    return str(jid), geom


def _run(job_id, geom, workspace: Path, tally: Path, *, execution_id, die_at="",
         forbid_source=False, extra_env=None):
    workspace.mkdir(parents=True, exist_ok=True)
    kwargs = {"job_id": job_id, "owner_id": _OWNER, "geometry_source": geom.ref.to_payload(),
              "geometry_interpretation": geom.interpretation_snapshot.to_payload(),
              "request_txt": "mesh it", "mesh_engine": "cfmesh"}
    env = {**os.environ, "PIPELINE_EXECUTION_ID": execution_id, "TALLY": str(tally),
           "DIE_AT": die_at, "FORBID_SOURCE": "1" if forbid_source else "0",
           "WORKSPACE_BASE": str(workspace), **(extra_env or {})}
    proc = subprocess.run([sys.executable, "-c", _CHILD, job_id, str(workspace),
                           json.dumps(kwargs)],
                          cwd=workspace, capture_output=True, text=True, timeout=300, env=env)
    return (json.loads(tally.read_text()) if tally.exists() else {}), proc


async def _expire_lease(job_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                              "interval '1 second' where id = :j"), {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()


async def _job(job_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        row = (await db.execute(text("select status::text as status, execution_generation "
                                     "from simulation_jobs where id = :j"),
                                {"j": uuid.UUID(job_id)})).mappings().first()
    await dispose_engine()
    return dict(row)


def _no_source_access(t: dict):
    for k in ("materializer", "exists", "download_file", "object_checksum", "create_download_url"):
        assert t.get(k, 0) == 0, f"{k} was called: {t}"


def _no_execution(t: dict):
    assert t.get("ainvoke", 0) == 0, f"the graph was invoked: {t}"
    assert t.get("nodes", []) == [], f"nodes executed: {t.get('nodes')}"


# materialising rows

async def test_absent_checkpoint_materialises_once_and_starts_from_start(tmp_path):
    job_id, geom = await _seed("absent", tmp_path)
    t, proc = _run(job_id, geom, tmp_path / "ws", tmp_path / "t.json",
                   execution_id=f"e-{uuid.uuid4()}")
    assert proc.returncode == 0, proc.stderr[-1500:]

    assert t["materializer"] == 1, t
    assert t["exists"] == 1 and t["download_file"] == 1, t       # one reconstruction sequence
    assert t["object_checksum"] == 0 and t["create_download_url"] == 0, t
    assert t["aget_state"] == 1, "classification should read the thread exactly once"
    assert t["aupdate_state"] == 0, "a fresh run has no handle to replace"
    assert t["ainvoke"] == 1 and t["nodes"] == ["first", "second"], t
    # repeated geometry_path reads inside the entry did not refetch
    assert t["download_file"] == 1
    # every node saw a file under THIS workspace, and one unchanged identity
    for p in t["paths"]:
        assert Path(p).is_relative_to(tmp_path / "ws"), p
    assert set(t["source_ids"]) == {geom.ref.source_id}


async def test_pending_checkpoint_materialises_once_and_runs_only_pending_nodes(tmp_path):
    job_id, geom = await _seed("pending", tmp_path)
    exec_id = f"e-{uuid.uuid4()}"
    a, proc_a = _run(job_id, geom, tmp_path / "ws-a", tmp_path / "a.json",
                     execution_id=exec_id, die_at="second")
    assert proc_a.returncode in (9, -9), proc_a.returncode
    assert a["nodes"] == ["first"], a
    await _expire_lease(job_id)

    b, proc_b = _run(job_id, geom, tmp_path / "ws-b", tmp_path / "b.json", execution_id=exec_id)
    assert proc_b.returncode == 0, proc_b.stderr[-1500:]

    assert b["materializer"] == 1, b
    assert b["exists"] == 1 and b["download_file"] == 1, b       # one reconstruction sequence
    assert b["aget_state"] == 1, b
    assert b["aupdate_state"] == 1, "the geometry handle was not replaced through graph state"
    assert b["fenced_update"] == 1, "the replacement did not pass through FencedCheckpointer"
    assert b["ainvoke"] == 1, b
    assert b["nodes"] == ["second"], f"a completed node replayed: {b['nodes']}"
    assert Path(b["paths"][0]).is_relative_to(tmp_path / "ws-b")
    assert b["source_ids"] == [geom.ref.source_id]      # identity unchanged by the new local path


# zero-access rows

async def test_a_terminal_job_touches_no_source_and_runs_no_graph(tmp_path):
    job_id, geom = await _seed("terminal", tmp_path)
    _run(job_id, geom, tmp_path / "ws1", tmp_path / "t1.json", execution_id=f"e-{uuid.uuid4()}")
    assert (await _job(job_id))["status"] in ("succeeded", "failed")

    t, _ = _run(job_id, geom, tmp_path / "ws2", tmp_path / "t2.json",
                execution_id=f"e-{uuid.uuid4()}", forbid_source=True)
    _no_source_access(t)
    _no_execution(t)
    assert t.get("aget_state", 0) == 0, "a terminal job should not even classify"


async def test_a_live_lease_conflict_touches_no_source_and_runs_no_graph(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    job_id, geom = await _seed("conflict", tmp_path)
    _run(job_id, geom, tmp_path / "ws1", tmp_path / "t1.json", execution_id=f"e-{uuid.uuid4()}",
         die_at="second")
    async with get_db() as db:
        await db.execute(text("update simulation_jobs set status='running', "
                              "lease_expires_at = now() + interval '900 seconds' where id = :j"),
                         {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()

    t, _ = _run(job_id, geom, tmp_path / "ws2", tmp_path / "t2.json",
                execution_id=f"e-{uuid.uuid4()}", forbid_source=True)
    _no_source_access(t)
    _no_execution(t)
    assert t.get("aupdate_state", 0) == 0, "a refused executor mutated checkpoint state"


async def test_a_complete_checkpoint_runs_no_nodes_and_terminalises(tmp_path):
    job_id, geom = await _seed("complete", tmp_path)
    exec_id = f"e-{uuid.uuid4()}"
    a, _ = _run(job_id, geom, tmp_path / "ws-a", tmp_path / "a.json", execution_id=exec_id)
    assert a["nodes"] == ["first", "second"], a

    # reopen as running with the COMPLETE checkpoint intact: death after the graph, before
    # terminal authority committed
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        await db.execute(text("update simulation_jobs set status='running', "
                              "lease_expires_at = now() - interval '1 second' where id = :j"),
                         {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()

    b, proc = _run(job_id, geom, tmp_path / "ws-b", tmp_path / "b.json",
                   execution_id=exec_id, forbid_source=True)
    assert proc.returncode == 0, proc.stderr[-1500:]
    _no_source_access(b)
    assert b["aget_state"] == 1, "classification should still read the thread"
    assert b["nodes"] == [], f"a completed graph replayed nodes: {b['nodes']}"
    assert (await _job(job_id))["status"] in ("succeeded", "failed"), \
        "the complete checkpoint did not terminalize the job"


async def test_a_corrupt_checkpoint_touches_no_source_and_runs_no_graph(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    job_id, geom = await _seed("corrupt", tmp_path)
    exec_id = f"e-{uuid.uuid4()}"
    _run(job_id, geom, tmp_path / "ws-a", tmp_path / "a.json", execution_id=exec_id,
         die_at="second")
    await _expire_lease(job_id)

    # the tables exist and hold a legitimate checkpoint by now; corrupt only this thread
    marker = f"unknown-serializer-{uuid.uuid4()}"
    async with get_db() as db:
        touched = (await db.execute(text(
            "update checkpoint_blobs set type = :m where thread_id like :p returning 1"),
            {"m": marker, "p": f"{job_id}%"})).rowcount
        escaped = (await db.execute(text(
            "select count(*) from checkpoint_blobs where thread_id not like :p and type = :m"),
            {"p": f"{job_id}%", "m": marker})).scalar()
        await db.commit()
    await dispose_engine()
    assert touched > 0 and escaped == 0, (touched, escaped)

    t, _ = _run(job_id, geom, tmp_path / "ws-b", tmp_path / "b.json", execution_id=exec_id,
                forbid_source=True)
    _no_source_access(t)
    _no_execution(t)


async def test_an_unavailable_checkpoint_store_touches_no_source_and_runs_no_graph(tmp_path):
    job_id, geom = await _seed("unavailable", tmp_path)
    t, _ = _run(job_id, geom, tmp_path / "ws", tmp_path / "t.json",
                execution_id=f"e-{uuid.uuid4()}", forbid_source=True,
                extra_env={"REQUIRE_DURABLE_CHECKPOINTER": "true",
                           "DATABASE_URL":
                               "postgresql+asyncpg://cm:cm@127.0.0.1:1/cm?sslmode=disable"})
    _no_source_access(t)
    _no_execution(t)


async def test_a_new_generation_reads_only_its_own_thread(tmp_path):
    job_id, geom = await _seed("generation", tmp_path)
    a, _ = _run(job_id, geom, tmp_path / "g1", tmp_path / "a.json",
                execution_id=f"e-{uuid.uuid4()}", die_at="second")
    assert a["nodes"] == ["first"], a
    gen1 = (await _job(job_id))["execution_generation"]
    await _expire_lease(job_id)

    b, _ = _run(job_id, geom, tmp_path / "g2", tmp_path / "b.json",
                execution_id=f"e-{uuid.uuid4()}")
    assert (await _job(job_id))["execution_generation"] == gen1 + 1
    # a new generation is its own absent thread: it materialises once and starts from START
    assert b["materializer"] == 1 and b["exists"] == 1 and b["download_file"] == 1, b
    assert b["nodes"] == ["first", "second"], f"a new generation inherited position: {b['nodes']}"
    assert b["aupdate_state"] == 0, "a fresh generation replaced a handle it never had"


# recovery without the source

async def test_a_complete_checkpoint_terminalises_after_its_source_is_deleted(tmp_path):
    import shutil

    from sqlalchemy import text

    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("survives-deletion", tmp_path, size=5120)

    # the source really is there to begin with
    store = build_object_store()
    assert store.exists(object_key=geom.ref.object_key)

    exec_id = f"e-{uuid.uuid4()}"
    ws_a = tmp_path / "ws-a"
    a, proc_a = _run(job_id, geom, ws_a, tmp_path / "a.json", execution_id=exec_id)
    assert proc_a.returncode == 0, proc_a.stderr[-1500:]
    assert a["nodes"] == ["first", "second"], a          # the graph really completed
    saved_verdict = a["result"].get("verdict")

    # death after the graph, before terminal authority committed
    async with get_db() as db:
        await db.execute(text("update simulation_jobs set status='running', final_result=null, "
                              "lease_expires_at = now() - interval '1 second' where id = :j"),
                         {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()
    assert (await _job(job_id))["status"] == "running", "the job should be non-terminal again"

    # the former workspace and the original bytes are both gone
    shutil.rmtree(ws_a)
    assert not ws_a.exists()
    store.delete_object(object_key=geom.ref.object_key)
    assert not store.exists(object_key=geom.ref.object_key)

    b, proc_b = _run(job_id, geom, tmp_path / "ws-b", tmp_path / "b.json",
                     execution_id=exec_id, forbid_source=True)
    assert proc_b.returncode == 0, proc_b.stderr[-2000:]

    # classification happened, and nothing reached for the deleted source
    assert b["aget_state"] == 1, "the thread was not classified"
    _no_source_access(b)
    assert b["nodes"] == [], f"a completed graph replayed nodes: {b['nodes']}"

    # terminal authority ran and recorded the SAVED computation
    row = await _job(job_id)
    assert row["status"] in ("succeeded", "failed"), row
    assert b["result"].get("verdict") == saved_verdict, \
        f"the recovered verdict differs from the saved one ({b['result']} vs {saved_verdict})"

    async with get_db() as db:
        stored = (await db.execute(text("select final_result from simulation_jobs where id = :j"),
                                   {"j": uuid.UUID(job_id)})).scalar()
    await dispose_engine()
    assert stored, "no durable terminal record was written on recovery"
