# Responsibility: Verify a durably completed node never replays, and an unreadable checkpoint never silently restarts.
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

_OWNER = "tenant-disposition"

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
from meshpipeline.pipeline.geometry_state import geometry_path

LEDGER = Path(os.environ["LEDGER"])
DIE = os.environ.get("DIE_AT", "")
DIE_AFTER_GRAPH = os.environ.get("DIE_AFTER_GRAPH") == "1"
# WHICH durability the crash is gated on. "" keeps the weak "any checkpoint exists" wait; an
# integer waits for that many nodes to have completed AND been checkpointed.
DIE_AFTER_HOPS = os.environ.get("DIE_AFTER_HOPS", "")
# EFFECT-BEARING mode. `first` performs real production durable effects through the real
# authorities, then optionally dies at the end of its body - after the effects are durable and
# BEFORE it returns, so LangGraph cannot yet have written the checkpoint that records its
# completion. That is the interleaving a pre-durable replay needs, and it is deterministic:
# nothing is timed, the node simply does not return.
EFFECTS = os.environ.get("F1_EFFECTS") == "1"
DIE_AFTER_EFFECTS = os.environ.get("F1_DIE_AFTER_EFFECTS") == "1"
OWNER = os.environ.get("F1_OWNER", "")


class _S(TypedDict, total=False):
    geometry: dict
    hops: int
    reviewer_verdict: str
    outcome_message: str
    # DECLARED so the node can compute the same thread identity production uses. Without it the
    # channel is dropped, the node polls a thread that does not exist, and the wait outlives the
    # run's own lease - at which point the fenced checkpointer rejects its writes and the run
    # leaves no checkpoint at all.
    execution_generation: int


async def _wait_for_checkpoint(state):
    # Block until an INDEPENDENT connection can see this run's durable checkpoint.
    #
    # LangGraph does not make a checkpoint durable by the time the next node starts: measured on
    # the installed version, an outside connection sees zero rows at the next node's entry and
    # they appear a moment later. Killing the process at node entry therefore destroys a run that
    # never checkpointed at all, which is not the crash this suite means to model.
    #
    # The thread is matched by job + state-schema prefix rather than by reading the generation
    # out of graph state, so the wait cannot be defeated by a channel the test graph forgot to
    # declare. step >= 0 excludes LangGraph's pre-run bookkeeping row, so this returns only once
    # real node progress is durable.
    import asyncio as _a

    import psycopg
    import meshpipeline.settings.providers as _pc
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as _V
    prefix = f"{job_id}:s{_V}:g%"
    dsn = _pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    # MUST yield to the event loop: LangGraph's checkpoint write is itself a coroutine, so a
    # blocking sleep here starves the very write this is waiting for and the wait can never
    # succeed. That is what made this harness look like a durability defect.
    loop = _a.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute("select count(*) from checkpoints where thread_id like %s "
                        "and (metadata->>'step')::int >= 0", (prefix,))
            if (cur.fetchone() or [0])[0] > 0:
                return True
        await _a.sleep(0.02)
    raise AssertionError("no durable checkpoint appeared before the crash window closed")


async def _wait_for_completed_node(state, hops):
    # Block until the checkpoint carrying a COMPLETED node's own output is durable to an
    # independent connection.
    #
    # _wait_for_checkpoint above answers a weaker question - does ANY checkpoint exist? - and the
    # first row satisfying it is the one written BEFORE the first node runs (hops=0,
    # branch:to:first). A crash gated on that is in an UNDEFINED durability state: whether the
    # completing checkpoint happens to have landed is incidental to what was awaited. That
    # ambiguity is what made "a completed node replayed" an intermittent assertion.
    #
    # hops is the durable evidence of progress: each node increments it, so hops >= n means n
    # nodes have completed AND been checkpointed.
    import asyncio as _a

    import psycopg
    import meshpipeline.settings.providers as _pc
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as _V
    prefix = f"{job_id}:s{_V}:g%"
    dsn = _pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    loop = _a.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute("select count(*) from checkpoints where thread_id like %s "
                        "and (checkpoint->'channel_values'->>'hops')::int >= %s", (prefix, hops))
            if (cur.fetchone() or [0])[0] > 0:
                return True
        await _a.sleep(0.02)
    raise AssertionError(
        f"no durable checkpoint recording hops>={hops} appeared before the crash window closed")


