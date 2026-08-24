# Responsibility: Assemble the pipeline state machine and the routing between its nodes.
# Owns: node registration, every routing function, and the compiled graph with its checkpointer.
# Boundaries: structure and routing only - no node's work lives here.
# Collaborates with: the node modules in this package and application/fenced_checkpointer.py.
from __future__ import annotations

import logging
from typing import Final, Literal

from langgraph.graph import END, START, StateGraph

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.agents.builder.agent import node_builder
from meshpipeline.agents.intake.agent import node_intake
from meshpipeline.agents.reviewer.visual import node_reviewer
from meshpipeline.contracts.pipeline_state import PipelineState
from meshpipeline.pipeline.classifier import node_classifier
from meshpipeline.pipeline.engine_select import node_engine_select
from meshpipeline.pipeline.enums import Verdict
from meshpipeline.pipeline.executor import node_executor
from meshpipeline.pipeline.geometry_admission import node_geometry_admission

logger = logging.getLogger(__name__)

# langgraph exports END as a plain `str`, which would widen every routing function's return
# type and cost us the exhaustiveness checking the Literal annotations buy. Bind the literal
# once. The graph tests compare routing results against langgraph's real END, so the two
# cannot drift apart unnoticed.
_END: Final[Literal["__end__"]] = "__end__"


async def node_failure_handler(state: PipelineState) -> dict:
    api_failure = state.get("api_failure", "")
    job_id      = state.get("job_id", "unknown")

    from meshpipeline.errors import classify_api_failure, user_message_for
    fc = classify_api_failure(api_failure)

    logger.warning(
        "node_failure_handler: SYSTEM FAILURE api_failure=%s class=%s - job_id=%s",
        api_failure, fc.value, job_id,
    )
    return {"outcome_message": user_message_for(fc)}



def route_after_builder(state: PipelineState) -> Literal["node_executor", "node_failure_handler"]:
    if state.get("api_failure"):
        return "node_failure_handler"
    return "node_executor"


def route_after_engine_select(state: PipelineState) -> Literal["node_geometry_admission", "node_reviewer"]:
    if state.get("user_dispute") and not state.get("reviewer_verdict"):
        logger.info("route_after_engine_select: dispute run → re-review delivered mesh first "
                    "- job_id=%s", state.get("job_id"))
        return "node_reviewer"
    return "node_geometry_admission"


def route_after_geometry_admission(
    state: PipelineState,
) -> Literal["node_builder", "node_executor"]:
    if state.get("geometry_unsuitable_reason"):
        return "node_executor"
    return "node_builder"


