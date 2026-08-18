# Responsibility: Verify a takeover after a crash keeps one of each logical effect and reconciles with its record.
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

if not os.getenv("DATABASE_URL") or not os.getenv("MINIO_ENDPOINT") or not os.getenv("REDIS_URL"):
    pytest.skip("real PostgreSQL, Redis and MinIO endpoints are required",
                allow_module_level=True)

pytest.importorskip("langgraph.checkpoint.postgres.aio")

_OWNER = "tenant-crash"


@pytest.fixture(autouse=True)
def _composed():
    from meshpipeline.runtime.composition import install_adapters
    install_adapters()

_CHILD = r"""
import hashlib, json, os, sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

job_id, workspace, kwargs_json = sys.argv[1:4]
os.environ["WORKSPACE_BASE"] = workspace

from meshpipeline.runtime.composition import install_adapters
install_adapters()

import meshpipeline.pipeline.graph as graph_module
from meshpipeline.capture.logger import TrainingLogger
from meshpipeline.contracts.event_stream import publisher
from meshpipeline.pipeline.geometry_state import geometry_path

DIE_AT = os.environ.get("DIE_AT", "")
CONFLICT = os.environ.get("CONFLICT") == "1"
LEDGER = Path(os.environ["LEDGER"])


class _S(TypedDict, total=False):
    geometry: dict
    hops: int
    execution_generation: int
    reviewer_verdict: str
    outcome_message: str


async def _wait_durable():
    import asyncio as _a
    import psycopg
    import meshpipeline.settings.providers as _pc
    dsn = _pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    loop = _a.get_running_loop(); deadline = loop.time() + 60
    while loop.time() < deadline:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute("select count(*) from checkpoints where thread_id like %s "
                        "and (metadata->>'step')::int >= 0", ("%s:s%%" % job_id,))
            if (cur.fetchone() or [0])[0] > 0:
                return
        await _a.sleep(0.02)
    raise AssertionError("no durable checkpoint appeared")


def _effectful(name):
    async def _f(state):
        gen = int(state.get("execution_generation", 0) or 0)
        verdict = "FAIL" if CONFLICT else "PASS"
        # a keyed capture record for THIS logical operation
        TrainingLogger(job_id).log("node_effect", {"node": name, "verdict": verdict},
                                   op_id=f"{name}:g{gen}")
        # a keyed public event for the same logical operation
        publisher(job_id, agent="geometry_admission").closing(
            f"{name} finished.", f"nodeevent:{name}:g{gen}")
        # and a keyed PROGRESS publication - the surface a reconnecting user actually reads
        publisher(job_id, agent="geometry_admission").note(f"{name} running", op_id=f"phase:{name}")
        LEDGER.open("a").write(json.dumps({"node": name, "path": geometry_path(state)}) + "\n")
        if DIE_AT == name:
            await _wait_durable(); os._exit(9)
        return {"hops": int(state.get("hops", 0)) + 1}
    return _f


async def _final(state):
    return {"hops": int(state.get("hops", 0)) + 1,
            "reviewer_verdict": "PASS", "outcome_message": "done"}


def _build(checkpointer=None):
    g = StateGraph(_S)
    g.add_node("work", _effectful("work"))
    g.add_node("finish", _final)
    g.add_edge(START, "work")
    g.add_edge("work", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


graph_module.build_graph = _build

from meshpipeline.application.pipeline_run import run_pipeline

print("RESULT " + json.dumps({"pid": os.getpid(), "result": run_pipeline(**json.loads(kwargs_json))}))
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


def _run(job_id, geom, workspace: Path, data_root: Path, ledger: Path, *, execution_id,
         die_at="", conflict=False):
    workspace.mkdir(parents=True, exist_ok=True)
    (data_root / "jobs").mkdir(parents=True, exist_ok=True)
    (data_root / "corpus").mkdir(parents=True, exist_ok=True)
    kwargs = {"job_id": job_id, "owner_id": _OWNER, "geometry_source": geom.ref.to_payload(),
              "geometry_interpretation": geom.interpretation_snapshot.to_payload(),
              "request_txt": "mesh it", "mesh_engine": "cfmesh"}
    env = {**os.environ, "PIPELINE_EXECUTION_ID": execution_id,
           "WORKSPACE_BASE": str(workspace), "LEDGER": str(ledger),
           "DIE_AT": die_at, "CONFLICT": "1" if conflict else "0",
           "DATA_ROOT": str(data_root),
           "JOBS_DIR": str(data_root / "jobs"), "CORPUS_DIR": str(data_root / "corpus"),
           "DATA_COLLECTION_ENABLED": "true"}
    proc = subprocess.run([sys.executable, "-c", _CHILD, job_id, str(workspace),
                           json.dumps(kwargs)],
                          cwd=workspace, capture_output=True, text=True, timeout=300, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    return (json.loads(lines[-1][len("RESULT "):]) if lines else {}), proc


def _backlog(job_id: str) -> list[dict]:
    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.events.channels import log_key_for
    return [json.loads(x) for x in sync_client().lrange(log_key_for(job_id), 0, -1)]


def _capture(data_root: Path, job_id: str, which: str = "corpus",
             only: str = "node_effect") -> list[dict]:
    from meshpipeline.persistence.repositories import capture_repository as cap
    rows = cap.trusted_operations(owner_id=_OWNER, job_id=job_id)
    return [r for r in rows if r["name"] == only] if only else rows


def _conflicts(job_id: str) -> list[dict]:
    from meshpipeline.persistence.repositories import capture_repository as cap
    return cap.conflicted_operations(owner_id=_OWNER, job_id=job_id)


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


# the harness

async def test_crash_takeover_and_replay_keep_one_of_each_logical_effect(tmp_path):
    job_id, geom = await _seed("crash-takeover", tmp_path)
    exec_id = f"e-{uuid.uuid4()}"
    root_a, root_b = tmp_path / "rootA", tmp_path / "rootB"
    ws_a, ws_b = tmp_path / "wsA", tmp_path / "wsB"
    ledger = tmp_path / "ledger.jsonl"

    # --- process A: effects, then death before the node checkpoint is durable
    a, proc_a = _run(job_id, geom, ws_a, root_a, ledger, execution_id=exec_id, die_at="work")
    assert proc_a.returncode in (9, -9), proc_a.stderr[-1500:]

    # independent observers confirm each intended effect actually happened
    assert len(_capture(root_a, job_id)) == 1, _capture(root_a, job_id)
    assert len([e for e in _backlog(job_id) if e.get("type") == "closing"]) == 1

    # --- A's workspace AND its data root are gone
    shutil.rmtree(ws_a)
    shutil.rmtree(root_a)
    assert not ws_a.exists() and not root_a.exists()

    await _expire_lease(job_id)

    # --- process B: same execution and generation, its own workspace and data root
    b, proc_b = _run(job_id, geom, ws_b, root_b, ledger, execution_id=exec_id)
    assert proc_b.returncode == 0, proc_b.stderr[-2500:]
    assert b["pid"] != a.get("pid")

    row = await _job(job_id)
    assert row["execution_generation"] == 1, row     # a restart, not a new generation

    # --- ONE logical public event survives, and the reconnect backlog shows it once
    closings = [e for e in _backlog(job_id) if e.get("type") == "closing"]
    node_events = [e for e in closings if "work finished" in json.dumps(e)]
    assert len(node_events) == 1, node_events

    seqs = [e["seq"] for e in _backlog(job_id)]
    assert seqs == sorted(seqs), seqs
    assert len(seqs) == len(set(seqs)), "a sequence number was reused"

    # --- the public payload carries no private identity
    blob = json.dumps(_backlog(job_id))
    for leak in (geom.ref.object_key, geom.ref.sha256, geom.ref.source_id, "op_key", "payload_sha256",
                 str(ws_b), str(root_b), "rpadminsecret", "sources/"):
        assert leak not in blob, f"public backlog exposed {leak!r}"

    # --- capture: B's own root holds exactly its record; A's root is gone and cannot be exported
    b_records = _capture(root_b, job_id)
    assert len(b_records) == 1, b_records
    assert b_records[0]["op_key"], "the capture record carries no operation identity"

    # --- the replayed PROGRESS publication reaches the reconnecting user exactly once
    notes = [e for e in _backlog(job_id) if e.get("type") == "note"
             and e.get("text") == "work running"]
    assert len(notes) == 1, notes


async def test_a_conflicting_replay_does_not_create_a_second_record(tmp_path):
    job_id, geom = await _seed("conflict", tmp_path)
    exec_id = f"e-{uuid.uuid4()}"
    root, ws_a, ws_b = tmp_path / "root", tmp_path / "wsA", tmp_path / "wsB"
    ledger = tmp_path / "l.jsonl"

    _, proc_a = _run(job_id, geom, ws_a, root, ledger, execution_id=exec_id, die_at="work")
    assert proc_a.returncode in (9, -9)
    assert len(_capture(root, job_id)) == 1

    await _expire_lease(job_id)
    # SAME data root, so the claim is visible - and B produces conflicting content
    _run(job_id, geom, ws_b, root, ledger, execution_id=exec_id, conflict=True)

    # Neither worker's account is trustworthy: A recorded PASS and then crashed, B recorded FAIL
    # and finished. Precedence is not the question - the capture layer cannot know which run
    # produced the delivered mesh, so it refuses to pick and quarantines the operation instead.
    assert _capture(root, job_id) == [], "a disputed payload stayed trusted"
    conflicts = _conflicts(job_id)
    assert len(conflicts) == 1, conflicts
    assert conflicts[0]["conflict_evidence"][0]["competing_sha256"]

    # and the production export consumer admits neither payload
    from meshpipeline.capture.events import EventLog
    assert [e for e in EventLog(job_id, owner_id=_OWNER).load()
            if e.event_type == "node_effect"] == []

    # the mesh itself was never held up by the dispute
    assert (await _job(job_id))["status"] in ("succeeded", "failed"), "the job did not terminalize"


async def test_a_stale_worker_cannot_publish_an_authoritative_closing(tmp_path):
    from meshpipeline.contracts.event_stream import publisher
    from meshpipeline.persistence.repositories.terminal_outbox_repository import dedup_key_for

    job_id, geom = await _seed("stale", tmp_path)
    root, ws = tmp_path / "root", tmp_path / "ws"
    _run(job_id, geom, ws, root, tmp_path / "l.jsonl", execution_id=f"e-{uuid.uuid4()}")

    before = len([e for e in _backlog(job_id) if e.get("type") == "closing"])
    # the superseded worker tries to close the job again
    publisher(job_id).closing("A stale worker's ending.", dedup_key_for(job_id))
    after = [e for e in _backlog(job_id) if e.get("type") == "closing"]

    assert len(after) == before, "a stale worker added a second closing"
    assert not any("stale worker" in json.dumps(e) for e in after)


async def test_a_takeover_reconciles_with_the_crashed_workers_record(tmp_path):
    job_id, geom = await _seed("preserve", tmp_path)
    exec_id = f"e-{uuid.uuid4()}"
    root_a, root_b = tmp_path / "rootA", tmp_path / "rootB"
    ws_a, ws_b = tmp_path / "wsA", tmp_path / "wsB"

    _, proc_a = _run(job_id, geom, ws_a, root_a, tmp_path / "l.jsonl",
                     execution_id=exec_id, die_at="work")
    assert proc_a.returncode in (9, -9)
    before = _capture(root_a, job_id)
    assert len(before) == 1 and before[0]["payload"]["verdict"] == "PASS"

    # A's whole data root is gone before the takeover even starts
    shutil.rmtree(root_a)
    await _expire_lease(job_id)

    _, proc_b = _run(job_id, geom, ws_b, root_b, tmp_path / "l.jsonl", execution_id=exec_id)
    assert proc_b.returncode == 0, proc_b.stderr[-2000:]

    after = _capture(root_b, job_id)
    assert len(after) == 1, f"the replayed record was captured twice: {after}"
    assert after[0]["payload_sha256"] == before[0]["payload_sha256"], "A's record was replaced"
    assert _conflicts(job_id) == [], "a faithful replay was mistaken for a disagreement"

    from meshpipeline.capture.events import EventLog
    assert len([e for e in EventLog(job_id, owner_id=_OWNER).load()
                if e.event_type == "node_effect"]) == 1
