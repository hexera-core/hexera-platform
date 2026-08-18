# Responsibility: Verify a completed node's side effect is not repeated by a restart, and events stay ordered.
from __future__ import annotations

import json
import os
import re
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

_OWNER = "tenant-sideeffect"

# Each node appends one line to a shared ledger and publishes one real Redis event, so a repeat
# is COUNTABLE rather than inferred.
_CHILD = r"""
import hashlib, json, os, sys, time
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

job_id, workspace, kwargs_json = sys.argv[1:4]
os.environ["WORKSPACE_BASE"] = workspace

from meshpipeline.runtime.composition import install_adapters
install_adapters()

import meshpipeline.pipeline.graph as graph_module
from meshpipeline.adapters.event_stream.redis import JobPublisher
from meshpipeline.pipeline.geometry_state import geometry_path

LEDGER = Path(os.environ["LEDGER"])
DIE = os.environ.get("DIE_AT", "")


class _S(TypedDict, total=False):
    geometry: dict
    hops: int


# Measured persistence semantics of the installed LangGraph (see the saver trace):
#   * the aput_writes CALL for a node is issued before the following node executes
#   * its awaited RETURN may be deferred until after later nodes have run
#   * every consolidated aput is deferred the same way
#   * only independent PostgreSQL visibility proves when a write is actually durable
def _effect(name, state):
    # THREE separately measured effects: the ledger counts function invocations, the capture
    # authority owns durable idempotency through its op_key uniqueness constraint, and the
    # publisher is the user-visible event. A replay must not collapse them into one number.
    LEDGER.open("a").write(json.dumps({"node": name, "path": geometry_path(state)}) + "\n")
    from meshpipeline.capture import trace as _capture
    _record({"event": "capture_attempt", "node": name, "op_id": f"node:{name}",
             "pid": os.getpid()})
    # ONE stable op_id for this logical operation, identical before and after replay
    _capture.add_event(job_id, f"{name}_completed", {"node": name},
                       attributes={"op_id": f"node:{name}"})
    _record({"event": "publish_attempt", "node": name, "pid": os.getpid()})
    # The SAME logical occurrence across replay. Without op_id the publisher republishes by
    # design, because two notes with identical words can be genuine repeated progress.
    JobPublisher(job_id, agent="geometry_admission").note(
        f"{name} completed", "info", op_id=f"node:{name}")


#: How the child is told to die. Each names a WINDOW, not a moment in wall-clock time.
#:   pre_durable   inside the node, after its effect, BEFORE it returns - so no checkpoint
#:                 recording this node can exist yet. Needs no observation at all.
#:   post_durable  inside the FOLLOWING node, once the previous node's committed channel state
#:                 is visible on an independent connection.
DIE_MODE = os.environ.get("DIE_MODE", "")
EXPECT_HOPS = int(os.environ.get("EXPECT_HOPS", "1"))
OBSERVED = Path(os.environ["OBSERVED"]) if os.environ.get("OBSERVED") else None
KILL_AFTER_STEP1 = os.environ.get("KILL_AFTER_STEP1") == "1"
KILL_AFTER_FIRST_WRITE = os.environ.get("KILL_AFTER_FIRST_WRITE") == "1"
HOLD_UNTIL_FIRST_WRITE = os.environ.get("HOLD_UNTIL_FIRST_WRITE") == "1"
KILL_AFTER_FINAL = os.environ.get("KILL_AFTER_FINAL") == "1"
KILL_TARGET_STEP1 = os.environ.get("KILL_TARGET_STEP1") == "1"


def _thread_of(state):
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as _V
    return f"{job_id}:s{_V}:g{int(state.get('execution_generation', 0) or 0)}"


def _dsn():
    import meshpipeline.settings.providers as _pc
    return _pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")


async def _observe(fn, *args):
    import asyncio as _a
    return await _a.to_thread(fn, *args)


def _first_write_row() -> tuple[str, str]:
    for o in _observations_local():
        if o.get("event") == "aput_writes_call" and \
                sorted(o.get("channels") or []) == ["branch:to:gate", "hops"]:
            return o["task_id"], o.get("thread") or ""
    raise AssertionError("first's aput_writes call was never recorded")


def _first_write_task() -> str:
    # process-local trace ONLY, to name which task to look for; durability is proved in PostgreSQL
    for o in _observations_local():
        if o.get("event") == "aput_writes_call" and \
                sorted(o.get("channels") or []) == ["branch:to:gate", "hops"]:
            return o["task_id"]
    raise AssertionError("first's aput_writes call was never recorded")


def _observations_local() -> list:
    if not OBSERVED or not OBSERVED.exists():
        return []
    return [json.loads(ln) for ln in OBSERVED.read_text().splitlines() if ln.strip()]


def _visible_task_write(thread, task_id):
    # the COMPLETE expected channel set for that exact task, never an arbitrary hops row
    import psycopg
    with psycopg.connect(_dsn()) as c, c.cursor() as cur:
        cur.execute("select checkpoint_id, checkpoint_ns, channel from checkpoint_writes "
                    "where thread_id=%s and task_id=%s order by idx", (thread, task_id))
        rows = cur.fetchall()
    return rows if {r[2] for r in rows} >= {"hops", "branch:to:gate"} else None


def _durable_completion(thread, expected_hops):
    # THE node-specific durability authority. This LangGraph version leaves metadata.writes null,
    # so completion is read from the committed channel state instead: each node increments `hops`,
    # so `hops >= n` on a loop checkpoint is exactly "n nodes have completed and been persisted".
    # A separate connection is what makes it independent of this process's checkpointer.
    import psycopg
    with psycopg.connect(_dsn()) as c, c.cursor() as cur:
        cur.execute(
            "select checkpoint_id, (metadata->>'step')::int, "
            "       (checkpoint->'channel_values'->>'hops')::int, "
            "       checkpoint->'channel_values' "
            "  from checkpoints "
            " where thread_id = %s and (metadata->>'step')::int >= 0 "
            "   and (checkpoint->'channel_values'->>'hops')::int >= %s "
            " order by (metadata->>'step')::int limit 1", (thread, expected_hops))
        return cur.fetchone()


def _record(payload):
    if OBSERVED:
        OBSERVED.open("a").write(json.dumps(payload) + "\n")


def _mk(name):
    async def _node(state):
        _record({"event": "node_entry", "node": name, "pid": os.getpid()})
        if DIE == name and DIE_MODE == "post_durable":
            # Bounded ATTEMPTS, not time: the cap exists only to fail a stalled test, and it can
            # never decide that a checkpoint became durable. The database round trip is the
            # observation boundary - there is no pacing sleep.
            last = None
            for attempt in range(400):
                row = await _observe(_durable_completion, _thread_of(state), EXPECT_HOPS)
                last = row or last
                if row:
                    _record({"event": "durable_completion_observed", "node": name,
                             "attempt": attempt, "checkpoint_id": row[0], "step": row[1],
                             "hops": row[2], "channel_values": row[3], "pid": os.getpid()})
                    os._exit(9)
            raise AssertionError(
                f"no consolidated checkpoint with hops>={EXPECT_HOPS} for {_thread_of(state)}; "
                f"last observed={last}")
        if HOLD_UNTIL_FIRST_WRITE and name == "gate":
            # Case B boundary: gate has ENTERED but must not act until first's own task write is
            # committed and visible on an independent connection. The observation runs off this
            # loop so the saver's background work can finish.
            task, real_thread = _first_write_row()
            last = None
            for attempt in range(600):
                rows = await _observe(_visible_task_write, real_thread, task)
                last = rows or last
                if rows:
                    _record({"event": "first_write_visible", "task_id": task, "attempt": attempt,
                             "rows": rows, "pid": os.getpid()})
                    os._exit(9)
            raise AssertionError(
                f"first's task write {task} never became visible on {real_thread}; "
                f"last={last}")
        _record({"event": "effect_attempt", "node": name, "pid": os.getpid()})
        _effect(name, state)
        _record({"event": "effect_durable", "node": name, "pid": os.getpid()})
        if DIE == name and DIE_MODE == "pre_durable":
            # After the effect, before the return. LangGraph checkpoints a node only once it
            # returns, so nothing recording this node can be durable yet - no query needed.
            _record({"event": "killed_pre_durable", "node": name, "pid": os.getpid()})
            os._exit(9)
        return {"hops": int(state.get("hops", 0)) + 1}
    return _node


def _fake_graph(checkpointer=None):
    _record({"event": "fake_graph_called", "has_checkpointer": checkpointer is not None,
             "checkpointer_type": type(checkpointer).__name__, "pid": os.getpid()})
    g = StateGraph(_S)
    for n in ("first", "gate", "second"):
        g.add_node(n, _mk(n))
    g.add_edge(START, "first")
    g.add_edge("first", "gate")
    g.add_edge("gate", "second")
    g.add_edge("second", END)
    # the saver handed in here is the REAL fenced one; the wrapper only records
    compiled = g.compile(checkpointer=_RecordingSaver(checkpointer) if checkpointer else None)
    _record({"event": "compile_done", "pid": os.getpid()})
    _real_ainvoke = compiled.ainvoke

    async def _traced_ainvoke(*a, **kw):
        _record({"event": "graph_ainvoke_call", "pid": os.getpid()})
        try:
            out = await _real_ainvoke(*a, **kw)
        except BaseException as exc:
            _record({"event": "graph_ainvoke_raised", "exc": f"{type(exc).__name__}: {exc}"[:400],
                     "pid": os.getpid()})
            raise
        _record({"event": "graph_ainvoke_return", "keys": sorted(out or {}), "pid": os.getpid()})
        return out

    compiled.ainvoke = _traced_ainvoke
    return compiled


from langgraph.checkpoint.base import BaseCheckpointSaver


class _RecordingSaver(BaseCheckpointSaver):
    # LangGraph validates the checkpointer with isinstance(BaseCheckpointSaver), so a duck-typed
    # delegate is rejected at compile. This is a real subclass that forwards every operation to
    # the wrapped saver and overrides only aput/aput_writes to record.
    def __init__(self, inner):
        super().__init__(serde=getattr(inner, "serde", None))
        self._inner = inner

    @property
    def config_specs(self):
        return self._inner.config_specs

    def get_next_version(self, current, channel=None):
        return self._inner.get_next_version(current, channel)

    # reads and lifecycle: delegated unchanged
    def get_tuple(self, config):
        return self._inner.get_tuple(config)

    def list(self, config, **kw):
        return self._inner.list(config, **kw)

    async def aget_tuple(self, config):
        return await self._inner.aget_tuple(config)

    def alist(self, config, **kw):
        return self._inner.alist(config, **kw)

    def put(self, config, checkpoint, metadata, new_versions):
        return self._inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, *a, **kw):
        return self._inner.put_writes(config, writes, task_id, *a, **kw)

    def __getattr__(self, name):
        # only for attributes the base class does not define
        return getattr(self._inner, name)

    async def aput_writes(self, config, writes, task_id, *a, **kw):
        _record({"event": "aput_writes_call", "t": time.monotonic(), "task_id": task_id,
                 "channels": [w[0] for w in writes],
                 "checkpoint_id": (config or {}).get("configurable", {}).get("checkpoint_id"),
                 "ns": (config or {}).get("configurable", {}).get("checkpoint_ns"),
                 "thread": (config or {}).get("configurable", {}).get("thread_id"),
                 "pid": os.getpid()})
        r = await self._inner.aput_writes(config, writes, task_id, *a, **kw)
        channels = sorted(w[0] for w in writes)
        _record({"event": "aput_writes_return", "t": time.monotonic(), "task_id": task_id,
                 "channels": channels,
                 "checkpoint_id": (config or {}).get("configurable", {}).get("checkpoint_id"),
                 "ns": (config or {}).get("configurable", {}).get("checkpoint_ns"),
                 "thread": (config or {}).get("configurable", {}).get("thread_id"),
                 "pid": os.getpid()})
        # first's task write is the one carrying BOTH its hops result and the edge to gate.
        # Matching on channel name alone would also catch gate's and second's writes.
        if KILL_AFTER_FIRST_WRITE and channels == ["branch:to:gate", "hops"]:
            _record({"event": "killed_after_pending_write", "task_id": task_id,
                     "channels": channels,
                     "checkpoint_id": (config or {}).get("configurable", {}).get("checkpoint_id"),
                     "ns": (config or {}).get("configurable", {}).get("checkpoint_ns"),
                     "thread": (config or {}).get("configurable", {}).get("thread_id"),
                     "pid": os.getpid()})
            os._exit(9)
        return r

    async def aput(self, config, checkpoint, metadata, new_versions, *a, **kw):
        scalars = {k: v for k, v in (checkpoint.get("channel_values") or {}).items()
                   if isinstance(v, (int, float, str, bool, type(None)))}
        _record({"event": "aput_call", "t": time.monotonic(),
                 "checkpoint_id": checkpoint.get("id"), "step": (metadata or {}).get("step"),
                 "source": (metadata or {}).get("source"),
                 "thread": (config or {}).get("configurable", {}).get("thread_id"),
                 "ns": (config or {}).get("configurable", {}).get("checkpoint_ns"),
                 "channel_values": scalars, "updated": sorted(new_versions or {}),
                 "pid": os.getpid()})
        out = await self._inner.aput(config, checkpoint, metadata, new_versions, *a, **kw)
        _record({"event": "aput_return", "t": time.monotonic(),
                 "checkpoint_id": checkpoint.get("id"), "step": (metadata or {}).get("step"),
                 "returned": (out or {}).get("configurable", {}).get("checkpoint_id"),
                 "pid": os.getpid()})
        vals = checkpoint.get("channel_values") or {}
        if KILL_AFTER_FINAL and (
                (vals.get("hops") == 1 and "branch:to:gate" in vals) if KILL_TARGET_STEP1
                else (vals.get("hops") == 3
                      and not any(k.startswith("branch:to:") for k in vals))):
            _record({"event": "killed_after_final_aput", "checkpoint_id": checkpoint.get("id"),
                     "returned": (out or {}).get("configurable", {}).get("checkpoint_id"),
                     "step": (metadata or {}).get("step"), "channel_values": scalars,
                     "updated": sorted(new_versions or {}),
                     "thread": (config or {}).get("configurable", {}).get("thread_id"),
                     "ns": (config or {}).get("configurable", {}).get("checkpoint_ns"),
                     "pid": os.getpid()})
            os._exit(9)
        # Window C: the EXACT step-1 consolidation - the one carrying first's result and gate
        # pending. Identified by step and by branch:to:gate in both updated and channel_values,
        # never by a hops threshold, which would also match steps 2 and 3.
        if KILL_AFTER_STEP1 and (metadata or {}).get("step") == 1 \
                and "branch:to:gate" in (new_versions or {}) \
                and "branch:to:gate" in (checkpoint.get("channel_values") or {}):
            _record({"event": "killed_after_aput", "checkpoint_id": checkpoint.get("id"),
                     "returned": (out or {}).get("configurable", {}).get("checkpoint_id"),
                     "step": 1, "channel_values": scalars, "updated": sorted(new_versions or {}),
                     "pid": os.getpid()})
            os._exit(9)
        return out


graph_module.build_graph = _fake_graph

from meshpipeline.application.pipeline_run import run_pipeline

print("RESULT " + json.dumps({"pid": os.getpid(), "result": run_pipeline(**json.loads(kwargs_json))}))
"""


