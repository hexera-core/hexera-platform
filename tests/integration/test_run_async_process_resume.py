# Responsibility: Verify a restart resumes its own generation, a live lease refuses a second executor, takeover bumps.
from __future__ import annotations

import json
import os
import shutil
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

_OWNER = "tenant-resume"

# The child. It patches ONLY the graph factory, then calls the real public entry.
_CHILD = r"""
import hashlib, json, os, sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

job_id, workspace, source_payload, mode = sys.argv[1:5]
os.environ["WORKSPACE_BASE"] = workspace

# Runtime composition, exactly as the one-shot worker entrypoint performs it. This binds the
# product contracts (ObjectStore, launcher, executor) - it is production wiring, not a stub.
from meshpipeline.runtime.composition import install_adapters
install_adapters()

import meshpipeline.pipeline.graph as graph_module
from meshpipeline.pipeline.geometry_state import geometry_path, geometry_ref

_DIE = os.environ.get("SUSPEND_AT_GATE") == "1"


class _S(TypedDict, total=False):
    # Declared channels, so `geometry` survives from the entry's initial state through every node
    # and into the checkpoint - an untyped dict schema drops keys a node does not return.
    geometry: dict
    hops: int
    reviewer_verdict: str
    outcome_message: str


def _record(state, stage):
    p = geometry_path(state)
    seen = {"stage": stage, "path": p,
            "sha": hashlib.sha256(Path(p).read_bytes()).hexdigest(),
            "source_id": geometry_ref(state).source_id,
            "hops": int(state.get("hops", 0)) + 1}
    (Path(workspace) / f"observed-{stage}.json").write_text(json.dumps(seen))
    return seen


async def _stage1(state):
    return {"hops": _record(state, "stage1")["hops"]}


async def _gate(state):
    if _DIE:
        # An abrupt container death AFTER stage1's checkpoint is DURABLE. Waiting for the row to
        # appear is what makes this deterministic: killing the process the instant stage1 returns
        # races the checkpointer's write, and a resume cannot be proven against a checkpoint that
        # was never committed.
        import asyncio as _a

        import psycopg
        import meshpipeline.settings.providers as _pc
        from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as _V
        thread = f"{job_id}:s{_V}:g{int(state.get('execution_generation', 0) or 0)}"
        dsn = _pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
        for _ in range(200):
            with psycopg.connect(dsn) as c, c.cursor() as cur:
                cur.execute("select count(*) from checkpoints where thread_id = %s", (thread,))
                if (cur.fetchone() or [0])[0] > 0:
                    os._exit(9)
            await _a.sleep(0.05)
        os._exit(9)
    return {}


async def _stage2(state):
    return {"hops": _record(state, "stage2")["hops"],
            "reviewer_verdict": "PASS", "outcome_message": "done"}


def _fake_graph(checkpointer=None):
    # The REAL checkpointer (AsyncPostgresSaver, fenced) is passed straight through; only the
    # graph body is ours, so the checkpoint written here is a genuine production checkpoint.
    g = StateGraph(_S)
    g.add_node("stage1", _stage1)
    g.add_node("gate", _gate)
    g.add_node("stage2", _stage2)
    g.add_edge(START, "stage1")
    g.add_edge("stage1", "gate")
    g.add_edge("gate", "stage2")
    g.add_edge("stage2", END)
    return g.compile(checkpointer=checkpointer)


graph_module.build_graph = _fake_graph

from meshpipeline.application.pipeline_run import run_pipeline

result = run_pipeline(**json.loads(source_payload))
out = {"pid": os.getpid(), "cwd": os.getcwd(), "result": result}
print("RESULT " + json.dumps(out))
"""


