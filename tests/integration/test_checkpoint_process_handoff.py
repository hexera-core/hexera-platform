# Responsibility: Verify a checkpoint written by one process resumes in another, and a new generation does not.
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

# Both processes run this. It is the production checkpointer, the production thread identity and
# the production materialisation boundary; only the graph body is a stub, so no model or native
# mesher runs. `mode` decides whether it seeds a checkpoint or resumes one.
_WORKER = r"""
import asyncio, hashlib, json, os, sys
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

import meshpipeline.settings.providers as provcfg
from meshpipeline.adapters.object_storage.factory import build_object_store
from meshpipeline.contracts import object_storage
from meshpipeline.contracts.geometry_source import (
    GeometryInterpretationRef,
    GeometrySourceRef,
)
from meshpipeline.application.geometry_materializer import materialize_for_job
from meshpipeline.persistence.session import get_db
from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION
from meshpipeline.pipeline.geometry_state import geometry_path, geometry_ref


class S(TypedDict, total=False):
    geometry: dict
    seen_path: str
    seen_sha: str
    hops: int


async def _node(state: S) -> dict:
    # what a downstream node actually consumes: a local file it can open
    p = geometry_path(state)
    return {"seen_path": p,
            "seen_sha": hashlib.sha256(Path(p).read_bytes()).hexdigest(),
            "hops": int(state.get("hops", 0)) + 1}


async def main():
    mode, ref_json, workspace, job_id, generation, interp_json = sys.argv[1:7]
    object_storage.set_object_store(build_object_store())
    ref = GeometrySourceRef.from_payload(json.loads(ref_json))
    interp = GeometryInterpretationRef.from_payload(json.loads(interp_json))

    # THE execution entry: this process obtains its own verified copy before the graph exists.
    async with get_db() as db:
        mg = await materialize_for_job(db, ref, interp, workspace=Path(workspace),
                                       job_id=job_id)

    thread_id = f"{job_id}:s{STATE_SCHEMA_VERSION}:g{generation}"
    dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")

    g = StateGraph(S)
    g.add_node("consume", _node)
    g.add_edge(START, "consume")
    g.add_edge("consume", END)

    async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
        await cp.setup()
        graph = g.compile(checkpointer=cp)
        cfg = {"configurable": {"thread_id": thread_id}}
        before = await graph.aget_state(cfg)
        restored = (before.values or {}).get("geometry") if before else None
        # fresh geometry is passed as INPUT, so it overwrites whatever the checkpoint held
        out = await graph.ainvoke({"geometry": mg.to_state()}, config=cfg)

    print(json.dumps({
        "pid": os.getpid(), "cwd": os.getcwd(), "thread_id": thread_id,
        "restored_geometry": restored,
        "seen_path": out["seen_path"], "seen_sha": out["seen_sha"], "hops": out["hops"],
        "ref_source_id": geometry_ref(out).source_id,
    }))

asyncio.run(main())
"""


async def _seed_source(marker: str, tmp_path, *, size: int = 7000,
                       unit: LengthUnit = LengthUnit.millimetre,
                       basis: ResolutionBasis = ResolutionBasis.file_declared):
    from meshpipeline.adapters.object_storage.factory import build_object_store
    from meshpipeline.contracts import object_storage
    from meshpipeline.persistence.session import dispose_engine, get_db

    object_storage.set_object_store(build_object_store())
    async with get_db() as db:
        geom = await persisted_geometry(db, tmp_path=tmp_path / f"seed-{marker}", owner_id=_OWNER,
                                        filename="duct.step", marker=marker, size=size,
                                        unit=unit, basis=basis)
    await dispose_engine()
    return geom


def _run_process(geom, workspace: Path, job_id: str, generation: int, mode: str = "run"):
    workspace.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, mode, json.dumps(geom.ref.to_payload()), str(workspace),
         job_id, str(generation), json.dumps(geom.interpretation_snapshot.to_payload())],
        cwd=workspace, capture_output=True, text=True, timeout=180, env={**os.environ})
    assert proc.returncode == 0, proc.stderr[-3000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


async def test_a_checkpoint_written_by_one_process_is_resumed_by_another(tmp_path):
    geom = await _seed_source("handoff", tmp_path)
    job_id = str(uuid.uuid4())
    sha = geom.ref.sha256

    ws_a = tmp_path / "workspace-a"
    a = _run_process(geom, ws_a, job_id, generation=1)
    assert a["hops"] == 1
    assert ws_a in Path(a["seen_path"]).parents
    assert a["seen_sha"] == sha

    # process A is gone and its workspace with it
    shutil.rmtree(ws_a)
    assert not ws_a.exists()

    ws_b = tmp_path / "workspace-b"
    b = _run_process(geom, ws_b, job_id, generation=1)

    # --- genuinely a different process, on the SAME checkpoint thread
    assert b["pid"] != a["pid"] != os.getpid()
    assert b["thread_id"] == a["thread_id"]
    assert Path(b["cwd"]).resolve() == ws_b.resolve()
    assert b["hops"] == 2, "the checkpoint did not carry over - this was not a resume"

    # --- the checkpoint really did hand back process A's dead path
    assert b["restored_geometry"] is not None, "no state was restored from PostgreSQL"
    stale = b["restored_geometry"]["local_path"]
    assert str(ws_a) in stale
    assert not Path(stale).exists()

    # --- and the graph was handed workspace B's freshly verified file instead
    assert ws_b in Path(b["seen_path"]).parents
    assert b["seen_path"] != stale
    assert b["seen_sha"] == sha                       # byte-identical to the approved upload
    assert b["ref_source_id"] == geom.ref.source_id        # durable identity unchanged


async def test_a_different_generation_does_not_resume_the_previous_one(tmp_path):
    geom = await _seed_source("generation", tmp_path)
    job_id = str(uuid.uuid4())

    g1 = _run_process(geom, tmp_path / "g1", job_id, generation=1)
    g2 = _run_process(geom, tmp_path / "g2", job_id, generation=2)

    assert g1["thread_id"] != g2["thread_id"]
    assert g2["restored_geometry"] is None, "a new generation inherited the old thread's state"
    assert g2["hops"] == 1


async def test_rematerialisation_failure_stops_before_the_graph(tmp_path):
    import dataclasses
    geom = await _seed_source("unavailable", tmp_path)
    job_id = str(uuid.uuid4())

    _run_process(geom, tmp_path / "ok", job_id, generation=1)

    broken = dataclasses.replace(geom.ref, sha256="d" * 64)   # snapshot disagrees with the row
    ws = tmp_path / "broken"
    ws.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, "run", json.dumps(broken.to_payload()), str(ws),
         job_id, "1", json.dumps(geom.interpretation_snapshot.to_payload())],
        cwd=ws, capture_output=True, text=True, timeout=180, env={**os.environ})
    assert proc.returncode != 0
    assert "GeometrySourceError" in proc.stderr
    assert "seen_path" not in proc.stdout, "the graph ran despite an unusable source"
