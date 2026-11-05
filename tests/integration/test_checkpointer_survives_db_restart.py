# Responsibility: Verify a run whose checkpointer connections are KILLED mid-run (what a Cloud SQL
# restart does to a 90-minute job) still finishes, with its checkpoints written - the checked pool
# replaces the dead connections and the fenced saver retries the write that met one.
# Boundaries: real PostgreSQL and the real LangGraph saver; a two-node graph stands in for the
# pipeline (kart_chassis, dev-areen 25 Oct: a reviewed, valid mesh was lost to "the connection is
# closed" on the checkpoint write after the reviewer's PASS).
from __future__ import annotations

import asyncio
import uuid
from typing import TypedDict

import pytest

import meshpipeline.settings.providers as provcfg

pytestmark = pytest.mark.asyncio


class _S(TypedDict, total=False):
    n: int
    verdict: str


def _kill_checkpointer_connections(dsn: str, *, wait_s: float = 10.0) -> int:
    """Terminate every server connection the checkpointer pool holds, from a connection of our own.
    The pool refills in the background after a kill, so this first waits (briefly) until it holds one."""
    import time

    import psycopg

    from meshpipeline.application.checkpoint_pool import APPLICATION_NAME
    deadline = time.monotonic() + wait_s
    with psycopg.connect(dsn, autocommit=True) as c:
        while True:
            rows = c.execute("select pg_terminate_backend(pid) from pg_stat_activity "
                             "where application_name = %s and pid <> pg_backend_pid()",
                             (APPLICATION_NAME,)).fetchall()
            if rows or time.monotonic() > deadline:
                return len(rows)
            time.sleep(0.1)


async def run_with_killed_checkpointer(dsn: str) -> dict:
    """Run the graph, killing the checkpointer's connections inside the first node AND the last one
    (after which the final checkpoint write must still land). Returns the facts the test asserts."""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.graph import END, START, StateGraph

    from meshpipeline.application.checkpoint_pool import checkpoint_pool
    from meshpipeline.application.fenced_checkpointer import FencedCheckpointer

    killed: list[int] = []

    # off the event loop: the pool reconnects on the loop, and a blocking wait here would starve it
    async def first(state):
        killed.append(await asyncio.to_thread(_kill_checkpointer_connections, dsn))
        return {"n": int(state.get("n", 0)) + 1}

    async def review(state):
        # the mesh is built and reviewed: the write that follows is the one kart_chassis lost
        killed.append(await asyncio.to_thread(_kill_checkpointer_connections, dsn))
        return {"n": int(state.get("n", 0)) + 1, "verdict": "PASS"}

    thread = f"{uuid.uuid4()}:s1:g1"
    config = {"configurable": {"thread_id": thread}}
    async with checkpoint_pool(dsn) as pool:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        g = StateGraph(_S)
        g.add_node("first", first)
        g.add_node("review", review)
        g.add_edge(START, "first")
        g.add_edge("first", "review")
        g.add_edge("review", END)
        graph = g.compile(checkpointer=FencedCheckpointer(saver, job_id=thread.split(":")[0],
                                                          execution_generation=1))
        out = await graph.ainvoke({"n": 0}, config=config)
        snap = await graph.aget_state(config)
    return {"out": dict(out), "killed": killed, "saved": dict(snap.values or {}), "next": tuple(snap.next)}


async def test_a_run_whose_checkpointer_connections_die_still_finishes_and_saves():
    dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    facts = await run_with_killed_checkpointer(dsn)
    assert all(k >= 1 for k in facts["killed"]), f"nothing was killed: {facts['killed']}"
    # delivered: the run finished with its reviewed result in hand
    assert facts["out"] == {"n": 2, "verdict": "PASS"}
    # and durably: the final checkpoint, written after the second kill, is the finished state
    assert facts["saved"] == {"n": 2, "verdict": "PASS"} and facts["next"] == ()