async def _seed(marker: str, tmp_path, *, size: int = 8000,
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


def _run_kwargs(job_id, geom):
    return {"job_id": job_id, "owner_id": _OWNER, "geometry_source": geom.ref.to_payload(),
              "geometry_interpretation": geom.interpretation_snapshot.to_payload(),
            "request_txt": "mesh it", "mesh_engine": "cfmesh"}


def _child(job_id, geom, workspace: Path, *, execution_id: str, lease_seconds="900",
           suspend=False, extra_env=None):
    workspace.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "PIPELINE_EXECUTION_ID": execution_id,      # what makes a restart the SAME execution
           "WORKER_LEASE_SECONDS": lease_seconds,
           # the runtime refuses a heartbeat >= the lease, so a short test lease needs a
           # proportionally short heartbeat - the production invariant is respected, not bypassed
           "WORKER_HEARTBEAT_SECONDS": str(max(1, int(lease_seconds) // 2)),
           "WORKSPACE_BASE": str(workspace),
           "SUSPEND_AT_GATE": "1" if suspend else "0",
           **(extra_env or {})}
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, job_id, str(workspace),
         json.dumps(_run_kwargs(job_id, geom)), "run"],
        cwd=workspace, capture_output=True, text=True, timeout=300, env=env)
    stages = {f.stem.split("-", 1)[1]: json.loads(f.read_text())
              for f in sorted(workspace.glob("observed-*.json"))}
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    if suspend:
        # killed mid-graph on purpose; the evidence is whichever stage files exist
        return ({"returncode": proc.returncode, "suspended": True, "stages": stages}, proc)
    if not line:
        pytest.fail(f"child produced no result\nSTDOUT:{proc.stdout[-2000:]}\n"
                    f"STDERR:{proc.stderr[-3000:]}")
    out = json.loads(line[-1][len("RESULT "):])
    out["stages"] = stages
    return out, proc


