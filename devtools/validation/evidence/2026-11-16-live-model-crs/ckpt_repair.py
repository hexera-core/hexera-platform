# Read-only: each job's checkpointed `repair_attempt` (and repair_report status) from the pipeline's
# own LangGraph checkpoint, for jobs whose terminal delivery never wrote the repair_report artifact.
import asyncio
import json
import sys

JOBS = sys.argv[1].split(",")
async def main():
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    import meshpipeline.settings.providers as provcfg
    from meshpipeline.contracts.pipeline_state import STATE_SCHEMA_VERSION as V
    from meshpipeline.pipeline.graph import build_graph
    dsn = provcfg.POSTGRES_DSN.replace("+asyncpg", "").replace("+psycopg", "")
    out = {}
    async with AsyncPostgresSaver.from_conn_string(dsn) as cp:
        g = build_graph(checkpointer=cp)
        for j in JOBS:
            best = None
            for gen in range(1, 6):
                snap = await g.aget_state({"configurable": {"thread_id": f"{j}:s{V}:g{gen}"}})
                vals = getattr(snap, "values", None) or {}
                if vals:
                    best = {"generation": gen,
                            "repair_attempt": vals.get("repair_attempt"),
                            "repair_status": (vals.get("repair_report") or {}).get("status")}
            out[j] = best
    print("@@JSON@@" + json.dumps(out, default=str))
asyncio.run(main())
