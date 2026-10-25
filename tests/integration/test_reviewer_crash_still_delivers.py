# Responsibility: Verify, on the real LangGraph runtime, that a reviewer node RAISING after the
# executor passed the mesh still ends the run with the mesh to deliver - finished from the last
# saved state with its review marked as not concluded - and that a crash before the gates passed
# is raised as before.
# Boundaries: the real StateGraph and MemorySaver; two stand-in nodes; no service (hermetic).
from __future__ import annotations

import uuid
from typing import TypedDict

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.hermetic]


class _S(TypedDict, total=False):
    executor_success: bool
    executor_failed_gate: str
    reviewer_verdict: str
    api_failure: str
    purpose: str


def _graph(executor_passes: bool):
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    async def executor(state):
        return {"executor_success": executor_passes, "executor_failed_gate": ""}

    async def reviewer(state):
        _ = f"{None:g}"               # lp_base, dev-areen 25 Oct: agents/reviewer/visual.py:80
        return {"reviewer_verdict": "PASS"}

    g = StateGraph(_S)
    g.add_node("executor", executor)
    g.add_node("reviewer", reviewer)
    g.add_edge(START, "executor")
    g.add_edge("executor", "reviewer")
    g.add_edge("reviewer", END)
    return g.compile(checkpointer=MemorySaver())


async def test_a_reviewer_that_raises_still_delivers_the_mesh():
    from meshpipeline.application.post_gate_rescue import REVIEWER_CRASHED, run_to_end

    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    out = await run_to_end(_graph(True), {"purpose": "structural"}, config)
    assert out["executor_success"] is True and out["api_failure"] == REVIEWER_CRASHED
    assert not out.get("reviewer_verdict")


async def test_a_crash_before_the_gates_passed_is_raised_as_before():
    from meshpipeline.application.post_gate_rescue import run_to_end

    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    with pytest.raises(TypeError):
        await run_to_end(_graph(False), {"purpose": "structural"}, config)



@pytest.mark.parametrize("engine,purpose", [("snappy", "external_cfd"), ("cfmesh", "internal_cfd"),
                                            ("gmsh", "structural")])
async def test_a_cfd_or_fea_job_whose_reviewer_raises_after_a_passing_mesh_is_delivered(engine, purpose):
    """The rule is the pipeline's, not one engine's: a snappy or cfMesh flow mesh that passed its
    gates is delivered through a reviewer crash exactly like an FEA mesh, and the terminal rules
    grant it."""
    import logging

    from meshpipeline.application.final_result import RunOutcome, derive_terminal_status
    from meshpipeline.application.post_gate_rescue import REVIEWER_CRASHED, run_to_end
    from meshpipeline.persistence.models import JobStatus

    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    out = await run_to_end(_graph(True), {"purpose": purpose}, config)
    assert out["api_failure"] == REVIEWER_CRASHED and out["purpose"] == purpose
    decision = derive_terminal_status(RunOutcome.from_graph_state({**out, "engine": engine}),
                                      job_id=f"t-{engine}", jlog=logging.getLogger("t"))
    assert decision.status == JobStatus.succeeded