async def _job_row(job_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        r = await db.execute(text(
            "select status::text as status, execution_generation, pipeline_execution_id, "
            "       lease_expires_at, active_worker_token "
            "from simulation_jobs where id = :j"), {"j": uuid.UUID(job_id)})
        row = r.mappings().first()
    await dispose_engine()
    return dict(row) if row else None


async def _await_lease_expiry(job_id, timeout_s: float = 30.0):
    import asyncio

    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        async with get_db() as db:
            expired = (await db.execute(text(
                "select lease_expires_at <= now() from simulation_jobs where id = :j"),
                {"j": uuid.UUID(job_id)})).scalar()
        await dispose_engine()
        if expired:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("the execution lease never expired")


# the handoff

async def test_a_restart_resumes_its_own_generation_and_rematerialises(tmp_path):
    job_id, geom = await _seed("full-entry", tmp_path)
    exec_id = f"exec-{uuid.uuid4()}"
    sha = geom.ref.sha256

    ws_a = tmp_path / "workspace-a"
    # A short CONFIGURED lease, so the dead owner's claim lapses on its own schedule. A restart
    # is refused while the lease is live - that is the anti-double-run guard, not a bug - so the
    # test waits on real database state rather than on a fixed sleep.
    a, proc_a = _child(job_id, geom, ws_a, execution_id=exec_id, suspend=True, lease_seconds="4")

    assert proc_a.returncode == -9 or proc_a.returncode == 9, proc_a.returncode
    assert set(a["stages"]) == {"stage1"}, "A should have checkpointed stage1 and died at the gate"
    assert a["stages"]["stage1"]["hops"] == 1
    assert ws_a in Path(a["stages"]["stage1"]["path"]).parents
    assert a["stages"]["stage1"]["sha"] == sha

    row_a = await _job_row(job_id)
    assert row_a["status"] == "running", row_a       # nonterminal: genuinely resumable
    assert row_a["execution_generation"] == 1
    assert row_a["pipeline_execution_id"] == exec_id

    # process A is gone, and so is everything it wrote
    stale_path = a["stages"]["stage1"]["path"]
    shutil.rmtree(ws_a)
    assert not Path(stale_path).exists()

    await _await_lease_expiry(job_id)

    # the same EXECUTION restarts (container restart), so the generation is kept
    ws_b = tmp_path / "workspace-b"
    b, _ = _child(job_id, geom, ws_b, execution_id=exec_id)

    assert b["pid"] != os.getpid()
    assert Path(b["cwd"]).resolve() == ws_b.resolve()

    row_b = await _job_row(job_id)
    assert row_b["execution_generation"] == 1, "a restart of the same execution took a new generation"

    # CONTINUATION, not replay. stage1 completed durably in process A, so process B must not
    # run it again - a node that already wrote to the database or published an event would
    # otherwise do it twice. Only the stages after the saved position execute here.
    assert set(b["stages"]) == {"stage2"}, \
        f"stage1 re-ran: the graph restarted instead of resuming ({sorted(b['stages'])})"
    obs = b["stages"]["stage2"]
    assert obs["hops"] == 2, "the hop counter did not carry across the restart"

    # ...and it continued against workspace B's own verified file, never A's.
    assert ws_b in Path(obs["path"]).parents
    assert obs["path"] != stale_path
    assert str(ws_a) not in obs["path"]
    assert obs["sha"] == sha                     # rematerialised, byte-identical
    assert obs["source_id"] == geom.ref.source_id     # durable identity unchanged


async def test_a_live_lease_refuses_a_second_executor(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("conflict", tmp_path)
    a, _ = _child(job_id, geom, tmp_path / "ws-live", execution_id=f"exec-{uuid.uuid4()}")
    assert a["stages"]["stage1"]["hops"] == 1

    # re-arm the lease so it is unambiguously live, then let a DIFFERENT execution try
    async with get_db() as db:
        await db.execute(text(
            "update simulation_jobs set status='running', "
            "lease_expires_at = now() + interval '900 seconds' where id = :j"),
            {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()

    other, _ = _child(job_id, geom, tmp_path / "ws-other", execution_id=f"exec-{uuid.uuid4()}")
    assert other["result"]["status"] == "skipped"
    assert other["result"]["reason"] == "active_lease_conflict"
    assert other["stages"] == {}, "the graph ran despite another owner holding the lease"


async def test_a_takeover_after_lease_expiry_starts_a_new_generation(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("takeover", tmp_path)
    _child(job_id, geom, tmp_path / "ws-dead", execution_id=f"exec-{uuid.uuid4()}")

    async with get_db() as db:
        await db.execute(text(
            "update simulation_jobs set status='running', "
            "lease_expires_at = now() - interval '1 second' where id = :j"),
            {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()

    taker, _ = _child(job_id, geom, tmp_path / "ws-taker", execution_id=f"exec-{uuid.uuid4()}")
    row = await _job_row(job_id)
    assert row["execution_generation"] == 2, row
    assert taker["stages"]["stage1"]["hops"] == 1, \
        "a takeover inherited the previous generation's state"


async def test_a_new_generation_gets_its_own_thread_and_starts_fresh(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("generation", tmp_path)
    first, _ = _child(job_id, geom, tmp_path / "g1", execution_id=f"exec-{uuid.uuid4()}")
    assert first["stages"]["stage1"]["hops"] == 1

    async with get_db() as db:
        await db.execute(text(
            "update simulation_jobs set status='running', "
            "lease_expires_at = now() - interval '1 second' where id = :j"),
            {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()

    second, _ = _child(job_id, geom, tmp_path / "g2", execution_id=f"exec-{uuid.uuid4()}")
    assert "stage1" in second["stages"], "a new generation inherited the old thread's state"
    assert second["stages"]["stage1"]["hops"] == 1
    assert second["stages"]["stage1"]["sha"] == geom.ref.sha256


async def test_a_resume_whose_source_became_unusable_never_reaches_the_graph(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("unusable", tmp_path)
    exec_id = f"exec-{uuid.uuid4()}"
    a, proc_a = _child(job_id, geom, tmp_path / "ok", execution_id=exec_id, suspend=True)
    assert a["stages"]["stage1"]["hops"] == 1

    # corrupt the catalog row so snapshot and row disagree, and re-arm for a restart
    async with get_db() as db:
        await db.execute(text("update geometry_sources set sha256 = :s where id = :i"),
                         {"s": "e" * 64, "i": uuid.UUID(geom.ref.source_id)})
        await db.execute(text(
            "update simulation_jobs set status='running', "
            "lease_expires_at = now() - interval '1 second' where id = :j"),
            {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()

    b, _ = _child(job_id, geom, tmp_path / "broken", execution_id=exec_id)
    assert b["result"]["status"] == "failed"
    assert b["result"]["reason"] == "geometry_unavailable"
    assert b["stages"] == {}, "the graph ran despite an unusable source"

    row = await _job_row(job_id)
    assert row["status"] == "failed", row      # terminal authority still applied