def route_after_executor(
    state: PipelineState,
) -> Literal["node_reviewer", "node_classifier", "__end__"]:
    success            = state.get("executor_success", False)
    retry_count        = state.get("retry_count", 0)
    solvability_failed = state.get("solvability_failed", False)

    if success:
        caveats = state.get("requirement_caveats") or []
        if caveats and retry_count <= bcfg.MAX_BUILDER_RETRIES:
            # a QUALITY-passing mesh that near-missed a stated requirement: while attempts
            # remain, the ladder retries for FULL conformance - delivery-with-caveats is the
            # floor at exhaustion, never a first-attempt shortcut (invariant I5). The
            # classifier hands the builder the gate's own measured diagnostic.
            logger.info(
                "route_after_executor: quality PASS with %d requirement caveat(s), "
                "attempt=%d/%d → classifier (retrying for full conformance)",
                len(caveats), retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS)
            return "node_classifier"
        if caveats:
            logger.warning(
                "route_after_executor: quality PASS with %d requirement caveat(s) and the "
                "ladder spent → reviewer (caveated delivery candidate) - job_id=%s",
                len(caveats), state.get("job_id"))
        return "node_reviewer"

    if retry_count <= bcfg.MAX_BUILDER_RETRIES:
        logger.info(
            "route_after_executor: executor FAIL attempt=%d/%d → classifier",
            retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return "node_classifier"

    # All attempts exhausted and the executor never produced a VALIDATED mesh
    # (contract / manifest / solvability gates all count). Such a mesh is not
    # deliverable, so the reviewer - which judges the VISUAL quality of an
    # already-valid mesh - must be bypassed. Routing an un-validated mesh to the
    # reviewer is exactly how an unchecked mesh could be PASS'd and shipped; end the
    # graph, and let the application render the honest failure from durable facts.
    logger.warning(
        "route_after_executor: executor FAIL all %d attempts exhausted "
        "(solvability_failed=%s) → END (TERMINAL - mesh never validated, "
        "reviewer bypassed) - job_id=%s",
        bcfg.BUILDER_MAX_TOTAL_ATTEMPTS, solvability_failed, state.get("job_id"),
    )
    return _END


def route_after_reviewer(
    state: PipelineState,
) -> Literal["__end__", "node_classifier", "node_failure_handler"]:
    if state.get("api_failure"):
        return "node_failure_handler"

    verdict     = state.get("reviewer_verdict", Verdict.FAIL)
    retry_count = state.get("retry_count", 0)

    _dispute = state.get("user_dispute") or {}
    if _dispute and retry_count == 0 and _dispute.get("mode") != "accept":
        # Dispute INITIAL review (of the parent job's mesh) in REBUILD mode: the user
        # wants a different mesh - this review exists to generate targeted feedback,
        # not to veto. Route to the classifier→builder rebuild regardless of verdict;
        # the POST-rebuild reviews (retry_count >= 1) follow the normal routing below.
        logger.info("route_after_reviewer: dispute initial review (verdict=%s) → rebuild "
                    "- job_id=%s", verdict, state.get("job_id"))
        return "node_classifier"

    if _dispute.get("mode") == "accept" and retry_count == 0:
        # ACCEPT mode: the user inspected the mesh and amended the ACCEPTANCE CRITERIA
        # (their statement joined the review brief). The reviewer has just re-judged the
        # SAME mesh against that corrected bar, so its verdict is HONOURED - pass and
        # deliver, or fail and fall through to the normal retry path. No forced rebuild:
        # rebuilding would throw away the very mesh the user said was good enough.
        logger.info("route_after_reviewer: dispute ACCEPT re-review (verdict=%s) - honouring "
                    "the verdict - job_id=%s", verdict, state.get("job_id"))

    if verdict == Verdict.PASS:
        logger.info(
            "route_after_reviewer: PASS - job_id=%s attempt=%d/%d",
            state.get("job_id"), retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return _END

    if retry_count <= bcfg.MAX_BUILDER_RETRIES:
        logger.info(
            "route_after_reviewer: FAIL attempt=%d/%d → classifier",
            retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return "node_classifier"

    if retry_count == bcfg.MAX_BUILDER_RETRIES + 1 and state.get("executor_success", False):
        logger.info(
            "route_after_reviewer: FAIL attempt=%d/%d - granting reviewer-feedback retry → classifier",
            retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
        )
        return "node_classifier"

    logger.warning(
        "route_after_reviewer: FAIL all %d attempts exhausted → END",
        bcfg.BUILDER_MAX_TOTAL_ATTEMPTS,
    )
    return _END



def _fenced(name: str, fn):
    async def _wrapped(state):
        # The ONE place every node is admitted. A worker whose generation/token was superseded is
        # refused HERE, before the node runs: no model call, no native launch, no workspace
        # mutation, and no result of its accepted into state. Placing it in the shared wrapper
        # (rather than in each node) means a NEW node is fenced by construction.
        from meshpipeline.application import execution_fence as _fence
        await _fence.assert_current_owner(f"graph node {name}")
        return await fn(state)

    _wrapped.__name__ = f"fenced_{name}"
    return _wrapped


def build_graph(checkpointer):
    if checkpointer is None:
        raise ValueError(
            "build_graph: checkpointer is required - pass a MemorySaver or "
            "an AsyncPostgresSaver obtained via 'async with AsyncPostgresSaver.from_conn_string(...)'"
        )

    b = StateGraph(PipelineState)

    # Every node runs inside a trace SPAN via the module-level, best-effort `_traced` wrapper
    # (the ONE shared instrumentation point - nodes stay uninstrumented). See _traced for the
    # fault-isolation contract.
    b.add_node("node_intake",          _fenced("node_intake", node_intake))
    b.add_node("node_engine_select",   _fenced("node_engine_select", node_engine_select))
    b.add_node("node_geometry_admission", _fenced("node_geometry_admission", node_geometry_admission))
    b.add_node("node_builder",         _fenced("node_builder", node_builder))
    b.add_node("node_executor",        _fenced("node_executor", node_executor))
    b.add_node("node_classifier",      _fenced("node_classifier", node_classifier))
    b.add_node("node_reviewer",        _fenced("node_reviewer", node_reviewer))
    b.add_node("node_failure_handler", _fenced("node_failure_handler", node_failure_handler))

    b.add_edge(START, "node_intake")
    b.add_conditional_edges(
        "node_intake",
        lambda s: "node_failure_handler" if s.get("api_failure") else "node_engine_select",
        {"node_engine_select": "node_engine_select", "node_failure_handler": "node_failure_handler"},
    )
    b.add_conditional_edges(
        "node_engine_select",
        route_after_engine_select,
        {"node_geometry_admission": "node_geometry_admission", "node_reviewer": "node_reviewer"},
    )
    b.add_conditional_edges(
        "node_geometry_admission",
        route_after_geometry_admission,
        {"node_builder": "node_builder", "node_executor": "node_executor"},
    )

    b.add_conditional_edges(
        "node_builder",
        route_after_builder,
        {"node_executor": "node_executor", "node_failure_handler": "node_failure_handler"},
    )
    b.add_conditional_edges(
        "node_executor",
        route_after_executor,
        {
            "node_reviewer":   "node_reviewer",
            "node_classifier": "node_classifier",
            END:               END,
        },
    )
    b.add_edge("node_classifier", "node_builder")
    b.add_conditional_edges(
        "node_reviewer",
        route_after_reviewer,
        {
            END:                    END,
            "node_classifier":      "node_classifier",
            "node_failure_handler": "node_failure_handler",
        },
    )
    b.add_edge("node_failure_handler", END)

    return b.compile(checkpointer=checkpointer)
