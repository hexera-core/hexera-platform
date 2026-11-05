# The crash handler used to read the checkpoint through the run's own graph binding - whose
# saver the crash itself had already closed (the async-with unwound first), so EVERY durable-
# path crash reported approved intent only. The ladder: live binding first (the MemorySaver
# dev path's only source), then a fresh saver against PostgreSQL, then approved intent.
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from meshpipeline.application.terminal_finalize import durable_facts_after_crash


class _Log:
    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)


@asynccontextmanager
async def _broken_session():
    raise RuntimeError("no db in this test")
    yield  # pragma: no cover


def _snap(values):
    return SimpleNamespace(values=values)


async def test_memory_saver_crash_facts_still_read(monkeypatch):
    # Rung 1: a live (MemorySaver) binding is the source; the fresh-saver rung must not run.
    import langgraph.checkpoint.postgres.aio as _aio

    def _must_not_run(dsn):  # noqa: ARG001
        raise AssertionError("fresh saver must not be opened when the live binding works")

    monkeypatch.setattr(_aio.AsyncPostgresSaver, "from_conn_string", _must_not_run)
    graph = SimpleNamespace(aget_state=AsyncMock(return_value=_snap(
        {"engine": "gmsh", "purpose": "external_cfd", "retry_count": 1})))
    facts = await durable_facts_after_crash(
        _broken_session, job_id="00000000-0000-0000-0000-000000000001", approved={},
        graph=graph, graph_config={"configurable": {"thread_id": "t"}},
        job_repo=SimpleNamespace(), jlog=_Log(), used_durable_checkpointer=False)
    assert facts["engine"] == "gmsh" and facts["attempts"] == 1


async def test_dead_graph_falls_through_to_durable_read(monkeypatch):
    # Rung 2: the run's binding fails exactly like a crash-closed saver; the fresh saver
    # reads the same thread from PostgreSQL and its facts win over approved intent.
    import langgraph.checkpoint.postgres.aio as _aio

    import meshpipeline.pipeline.graph as _graphmod

    @asynccontextmanager
    async def _fresh(dsn):  # noqa: ARG001
        assert "+asyncpg" not in dsn and "+psycopg" not in dsn
        yield "cp"

    monkeypatch.setattr(_aio.AsyncPostgresSaver, "from_conn_string", staticmethod(_fresh))
    monkeypatch.setattr(_graphmod, "build_graph", lambda checkpointer: SimpleNamespace(
        aget_state=AsyncMock(return_value=_snap(
            {"engine": "snappy", "purpose": "external_cfd", "retry_count": 2}))))

    dead = SimpleNamespace(aget_state=AsyncMock(side_effect=RuntimeError(
        "the connection is closed")))
    log = _Log()
    facts = await durable_facts_after_crash(
        _broken_session, job_id="00000000-0000-0000-0000-000000000002",
        approved={"mesh_engine": "cfmesh"},
        graph=dead, graph_config={"configurable": {"thread_id": "t"}},
        job_repo=SimpleNamespace(), jlog=log, used_durable_checkpointer=True)
    assert facts["engine"] == "snappy" and facts["attempts"] == 2
    assert any("trying a fresh saver" in w for w in log.warnings)


async def test_both_rungs_dead_reports_approved_intent(monkeypatch):
    import langgraph.checkpoint.postgres.aio as _aio

    def _also_dead(dsn):  # noqa: ARG001
        raise RuntimeError("postgres is down too")

    monkeypatch.setattr(_aio.AsyncPostgresSaver, "from_conn_string", staticmethod(_also_dead))
    dead = SimpleNamespace(aget_state=AsyncMock(side_effect=RuntimeError(
        "the connection is closed")))
    log = _Log()
    facts = await durable_facts_after_crash(
        _broken_session, job_id="00000000-0000-0000-0000-000000000003",
        approved={"mesh_engine": "cfmesh", "purpose": "internal_cfd"},
        graph=dead, graph_config={"configurable": {"thread_id": "t"}},
        job_repo=SimpleNamespace(), jlog=log, used_durable_checkpointer=True)
    assert facts["engine"] == "cfmesh"       # the intent fallback, exactly as before
    assert any("reporting only" in w for w in log.warnings)