async def _seed(marker: str, tmp_path, *, size: int = 6000,
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
           die_mode="", expect_hops=1, observed=None, kill_after_step1=False,
           kill_after_first_write=False, hold_until_first_write=False,
           kill_after_final=False,
           lease_seconds="900", expect_clean_exit=False):
    workspace.mkdir(parents=True, exist_ok=True)
    kwargs = {"job_id": job_id, "owner_id": _OWNER, "geometry_source": geom.ref.to_payload(),
              "geometry_interpretation": geom.interpretation_snapshot.to_payload(),
              "request_txt": "mesh it", "mesh_engine": "cfmesh"}
    env = {**os.environ, "PIPELINE_EXECUTION_ID": execution_id, "LEDGER": str(ledger),
           "DATA_COLLECTION_ENABLED": "true",
           "DIE_AT": die_at, "DIE_MODE": die_mode, "EXPECT_HOPS": str(expect_hops),
           **({"KILL_AFTER_STEP1": "1"} if kill_after_step1 else {}),
           **({"KILL_AFTER_FIRST_WRITE": "1"} if kill_after_first_write else {}),
           **({"HOLD_UNTIL_FIRST_WRITE": "1"} if hold_until_first_write else {}),
           **({"KILL_AFTER_FINAL": "1"} if kill_after_final else {}),
           "WORKSPACE_BASE": str(workspace),
           "WORKER_LEASE_SECONDS": lease_seconds,
           "WORKER_HEARTBEAT_SECONDS": str(max(1, int(lease_seconds) // 2))}
    if observed is not None:
        env["OBSERVED"] = str(observed)
    proc = subprocess.run([sys.executable, "-c", _CHILD, job_id, str(workspace),
                           json.dumps(kwargs)],
                          cwd=workspace, capture_output=True, text=True, timeout=300, env=env)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
    out = json.loads(lines[-1][len("RESULT "):]) if lines else {}
    if expect_clean_exit:
        # A child that dies without a RESULT must never look like a pass. Crash tests opt out and
        # assert their own exit code instead.
        assert proc.returncode == 0 and len(lines) == 1 and out.get("result"), _child_failure(proc, lines)
    return out, proc


_DSN_CREDS = re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://)[^@/\s]*@")


def _redact(text: str) -> str:
    return _DSN_CREDS.sub(r"\1<redacted>@", text or "")


def _child_failure(proc, lines) -> str:
    return ("the child did not produce one usable RESULT\n"
            f"  exit code : {proc.returncode}\n"
            f"  RESULT lines: {len(lines)}\n"
            f"  stdout    : {_redact(proc.stdout)[-2000:]}\n"
            f"  stderr    : {_redact(proc.stderr)[-4000:]}")


def _ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


async def _await_lease_expiry(job_id, timeout_s=30.0):
    import asyncio

    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        async with get_db() as db:
            expired = (await db.execute(text(
                "select lease_expires_at <= now() from simulation_jobs where id = :j"),
                {"j": uuid.UUID(job_id)})).scalar()
        await dispose_engine()
        if expired:
            return
        await asyncio.sleep(0.2)
    raise AssertionError("the execution lease never expired")


# THE delivery guarantee, split into the two windows it always contained. One test could not
# hold both: its trigger fired on any checkpoint existing, including the input checkpoint written
# before the first node runs, so the same run landed in either window depending on load.


def _observations(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _pending_writes(thread: str) -> list[tuple]:
    import psycopg

    import meshpipeline.settings.providers as pc
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select checkpoint_id, checkpoint_ns, task_id, channel, idx "
                    "from checkpoint_writes where thread_id = %s order by task_id, idx",
                    (thread,))
        return cur.fetchall()


def _backlog(job_id: str) -> list[dict]:
    from meshpipeline.adapters.event_stream.redis import log_key_for, sync_client
    return [json.loads(b) for b in sync_client().lrange(log_key_for(job_id), 0, -1)]


def _notes(job_id: str, text: str) -> list[dict]:
    # by TYPED identity - never by deleting or hiding the run's other lifecycle events
    return [e for e in _backlog(job_id)
            if e.get("type") == "note" and e.get("text") == text]


async def _is_terminal(job_id: str) -> bool:
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        status = (await db.execute(text(
            "select status::text from simulation_jobs where id = :j"),
            {"j": uuid.UUID(job_id)})).scalar()
    await dispose_engine()
    return str(status) in ("succeeded", "failed", "cancelled")


async def _claimed_generation(job_id: str) -> int:
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db
    async with get_db() as db:
        gen = (await db.execute(text(
            "select execution_generation from simulation_jobs where id = :j"),
            {"j": uuid.UUID(job_id)})).scalar()
    await dispose_engine()
    return int(gen or 0)


def _thread_id(job_id: str, generation: int) -> str:
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as V
    return f"{job_id}:s{V}:g{generation}"


def _capture_rows(job_id: str, op_key_prefix: str = "") -> list[tuple]:
    import psycopg

    import meshpipeline.settings.providers as pc
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select op_key, name, execution_generation from capture_operations "
                    "where job_id = %s order by seq", (uuid.UUID(job_id),))
        return [r for r in cur.fetchall() if not op_key_prefix or r[1].startswith(op_key_prefix)]


def _durable_rows(job_id: str, generation: int = 0):
    import psycopg

    import meshpipeline.settings.providers as pc
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as V
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        cur.execute("select checkpoint_id, (metadata->>'step')::int, "
                    "       (checkpoint->'channel_values'->>'hops')::int "
                    "  from checkpoints where thread_id = %s order by 2",
                    (f"{job_id}:s{V}:g{generation}",))
        return cur.fetchall()


async def test_pre_durable_crash_replays_function_but_deduplicates_capture_operation(tmp_path):
    # Killed inside `first`, after its effect and before it returns: no aput_writes for `first`
    # can exist, so the function must run again. Each effect is counted at its own authority -
    # the ledger counts invocations, capture_operations owns durable idempotency.
    job_id, geom = await _seed("pre-durable", tmp_path)
    ledger, observed = tmp_path / "ledger.jsonl", tmp_path / "observed.jsonl"
    exec_id = f"exec-{uuid.uuid4()}"

    ws_a = tmp_path / "ws-a"
    _, proc_a = _child(job_id, geom, ws_a, ledger, execution_id=exec_id, die_at="first",
                       die_mode="pre_durable", observed=observed, lease_seconds="4")
    assert proc_a.returncode in (9, -9), (proc_a.returncode, _redact(proc_a.stderr)[-800:])

    obs_a = _observations(observed)
    assert [o["event"] for o in obs_a if o.get("node") == "first"] == [
        "node_entry", "effect_attempt", "capture_attempt", "publish_attempt",
        "effect_durable", "killed_pre_durable"], obs_a
    gen = await _claimed_generation(job_id)
    thread = _thread_id(job_id, gen)
    assert [r for r in _durable_rows(job_id, gen) if (r[2] or 0) >= 1] == [], \
        f"a consolidated checkpoint carrying the completed node was durable on {thread}"

    shutil.rmtree(ws_a)
    await _await_lease_expiry(job_id)

    ws_b = tmp_path / "ws-b"
    _child(job_id, geom, ws_b, ledger, execution_id=exec_id, observed=observed)

    obs = _observations(observed)
    def _count(event, node="first"):
        return len([o for o in obs if o["event"] == event and o.get("node") == node])
    nodes = [e["node"] for e in _ledger(ledger)]
    captures = [r for r in _capture_rows(job_id) if r[1] == "first_completed"]

    # the FUNCTION replays; each durable authority is measured separately
    assert nodes == ["first", "first", "gate", "second"], nodes
    assert _count("node_entry") == 2, f"the interrupted node did not replay: {_count('node_entry')}"
    assert _count("capture_attempt") == 2, f"capture attempts: {_count('capture_attempt')}"
    # Publication attempts are RECORDED but their durable/user-visible outcome is not asserted
    # here - measuring JobPublisher.note's replay behaviour is still owed.
    assert _count("publish_attempt") == 2, f"publish attempts: {_count('publish_attempt')}"
    assert len(captures) == 1, (
        f"the real capture authority stored {len(captures)} rows for one logical operation: "
        f"{captures}")
    assert captures[0][0], "the stored operation carries no op_key"

    # the USER-VISIBLE effect: two attempts, one durable entry in the replayable backlog
    visible = _notes(job_id, "first completed")
    assert len(visible) == 1, (
        f"the replay published {len(visible)} user-visible 'first completed' events: "
        f"{[(e.get('seq'), e.get('type')) for e in visible]}")
    assert len({e["seq"] for e in visible}) == 1, visible
    assert len({o["pid"] for o in obs if o["event"] == "node_entry" and o["node"] == "first"}) == 2


async def test_durable_pending_write_resumes_after_completed_node(tmp_path):
    # Case B: first is durably complete (its exact task write is visible on an independent
    # connection) while gate has entered but not acted. Killing there proves what a durable
    # pending write does and does not protect.
    job_id, geom = await _seed("pending-write", tmp_path)
    ledger, observed = tmp_path / "ledger.jsonl", tmp_path / "observed.jsonl"
    exec_id = f"exec-{uuid.uuid4()}"

    ws_a = tmp_path / "ws-a"
    _, proc_a = _child(job_id, geom, ws_a, ledger, execution_id=exec_id,
                       hold_until_first_write=True, observed=observed, lease_seconds="4")
    assert proc_a.returncode in (9, -9), (proc_a.returncode, _redact(proc_a.stderr)[-1500:])

    obs_a = _observations(observed)
    seen = [o for o in obs_a if o["event"] == "first_write_visible"]
    assert len(seen) == 1, f"first's write never became visible while gate was held: {obs_a[-3:]}"
    target = seen[0]
    assert {r[2] for r in target["rows"]} >= {"hops", "branch:to:gate"}, target

    def _count(event, node):
        return len([o for o in obs_a if o["event"] == event and o.get("node") == node])
    assert _count("node_entry", "first") == 1 and _count("effect_attempt", "first") == 1
    assert _count("capture_attempt", "first") == 1 and _count("publish_attempt", "first") == 1
    assert _count("node_entry", "gate") == 1, "gate did not enter"
    assert _count("effect_attempt", "gate") == 0, "gate acted before the kill"
    assert _count("node_entry", "second") == 0, "second entered before the kill"
    assert [e["node"] for e in _ledger(ledger)] == ["first"], _ledger(ledger)

    gen = await _claimed_generation(job_id)
    thread = _thread_id(job_id, gen)
    # the parent re-verifies the SAME rows independently, after process A is gone
    again = [w for w in _pending_writes(thread) if w[2] == target["task_id"]]
    assert {w[3] for w in again} >= {"hops", "branch:to:gate"}, again

    shutil.rmtree(ws_a)
    await _await_lease_expiry(job_id)

    ws_b = tmp_path / "ws-b"
    _child(job_id, geom, ws_b, ledger, execution_id=exec_id, observed=observed)

    obs = _observations(observed)
    pids_a = {o["pid"] for o in obs_a}
    entries = [o["node"] for o in obs if o["event"] == "node_entry"]
    resumed = [o["node"] for o in obs if o["event"] == "node_entry" and o["pid"] not in pids_a]
    nodes = [e["node"] for e in _ledger(ledger)]

    assert entries == ["first", "gate", "gate", "second"], entries
    assert "first" not in resumed, f"a durably complete node re-entered: {resumed}"
    assert resumed == ["gate", "second"], resumed
    assert nodes == ["first", "gate", "second"], nodes

    for node in ("first", "gate", "second"):
        rows = [r for r in _capture_rows(job_id) if r[1] == f"{node}_completed"]
        assert len(rows) == 1, f"{node} produced {len(rows)} durable capture rows: {rows}"
        visible = _notes(job_id, f"{node} completed")
        assert len(visible) == 1, f"{node} produced {len(visible)} user-visible events"
    final = _durable_rows(job_id, gen)
    assert max((r[2] or 0) for r in final) == 3, f"final channel state is wrong: {final}"
    for entry in _ledger(ledger)[1:]:
        assert str(ws_b) in entry["path"] and str(ws_a) not in entry["path"]


async def test_completed_graph_checkpoint_terminalizes_without_node_replay(tmp_path):
    # The final saver return proves the whole graph's state is durable. Killing inside the
    # wrapper means aput never returns to ainvoke, so the terminal authority cannot have run.
    job_id, geom = await _seed("completed-graph", tmp_path)
    ledger, observed = tmp_path / "ledger.jsonl", tmp_path / "observed.jsonl"
    exec_id = f"exec-{uuid.uuid4()}"

    ws_a = tmp_path / "ws-a"
    _, proc_a = _child(job_id, geom, ws_a, ledger, execution_id=exec_id,
                       kill_after_final=True, observed=observed, lease_seconds="4")
    assert proc_a.returncode in (9, -9), (proc_a.returncode, _redact(proc_a.stderr)[-1200:])

    obs_a = _observations(observed)
    killed = [o for o in obs_a if o["event"] == "killed_after_final_aput"]
    assert len(killed) == 1, f"the completed-graph checkpoint was never reached: {killed}"
    target = killed[0]
    assert target["channel_values"].get("hops") == 3, target
    assert not [k for k in target["channel_values"] if k.startswith("branch:to:")], target
    assert target["returned"] == target["checkpoint_id"], target

    assert [e["node"] for e in _ledger(ledger)] == ["first", "gate", "second"], _ledger(ledger)
    gen = await _claimed_generation(job_id)
    thread = _thread_id(job_id, gen)
    assert target["thread"] == thread, (target["thread"], thread)

    rows = _durable_rows(job_id, gen)
    assert target["checkpoint_id"] in [r[0] for r in rows], rows
    assert max((r[2] or 0) for r in rows) == 3, rows
    assert not await _is_terminal(job_id), "the job terminalized before the crash"

    shutil.rmtree(ws_a)
    await _await_lease_expiry(job_id)

    ws_b = tmp_path / "ws-b"
    out_b, _ = _child(job_id, geom, ws_b, ledger, execution_id=exec_id, observed=observed)

    obs = _observations(observed)
    pids_a = {o["pid"] for o in obs_a}
    resumed = [o["node"] for o in obs if o["event"] == "node_entry" and o["pid"] not in pids_a]
    nodes = [e["node"] for e in _ledger(ledger)]

    assert resumed == [], f"a completed graph re-executed nodes: {resumed}"
    assert nodes == ["first", "gate", "second"], nodes
    for node in ("first", "gate", "second"):
        assert len([r for r in _capture_rows(job_id) if r[1] == f"{node}_completed"]) == 1
        assert len(_notes(job_id, f"{node} completed")) == 1
    assert max((r[2] or 0) for r in _durable_rows(job_id, gen)) == 3

    # terminalized ONCE, from the saved state - the expected class for this graph, not a
    # checkpoint failure
    result = out_b.get("result") or {}
    assert result.get("status") == "failed", result
    assert result.get("reason") != "checkpoint_unreadable", result
    assert await _is_terminal(job_id), "the completed graph never terminalized"


async def test_delayed_step_one_consolidation_is_written_after_every_node_ran(tmp_path):
    # Consolidation is DEFERRED in this LangGraph version: by the time the step-1 checkpoint is
    # written, every node function has already run. This characterises what a crash at that
    # moment actually leaves behind - it is not a mid-graph checkpoint window.
    job_id, geom = await _seed("delayed-consolidation", tmp_path)
    ledger, observed = tmp_path / "ledger.jsonl", tmp_path / "observed.jsonl"
    exec_id = f"exec-{uuid.uuid4()}"

    ws_a = tmp_path / "ws-a"
    _, proc_a = _child(job_id, geom, ws_a, ledger, execution_id=exec_id,
                       kill_after_step1=True, observed=observed, lease_seconds="4")
    assert proc_a.returncode in (9, -9), (proc_a.returncode, _redact(proc_a.stderr)[-800:])

    obs_a = _observations(observed)
    killed = [o for o in obs_a if o["event"] == "killed_after_aput"]
    assert len(killed) == 1, f"the step-1 consolidation was never reached: {killed}"
    target = killed[0]
    assert target["step"] == 1 and "branch:to:gate" in target["updated"], target
    assert target["returned"] == target["checkpoint_id"], target

    # every node function had already run before that consolidation was written
    assert [e["node"] for e in _ledger(ledger)] == ["first", "gate", "second"], _ledger(ledger)

    gen = await _claimed_generation(job_id)
    thread = _thread_id(job_id, gen)
    rows = _durable_rows(job_id, gen)
    ids = [r[0] for r in rows]
    assert target["checkpoint_id"] in ids, (
        f"the returned checkpoint {target['checkpoint_id']} is not visible on {thread}: {rows}")
    # later consolidations are NOT assumed durable - the kill happened before them
    assert max((r[2] or 0) for r in rows) >= 1

    shutil.rmtree(ws_a)
    await _await_lease_expiry(job_id)

    ws_b = tmp_path / "ws-b"
    _child(job_id, geom, ws_b, ledger, execution_id=exec_id, observed=observed)

    obs = _observations(observed)
    pids_a = {o["pid"] for o in obs_a}
    resumed = [o["node"] for o in obs
               if o["event"] == "node_entry" and o["pid"] not in pids_a]
    nodes = [e["node"] for e in _ledger(ledger)]
    captures = {r[1] for r in _capture_rows(job_id)}

    # the OBSERVED behaviour, recorded rather than asserted from expectation
    print(f"DELAYED-CONSOLIDATION thread={thread} killed_at={target['checkpoint_id']} "
          f"resumed_nodes={resumed} ledger={nodes} captures={sorted(captures)}")
    assert nodes[:3] == ["first", "gate", "second"], nodes
    # whatever process B re-ran, each logical capture stayed single through its op_key
    for name in ("first_completed", "gate_completed", "second_completed"):
        same = [r for r in _capture_rows(job_id) if r[1] == name]
        assert len(same) <= 1, f"{name} produced {len(same)} durable rows: {same}"


async def test_an_interrupted_node_is_at_least_once_and_is_fenced(tmp_path):
    from meshpipeline.pipeline import graph as graph_mod
    src = Path(graph_mod.__file__).read_text()
    # every node passes through the fence before its body executes
    assert 'assert_current_owner(f"graph node {name}")' in src


# events

async def test_run_events_stay_ordered_and_are_not_republished(tmp_path):
    from meshpipeline.adapters._shared.redis_client import sync_client
    from meshpipeline.adapters.event_stream.redis import log_key_for

    job_id, geom = await _seed("events", tmp_path)
    ledger = tmp_path / "ledger.jsonl"
    exec_id = f"exec-{uuid.uuid4()}"

    _child(job_id, geom, tmp_path / "ws-a", ledger, execution_id=exec_id, die_at="gate",
           lease_seconds="4")
    raw_after_a = sync_client().lrange(log_key_for(job_id), 0, -1)
    first_events = [e for e in raw_after_a if "first completed" in str(e)]
    assert len(first_events) == 1

    await _await_lease_expiry(job_id)
    _child(job_id, geom, tmp_path / "ws-b", ledger, execution_id=exec_id)

    raw = [str(e) for e in sync_client().lrange(log_key_for(job_id), 0, -1)]
    assert len([e for e in raw if "first completed" in e]) == 1, \
        "a completed node's event was republished by the restart"
    assert len([e for e in raw if "second completed" in e]) == 1

    seqs = [json.loads(e).get("seq") for e in raw if json.loads(e).get("seq") is not None]
    assert seqs == sorted(seqs), "event sequence went backwards across the restart"
    assert len(seqs) == len(set(seqs)), "a sequence number was reused"

    blob = " ".join(raw)
    for leak in (geom.ref.object_key, geom.ref.sha256, geom.ref.source_id, "sources/", "cont-data",
                 "contadminsecret", str(tmp_path)):
        assert leak not in blob, "the run event stream exposed storage detail"


# ownership

async def test_a_terminal_job_does_nothing_on_redelivery(tmp_path):
    job_id, geom = await _seed("terminal", tmp_path)
    ledger = tmp_path / "ledger.jsonl"

    first, _ = _child(job_id, geom, tmp_path / "ws-1", ledger, execution_id=f"e-{uuid.uuid4()}")
    assert [e["node"] for e in _ledger(ledger)] == ["first", "gate", "second"]
    before = len(_ledger(ledger))

    again, _ = _child(job_id, geom, tmp_path / "ws-2", ledger, execution_id=f"e-{uuid.uuid4()}")
    assert again["result"].get("skipped") == "already_terminal", again["result"]
    assert len(_ledger(ledger)) == before, "a terminal job ran its graph again"


async def test_no_heartbeat_task_survives_the_run(tmp_path):
    from sqlalchemy import text

    from meshpipeline.persistence.session import dispose_engine, get_db

    job_id, geom = await _seed("heartbeat", tmp_path)
    _child(job_id, geom, tmp_path / "ws", tmp_path / "ledger.jsonl",
           execution_id=f"e-{uuid.uuid4()}", lease_seconds="4")

    # the process exited; its lease must simply lapse rather than being refreshed forever
    await _await_lease_expiry(job_id)
    async with get_db() as db:
        row = (await db.execute(text(
            "select status::text, lease_expires_at <= now() from simulation_jobs where id = :j"),
            {"j": uuid.UUID(job_id)})).first()
    await dispose_engine()
    assert row[1] is True, "the lease was still being heartbeaten after the process exited"