async def _durable_effects(state, node):
    # THE production authorities, called the way production calls them. The operation identity is
    # stable across a replay of the same node in the same generation - which is the whole point:
    # a replayed node must re-derive the same identity, or its idempotency authority has nothing
    # to recognise.
    from meshpipeline.application.execution_publisher import execution_publisher
    from meshpipeline.capture import scope as _cap_scope
    from meshpipeline.capture.logger import TrainingLogger

    op_id = f"f1-{node}"
    # EVERY generation observation point, from inside the node, before anything is recorded.
    from meshpipeline.application import execution_fence as _fence
    _own = _fence.current_ownership()
    LEDGER.open("a").write(json.dumps({
        "barrier": "generation_observation", "node": node,
        "ownership_bound": _own is not None,
        "ownership_generation": getattr(_own, "execution_generation", None),
        "token_fingerprint": (_own.token_hash() if _own is not None
                              and hasattr(_own, "token_hash") else ""),
        "capture_scope_before_bind": (_cap_scope.current_scope() is not None),
        "capture_generation_before_bind": _cap_scope.current_generation(),
        "state_execution_generation": state.get("execution_generation"),
    }) + "\n")
    _cap_scope.bind(OWNER, job_id)
    LEDGER.open("a").write(json.dumps({
        "barrier": "generation_after_bind", "node": node,
        "capture_generation": _cap_scope.current_generation(),
    }) + "\n")
    TrainingLogger(job_id).log("f1_effect", {"node": node}, op_id=op_id)
    await execution_publisher(job_id, agent="executor").anote(
        f"f1 effect from {node}", op_id=op_id)
    return op_id


def _mk(name, last=False):
    async def _node(state):
        if DIE == name:
            await (_wait_for_completed_node(state, int(DIE_AFTER_HOPS))
               if DIE_AFTER_HOPS else _wait_for_checkpoint(state))
            os._exit(9)
        LEDGER.open("a").write(json.dumps({"node": name, "path": geometry_path(state)}) + "\n")
        if EFFECTS:
            op_id = await _durable_effects(state, name)
            if DIE_AFTER_EFFECTS and name == "first":
                # The effects are durable and the node has NOT returned, so no checkpoint records
                # its completion. Dying here is the pre-durable crash by construction.
                LEDGER.open("a").write(json.dumps({"barrier": "effects_committed",
                                                   "node": name, "op_id": op_id}) + "\n")
                os._exit(9)
        out = {"hops": int(state.get("hops", 0)) + 1}
        if last:
            out.update(reviewer_verdict="PASS", outcome_message="done")
        return out
    return _node


async def _final(state):
    # the LAST node: after it returns, the graph is complete and its checkpoint lands. Dying
    # BEFORE its body leaves a pending position; dying after the whole graph (DIE_AFTER_GRAPH)
    # models a crash between the final checkpoint and terminal database authority.
    if DIE == "final":
        await (_wait_for_completed_node(state, int(DIE_AFTER_HOPS))
           if DIE_AFTER_HOPS else _wait_for_checkpoint(state))
        os._exit(9)
    LEDGER.open("a").write(json.dumps({"node": "final", "path": geometry_path(state)}) + "\n")
    if DIE_AFTER_GRAPH:
        import asyncio as _a
        await _a.sleep(0)
    return {"hops": int(state.get("hops", 0)) + 1,
            "reviewer_verdict": "PASS", "outcome_message": "done"}


def _fake_graph(checkpointer=None):
    g = StateGraph(_S)
    g.add_node("first", _mk("first"))
    g.add_node("final", _final)
    g.add_edge(START, "first")
    g.add_edge("first", "final")
    g.add_edge("final", END)
    return g.compile(checkpointer=checkpointer)


graph_module.build_graph = _fake_graph

import meshpipeline.application.pipeline_run as pr

if DIE_AFTER_GRAPH:
    # crash AFTER the graph completes and checkpoints, BEFORE terminal authority commits
    _real = pr._invoke_with_heartbeat if hasattr(pr, "_invoke_with_heartbeat") else None
    import meshpipeline.application.final_result as _fr
    _orig = _fr.build_final_result

    def _boom(*a, **k):
        os._exit(9)
    _fr.build_final_result = _boom

