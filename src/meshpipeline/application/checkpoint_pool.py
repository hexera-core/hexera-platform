# Responsibility: Give the durable checkpointer a small connection pool that checks every
# connection before handing it out, so a long job survives a database restart.
# Boundaries: builds and closes the pool; the saver itself is LangGraph's AsyncPostgresSaver.
from __future__ import annotations

from contextlib import asynccontextmanager

#: Connections the checkpointer keeps: one is enough for a run (its calls are sequential); a
#: second covers a background write overlapping a read. Small on purpose - the dev database is a
#: db-g1-small shared by three environments (dev-areen 25 Oct: its slots filled during a redeploy).
POOL_MIN, POOL_MAX = 1, 2
#: How long opening the pool may wait for its first connection before the run is told it has none.
OPEN_TIMEOUT_S = 30.0
#: How its connections are named on the server (pg_stat_activity), so an operator can find them.
APPLICATION_NAME = "hexera-checkpointer"


@asynccontextmanager
async def checkpoint_pool(dsn: str):
    """An open AsyncConnectionPool for AsyncPostgresSaver: the same connection settings LangGraph's
    from_conn_string uses (autocommit, no server-side prepares, dict rows), plus a CHECK on every
    borrow - a connection a restart killed is replaced, not handed to the next checkpoint write."""
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    # ONE PLAIN CONNECT FIRST, as from_conn_string made: a database that is not there fails here at
    # once (the run then decides: refuse, or fall back to in-process checkpoints where allowed),
    # instead of the pool retrying in the background for OPEN_TIMEOUT_S
    probe = await AsyncConnection.connect(dsn, autocommit=True)
    await probe.close()
    pool = AsyncConnectionPool(
        conninfo=dsn, min_size=POOL_MIN, max_size=POOL_MAX, open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row,
                "application_name": APPLICATION_NAME},
        check=AsyncConnectionPool.check_connection)
    await pool.open(wait=True, timeout=OPEN_TIMEOUT_S)
    try:
        yield pool
    finally:
        await pool.close()


__all__ = ["APPLICATION_NAME", "OPEN_TIMEOUT_S", "POOL_MAX", "POOL_MIN", "checkpoint_pool"]
