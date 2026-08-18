# Responsibility: Verify a crashed thread keeps its position, and replacing geometry does not move it.
from __future__ import annotations

import os
import uuid
from typing import TypedDict

import pytest
from tests import harness_provisioning as hp

import meshpipeline.settings.providers as provcfg

pytestmark = pytest.mark.asyncio

if not os.getenv("DATABASE_URL"):
    pytest.skip("a real PostgreSQL endpoint is required", allow_module_level=True)

pytest.importorskip("langgraph.checkpoint.postgres.aio")


class _S(TypedDict, total=False):
    geometry: dict
    trail: list


def _graph(ran: list, fail_at: str | None):
    from langgraph.graph import END, START, StateGraph

    def _mk(name):
        async def _node(state):
            ran.append(name)
            if fail_at == name:
                raise RuntimeError(f"controlled crash in {name}")
            return {"trail": (state.get("trail") or []) + [name]}
        return _node

    g = StateGraph(_S)
    for n in ("a", "b", "c"):
        g.add_node(n, _mk(n))
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", END)
    return g


async def _saver():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    import meshpipeline.settings.providers as pc
    dsn = pc.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    return AsyncPostgresSaver.from_conn_string(dsn)


@pytest.fixture()
def cfg():
    return {"configurable": {"thread_id": f"semantics-{uuid.uuid4()}"}}


# what is stored

async def test_a_crash_leaves_position_values_and_the_interrupted_task(cfg):
    ran: list = []
    async with await _saver() as cp:
        await cp.setup()
        graph = _graph(ran, fail_at="b").compile(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke({"geometry": {"local_path": "/ws-a/f"}, "trail": []}, config=cfg)
        st = await graph.aget_state(cfg)

    assert st.next == ("b",)                       # pending POSITION survived
    assert st.values["trail"] == ["a"]             # completed work survived
    assert [t.name for t in st.tasks] == ["b"]     # the interrupted task is recorded
    assert any(t.error for t in st.tasks)


# how you continue

async def test_a_complete_input_replays_completed_nodes(cfg):
    ran: list = []
    async with await _saver() as cp:
        await cp.setup()
        graph = _graph(ran, fail_at="b").compile(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke({"geometry": {"local_path": "/ws-a/f"}, "trail": []}, config=cfg)

        ran.clear()
        graph = _graph(ran, fail_at=None).compile(checkpointer=cp)
        out = await graph.ainvoke({"geometry": {"local_path": "/ws-b/f"}, "trail": ["a"]},
                                  config=cfg)

    assert ran == ["a", "b", "c"], ran             # 'a' ran a SECOND time
    assert out["trail"] == ["a", "a", "b", "c"]    # visible as duplicated work


async def test_none_continues_from_the_saved_position(cfg):
    ran: list = []
    async with await _saver() as cp:
        await cp.setup()
        graph = _graph(ran, fail_at="b").compile(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke({"geometry": {"local_path": "/ws-a/f"}, "trail": []}, config=cfg)

        ran.clear()
        graph = _graph(ran, fail_at=None).compile(checkpointer=cp)
        out = await graph.ainvoke(None, config=cfg)

    assert ran == ["b", "c"], ran                  # 'a' did NOT replay
    assert out["trail"] == ["a", "b", "c"]         # each node's work appears exactly once


# substitution is positionless

async def test_updating_geometry_replaces_the_handle_without_moving_position(cfg):
    ran: list = []
    async with await _saver() as cp:
        await cp.setup()
        graph = _graph(ran, fail_at="b").compile(checkpointer=cp)
        with pytest.raises(RuntimeError):
            await graph.ainvoke({"geometry": {"local_path": "/ws-a/f"}, "trail": []}, config=cfg)

        before = await graph.aget_state(cfg)
        await graph.aupdate_state(cfg, {"geometry": {"local_path": "/ws-b/f"}})
        after = await graph.aget_state(cfg)

        ran.clear()
        graph = _graph(ran, fail_at=None).compile(checkpointer=cp)
        out = await graph.ainvoke(None, config=cfg)

    assert before.values["geometry"]["local_path"] == "/ws-a/f"
    assert after.values["geometry"]["local_path"] == "/ws-b/f"   # handle replaced
    assert after.next == before.next == ("b",)                   # position untouched
    assert [t.name for t in after.tasks] == ["b"]                # nothing became pending again
    assert after.values["trail"] == ["a"]                        # no completed work resurrected
    assert ran == ["b", "c"]                                     # and it still continues
    assert out["geometry"]["local_path"] == "/ws-b/f"            # the graph used the NEW path


async def test_a_completed_thread_has_nothing_pending(cfg):
    ran: list = []
    async with await _saver() as cp:
        await cp.setup()
        graph = _graph(ran, fail_at=None).compile(checkpointer=cp)
        await graph.ainvoke({"geometry": {"local_path": "/ws/f"}, "trail": []}, config=cfg)
        st = await graph.aget_state(cfg)

    assert st.next == ()          # a finished thread is not resumable, so a later entry is fresh
    assert ran == ["a", "b", "c"]


async def test_an_unused_thread_has_no_state(cfg):
    async with await _saver() as cp:
        await cp.setup()
        graph = _graph([], fail_at=None).compile(checkpointer=cp)
        st = await graph.aget_state(cfg)
    assert not st.next


async def test_classification_creates_its_own_tables_on_a_fresh_install(cfg):

    from meshpipeline.application.pipeline_run import _classify_checkpoint
    from meshpipeline.persistence.session import dispose_engine

    await hp.drop_tables(provcfg.POSTGRES_DSN, "checkpoints", "checkpoint_blobs",
                         "checkpoint_writes", "checkpoint_migrations")
    await dispose_engine()

    assert await _classify_checkpoint(cfg["configurable"]["thread_id"]) == "absent"