print("RESULT " + json.dumps({"pid": os.getpid(),
                              "result": pr.run_pipeline(**json.loads(kwargs_json))}))
"""


async def _seed(marker: str, tmp_path, *, size: int = 5000,
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


def _child(job_id, geom, workspace: Path, ledger: Path, *, execution_id, die_at="",
           die_after_graph=False, die_after_hops=None, lease_seconds="900", extra_env=None):
    workspace.mkdir(parents=True, exist_ok=True)
    kwargs = {"job_id": job_id, "owner_id": _OWNER, "geometry_source": geom.ref.to_payload(),
              "geometry_interpretation": geom.interpretation_snapshot.to_payload(),
              "request_txt": "mesh it", "mesh_engine": "cfmesh"}
    env = {**os.environ, "PIPELINE_EXECUTION_ID": execution_id, "LEDGER": str(ledger),
           "DIE_AT": die_at, "DIE_AFTER_GRAPH": "1" if die_after_graph else "0",
           "DIE_AFTER_HOPS": "" if die_after_hops is None else str(die_after_hops),
           "WORKSPACE_BASE": str(workspace), "WORKER_LEASE_SECONDS": lease_seconds,
           "WORKER_HEARTBEAT_SECONDS": str(max(1, int(lease_seconds) // 2)),
           **(extra_env or {})}
    proc = subprocess.run([sys.executable, "-c", _CHILD, job_id, str(workspace),
                           json.dumps(kwargs)],
                          cwd=workspace, capture_output=True, text=True, timeout=300, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    return (json.loads(lines[-1][len("RESULT "):]) if lines else {}), proc


def _ledger(path: Path) -> list[str]:
    # INVOCATIONS only. Barrier markers carry the same node name and must never be counted as
    # one: the whole question is how many times the body ran.
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [r["node"] for r in rows if "barrier" not in r]


def _barriers(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    return [r for r in rows if "barrier" in r]


async def _job(job_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        row = (await db.execute(text(
            "select status::text as status, execution_generation from simulation_jobs "
            "where id = :j"), {"j": uuid.UUID(job_id)})).mappings().first()
    await dispose_engine()
    return dict(row)


def _capture_operations(job_id, name: str = "f1_effect") -> list[dict]:
    # Durable capture operations, counted through an INDEPENDENT connection. `op_key` is the
    # production identity (job, name, kind, attempt, generation, op_id); counting DISTINCT keys
    # answers "how many operations" while count(*) would answer "how many rows", and the
    # difference between them is exactly what a replay would create if the key did not hold.
    import psycopg

    import meshpipeline.settings.providers as pc
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        # THIS operation, not every record the run happens to write. A run also captures intake
        # and claim-time records under their own identities; counting those would answer a
        # different question than "did the replayed node write its record twice?".
        cur.execute("select execution_generation, op_key, count(*) from capture_operations "
                    "where job_id = %s and name = %s group by 1, 2 order by 1, 2",
                    (str(job_id), name))
        return [{"generation": g, "op_key": k[:16], "rows": n} for g, k, n in cur.fetchall()]


def _event_counts(job_id) -> dict:
    from meshpipeline.adapters.event_stream.redis import sync_client
    from meshpipeline.events.channels import log_key_for, opkey_set_for

    r = sync_client()
    events = [json.loads(e) for e in r.lrange(log_key_for(str(job_id)), 0, -1)]
    keys = {k.decode() if isinstance(k, bytes) else k
            for k in r.smembers(opkey_set_for(str(job_id)))}
    return {"events": len(events),
            "f1_effect_events": sum(1 for e in events if "f1 effect from" in json.dumps(e)),
            # THIS operation's key, not the run's whole key set: continuation legitimately adds
            # keys for the events it publishes afterwards.
            "f1_op_keys": sorted(k for k in keys if "f1-first" in k),
            "op_keys": len(keys)}


def _checkpoint_rows(job_id) -> list[dict]:
    import psycopg

    import meshpipeline.settings.providers as pc
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as V
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as V
        cur.execute("select metadata->>'step', metadata->>'source', checkpoint::text "
                    "from checkpoints where thread_id like %s "
                    "order by (metadata->>'step')::int", (f"{job_id}:s{V}:g%",))
        return [{"step": int(step), "source": source,
                 "hops": (json.loads(ck).get("channel_values") or {}).get("hops")}
                for step, source, ck in cur.fetchall()]


async def _durable_artifacts(job_id) -> int:
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        n = (await db.execute(text("select count(*) from artifacts where job_id = :j"),
                              {"j": uuid.UUID(job_id)})).scalar_one()
    await dispose_engine()
    return int(n)


async def _expire_lease(job_id):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        await db.execute(text("update simulation_jobs set lease_expires_at = now() - "
                              "interval '1 second' where id = :j"), {"j": uuid.UUID(job_id)})
        await db.commit()
    await dispose_engine()


# absent

async def test_no_checkpoint_runs_the_whole_graph_from_start(tmp_path):
    job_id, geom = await _seed("absent", tmp_path)
    ledger = tmp_path / "l.jsonl"
    out, _ = _child(job_id, geom, tmp_path / "ws", ledger, execution_id=f"e-{uuid.uuid4()}")
    assert _ledger(ledger) == ["first", "final"]
    assert (await _job(job_id))["status"] in ("succeeded", "failed")


# pending

async def test_a_durably_completed_node_does_not_replay(tmp_path):
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"
    ws_a = tmp_path / "ws-a"
    _, proc = _child(job_id, geom, ws_a, ledger, execution_id=exec_id, die_at="final",
                     die_after_hops=1)
    assert proc.returncode in (9, -9)
    assert _ledger(ledger) == ["first"]

    shutil.rmtree(ws_a)
    await _expire_lease(job_id)
    _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id)
    assert _ledger(ledger) == ["first", "final"], "a durably completed node replayed"


async def test_a_crash_before_any_node_completes_replays_but_duplicates_no_durable_effect(tmp_path):
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"
    ws_a = tmp_path / "ws-a"
    _, proc = _child(job_id, geom, ws_a, ledger, execution_id=exec_id, die_at="first")
    assert proc.returncode in (9, -9)
    assert _ledger(ledger) == [], "the node recorded work despite dying at its own entry"

    shutil.rmtree(ws_a)
    await _expire_lease(job_id)
    _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id)

    led = _ledger(ledger)
    assert led[-1] == "final", f"the run did not reach the last node: {led}"
    assert led.count("final") == 1, f"the last node ran more than once: {led}"
    row = await _job(job_id)
    assert row["status"] in ("succeeded", "failed"), row
    # THE durable guarantee, independent of how often a function re-entered.
    assert await _durable_artifacts(job_id) <= 1, "a replayed node duplicated durable state"


# complete + terminal

async def test_a_terminal_job_does_nothing(tmp_path):
    job_id, geom = await _seed("terminal", tmp_path)
    ledger = tmp_path / "l.jsonl"
    _child(job_id, geom, tmp_path / "ws-1", ledger, execution_id=f"e-{uuid.uuid4()}")
    before = _ledger(ledger)
    again, _ = _child(job_id, geom, tmp_path / "ws-2", ledger, execution_id=f"e-{uuid.uuid4()}")
    assert again["result"].get("skipped") == "already_terminal", again["result"]
    assert _ledger(ledger) == before, "a terminal job re-ran its graph"


# complete + NOT terminal

async def test_a_completed_graph_with_no_terminal_status_reconciles_without_replay(tmp_path):
    job_id, geom = await _seed("crash-after-graph", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"

    _, proc = _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=exec_id,
                     die_after_graph=True)
    assert proc.returncode in (9, -9), proc.returncode
    assert _ledger(ledger) == ["first", "final"], "the graph did not complete before the crash"
    assert (await _job(job_id))["status"] == "running", "the job should not be terminal yet"

    await _expire_lease(job_id)
    out, _ = _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id)

    assert _ledger(ledger) == ["first", "final"], \
        f"a completed graph was re-run on recovery ({_ledger(ledger)})"
    assert (await _job(job_id))["status"] in ("succeeded", "failed"), \
        "recovery did not terminalize the job"


# 5/6. unreadable

async def test_an_unreadable_checkpoint_never_silently_restarts(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("corrupt", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"
    _, proc = _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=exec_id, die_at="final")
    assert proc.returncode in (9, -9), proc.returncode
    assert _ledger(ledger) == ["first"]
    await _expire_lease(job_id)

    thread = await _thread_of(job_id)
    # A marker unique to THIS run, so the blast-radius assertion below is about this corruption
    # and not about one a previously-ordered test left in the shared database.
    marker = f"unknown-serializer-{uuid.uuid4()}"
    neighbour = await _some_readable_other_thread(thread)

    # 1. readable BEFORE the mutation
    assert await _saver_reads(thread) is True

    # 2. mutate ONLY this thread's blobs, and prove the statement's blast radius
    async with get_db() as db:
        affected = (await db.execute(text(
            "update checkpoint_blobs set type = :m where thread_id = :t returning 1"),
            {"m": marker, "t": thread})).rowcount
        other = (await db.execute(text(
            "select count(*) from checkpoint_blobs where thread_id <> :t and type = :m"),
            {"t": thread, "m": marker})).scalar()
        await db.commit()
    await dispose_engine()
    assert affected > 0, "the corruption matched no rows - it proved nothing"
    assert other == 0, "the corruption escaped this thread"

    # 3. the installed production-configured saver now FAILS on this exact thread
    assert await _saver_reads(thread) is False

    # 4. ...and a neighbouring thread is still readable
    if neighbour:
        assert await _saver_reads(neighbour) is True, "an unrelated checkpoint was damaged"

    # 5. the runtime refuses: no replay, no fresh start, nothing added to the ledger
    out, proc_b = _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id)
    assert _ledger(ledger) == ["first"], \
        f"an unreadable checkpoint restarted the graph ({_ledger(ledger)})"

    combined = (proc_b.stdout + proc_b.stderr)
    for leak in (geom.ref.object_key, geom.ref.sha256, geom.ref.source_id, thread, "cbadminsecret", "cb-data"):
        assert leak not in combined, f"checkpoint failure output exposed {leak!r}"


async def _thread_of(job_id: str) -> str:
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as V
    row = await _job(job_id)
    return f"{job_id}:s{V}:g{row['execution_generation']}"


async def _some_readable_other_thread(exclude: str):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        rows = (await db.execute(text(
            "select distinct thread_id from checkpoints where thread_id <> :t limit 10"),
            {"t": exclude})).scalars().all()
    await dispose_engine()
    for candidate in rows:
        if await _saver_reads(candidate):
            return candidate
    return None


async def _saver_reads(thread: str) -> bool:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    import meshpipeline.settings.providers as pc
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
        try:
            await cp.aget_tuple({"configurable": {"thread_id": thread}})
            return True
        except Exception:  # noqa: BLE001 - any failure to read is the condition under test
            return False


# generation isolation

async def test_a_new_generation_never_inherits_another_generations_position(tmp_path):
    job_id, geom = await _seed("generation", tmp_path)
    ledger = tmp_path / "l.jsonl"
    _, proc = _child(job_id, geom, tmp_path / "g1", ledger, execution_id=f"e-{uuid.uuid4()}",
                     die_at="final")
    assert _ledger(ledger) == ["first"]
    gen1 = (await _job(job_id))["execution_generation"]

    await _expire_lease(job_id)
    _child(job_id, geom, tmp_path / "g2", ledger, execution_id=f"e-{uuid.uuid4()}")

    assert (await _job(job_id))["execution_generation"] == gen1 + 1
    assert _ledger(ledger) == ["first", "first", "final"], \
        "a new generation did not start from START"

    # the previous generation's checkpoint is still there - takeover does not destroy history
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        n = (await db.execute(text(
            "select count(*) from checkpoints where thread_id like :p"),
            {"p": f"{job_id}:s%:g{gen1}"})).scalar()
    await dispose_engine()
    assert n > 0, "the previous generation's checkpoint was destroyed"


# pre-durable effect replay
#
# `first` commits REAL durable effects through the production authorities and then dies at the
# end of its body, before returning. LangGraph writes the checkpoint recording a node's
# completion only after that node returns, so this crash is pre-durable BY CONSTRUCTION - it is
# not timed, and it cannot drift. The proven structure is step -1 (input), step 0 (written
# before `first` runs) and step 1 with hops=1 (records that `first` completed).
#
# The historical wait was `count(*) where step >= 0`, which the step-0 row satisfies. That is why
# it could release before `first` had done anything, and why the crash it gated was in an
# undefined durability state.

_EFFECT_ENV = {"F1_EFFECTS": "1", "F1_OWNER": _OWNER, "DATA_COLLECTION_ENABLED": "true"}


async def test_a_pre_durable_crash_replays_the_node_without_duplicating_its_effects(tmp_path):
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"

    _, proc = _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=exec_id,
                     extra_env={**_EFFECT_ENV, "F1_DIE_AFTER_EFFECTS": "1"})
    assert proc.returncode in (9, -9), proc.stderr[-2000:]

    # AT THE KILL, through independent connections: the effects are durable and the checkpoint
    # that would record `first`'s completion is not.
    at_kill = _checkpoint_rows(job_id)
    steps = {r["step"] for r in at_kill}
    assert 0 in steps, f"process A never reached the step-0 checkpoint: {at_kill}"
    assert not any(r["step"] == 1 and r["hops"] == 1 for r in at_kill), (
        f"the completing checkpoint for `first` was already durable - not a pre-durable crash: "
        f"{at_kill}")
    assert len(_capture_operations(job_id)) == 1, "process A's capture operation is not durable"
    events_at_kill = _event_counts(job_id)
    assert events_at_kill["f1_effect_events"] == 1, events_at_kill

    shutil.rmtree(tmp_path / "ws-a")
    await _expire_lease(job_id)
    _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id, extra_env=_EFFECT_ENV)

    # THE INVOCATION replays - that is correct at-least-once behaviour for a node whose
    # completion was never durable.
    nodes = _ledger(ledger)
    assert nodes.count("first") == 2, f"the pre-durable node did not replay: {nodes}"
    assert nodes[-1] == "final", f"the run did not continue to completion: {nodes}"

    # THE EFFECTS do not. Each authority recognised the replayed operation identity.
    ops = _capture_operations(job_id)
    assert len(ops) == 1 and ops[0]["rows"] == 1, (
        f"the replayed node's capture operation is not exactly one durable record: {ops}")
    after = _event_counts(job_id)
    assert after["f1_effect_events"] == 1, (
        f"the replayed node published a second user-visible event: {after}")
    assert after["f1_op_keys"] == events_at_kill["f1_op_keys"] and len(after["f1_op_keys"]) == 1, (
        f"the replayed event took a different operation identity instead of being suppressed by "
        f"the existing one: {events_at_kill['f1_op_keys']} -> {after['f1_op_keys']}")

    final = _checkpoint_rows(job_id)
    # `hops`, not a step number: a resumed run writes an `update` row for the continuation, which
    # shifts every later step. What is durable evidence of progress is the channel value each
    # node increments - so `first` completed and was checkpointed iff some row carries hops >= 1.
    assert any((r["hops"] or 0) >= 1 for r in final), (
        f"process B never made `first`'s completion durable: {final}")
    assert any((r["hops"] or 0) >= 2 for r in final), (
        f"the run did not checkpoint through `final`: {final}")


async def test_a_post_durable_crash_replays_no_effect_because_it_replays_no_node(tmp_path):
    # The control: the same effect-bearing node, crashed AFTER its completing checkpoint is
    # durable. Without it, the test above would prove only that a replay is survivable, not that
    # the durability boundary is what decides whether one happens.
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"

    _, proc = _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=exec_id,
                     die_at="final", die_after_hops=1, extra_env=_EFFECT_ENV)
    assert proc.returncode in (9, -9), proc.stderr[-2000:]
    at_kill = _checkpoint_rows(job_id)
    assert any(r["step"] == 1 and r["hops"] == 1 for r in at_kill), (
        f"the completing checkpoint was not durable - this is not the post-durable case: {at_kill}")

    shutil.rmtree(tmp_path / "ws-a")
    await _expire_lease(job_id)
    _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id, extra_env=_EFFECT_ENV)

    nodes = _ledger(ledger)
    assert nodes.count("first") == 1, f"a durably completed node replayed: {nodes}"
    assert len(_capture_operations(job_id)) == 1
    assert _event_counts(job_id)["f1_effect_events"] == 1


async def test_the_retired_weak_wait_releases_before_the_node_has_done_anything(tmp_path):
    # WHY the historical contract was ambiguous, as a behaviour rather than a claim: the step-0
    # row - the one the retired `step >= 0` wait accepted - is written BEFORE `first` runs, so it
    # is present in a crash where the node produced nothing at all.
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    _, proc = _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=f"e-{uuid.uuid4()}",
                     die_at="first", extra_env=_EFFECT_ENV)
    assert proc.returncode in (9, -9)
    rows = _checkpoint_rows(job_id)
    assert any(r["step"] >= 0 for r in rows), "the retired wait would not even have released"
    assert not any(r["step"] == 1 for r in rows), rows
    assert _ledger(ledger) == [], "the node body ran despite dying at entry"
    assert _capture_operations(job_id) == [], "an effect landed before the node body ran"


async def _db_generation(job_id) -> dict:
    # THROUGH AN INDEPENDENT CONNECTION: what PostgreSQL says the claim is, beside what the
    # process believed.
    import psycopg

    import meshpipeline.settings.providers as pc
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select execution_generation, active_worker_token is not null "
                    "from simulation_jobs where id = %s", (str(job_id),))
        gen, has_token = cur.fetchone()
    return {"execution_generation": gen, "has_active_token": has_token}


async def test_every_authority_inside_the_node_uses_the_delivered_generation(tmp_path):
    # INITIAL DELIVERY. The generation PostgreSQL granted is the one the node's ownership,
    # the capture scope and the graph state all report - one claim, one number, no defaults.
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    _child(job_id, geom, tmp_path / "ws", ledger, execution_id=f"e-{uuid.uuid4()}",
           extra_env=_EFFECT_ENV)

    seen = [b for b in _barriers(ledger) if b["barrier"] == "generation_observation"]
    assert seen, "the node recorded no generation observation"
    durable = await _db_generation(job_id)
    for observation in seen:
        assert observation["ownership_bound"] is True, (
            "the graph node ran with no execution ownership bound")
        assert observation["ownership_generation"] == durable["execution_generation"], (
            f"the node held generation {observation['ownership_generation']} while PostgreSQL "
            f"recorded {durable['execution_generation']}")
        assert observation["capture_generation_before_bind"] == observation["ownership_generation"], (
            "capture scope reported a different generation from the bound ownership - a capture "
            "record would then carry an identity no replay could match")
        assert observation["state_execution_generation"] == observation["ownership_generation"], (
            "graph state carries a different generation from the claim it runs under")
        assert observation["token_fingerprint"], "no worker token was bound"
        assert len(observation["token_fingerprint"]) <= 16, "a raw worker token was recorded"


async def test_a_restart_of_the_same_execution_keeps_its_generation_and_rotates_its_token(tmp_path):
    # THE PATH a pre-durable worker death actually takes. The lease is expired and the SAME backend
    # execution re-delivers: PostgreSQL keeps the generation and issues a new worker token. That
    # is what makes it a genuine cross-process SAME-GENERATION continuation - two processes, one
    # generation, so both derive identical operation identities - and it is not the
    # new-generation takeover a different backend execution would get.
    job_id, geom = await _seed("pending", tmp_path)
    ledger = tmp_path / "l.jsonl"
    exec_id = f"e-{uuid.uuid4()}"

    _, proc = _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=exec_id,
                     extra_env={**_EFFECT_ENV, "F1_DIE_AFTER_EFFECTS": "1"})
    assert proc.returncode in (9, -9)
    first_claim = await _db_generation(job_id)

    shutil.rmtree(tmp_path / "ws-a")
    await _expire_lease(job_id)
    _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id, extra_env=_EFFECT_ENV)

    observations = [b for b in _barriers(ledger) if b["barrier"] == "generation_observation"]
    assert len(observations) == 2, f"expected one observation per process: {observations}"
    a, b = observations
    assert a["ownership_generation"] == b["ownership_generation"] == \
        first_claim["execution_generation"], (
        f"the restart changed the execution generation: {a['ownership_generation']} -> "
        f"{b['ownership_generation']}")
    assert a["token_fingerprint"] != b["token_fingerprint"], (
        "the restart reused the dead process's worker token instead of rotating it")
    # and because the generation held, both processes derived the SAME operation identity
    assert len(_capture_operations(job_id)) == 1
    assert len(_event_counts(job_id)["f1_op_keys"]) == 1
