# Responsibility: Run the builder node: one build attempt, from prepared context to a submitted mesh or a refusal.
# Boundaries: the node seam; the attempt's mechanics belong to attempt.py and the loop, and the tools belong to tools/.
# Collaborates with: agents/builder/attempt.py, agents/loop/ and agents/builder/tools/.
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.application.execution_publisher import execution_publisher

logger = logging.getLogger(__name__)

# PipelineState is the inter-node contract (defined in graph.py). Imported under
# TYPE_CHECKING only - annotations are strings here, so this adds typing/IDE support
# without a runtime import (which would cycle: graph imports these node modules).
if TYPE_CHECKING:
    from meshpipeline.contracts.pipeline_state import PipelineState

# Re-exported so `from meshpipeline.agents.builder.agent import ...` callers/tests keep working after
# the split (context machinery → builder_context; tool defs → builder_tools).
from meshpipeline.agents.builder import attempt as attempt_mod
from meshpipeline.agents.builder import attempt_capture, invoke, turn_patch
from meshpipeline.agents.builder import budget as budget_mod
from meshpipeline.agents.builder import noop as noop_mod
from meshpipeline.agents.builder.context import (  # noqa: F401
    _BUILDER_CONTEXT_WINDOW,
    _CHECKPOINT_INJECT_THRESHOLD,
    _CHECKPOINT_RECOVER_THRESHOLD,
    _CHECKPOINT_REINJECTION_GAP,
    _PER_MESSAGE_OVERHEAD_TOKENS,
    _TOKENIZER_ENCODING_NAME,
    _compress_tool_output,
    _count_message_tokens,
    _count_tokens,
    _get_tokenizer,
)
from meshpipeline.agents.builder.driver_run import BuilderDriverRun  # noqa: F401
from meshpipeline.agents.builder.loop import _run_tool_loop  # noqa: F401
from meshpipeline.agents.builder.messages import _build_initial_messages  # noqa: F401
from meshpipeline.agents.builder.tools import (  # noqa: F401
    _PROTECTED_PATHS,
    TOOLS,
    _active_tools,
    _dispatch_tool,
    _tool_geometry_report,
    _tool_list_directory,
    _tool_read_file,
    _tool_run_mesh,
    _tool_run_python,
    _tool_submit_mesh,
    _tool_web_search,
    _tool_write_file,
    get_spec_run_files,
)

# Workspace scaffolding + message construction live in their own modules;
# re-exported so `from meshpipeline.agents.builder.agent import ...` callers/tests keep working.

# The Builder write surface, its runtime guard and the single patch-construction path live in
# `turn_patch`; re-exported below so existing importers keep working.
_BUILDER_RETURN_KEYS = turn_patch.BUILDER_RETURN_KEYS
_builder_return = turn_patch.builder_state
_advisory_block = attempt_mod.advisory_block
_authored_digest = noop_mod.authored_digest


async def node_builder(state: PipelineState) -> dict:
    mode = state.get("builder_mode", "initial")
    job_id = state["job_id"]

    # FENCE - before ANY builder work (model calls, workspace scaffolding, geometry staging).
    # A superseded worker must not spend model budget or touch a workspace a newer generation owns.
    # This stays in the node because it is the ADMISSION point: anywhere later, preparation would
    # already have begun.
    from meshpipeline.application import execution_fence as _fence
    await _fence.assert_current_owner("builder node admission")

    logger.info("Builder starting - job_id=%s mode=%s", job_id, mode)
    # THE one publisher for this build. Every event the builder reaches - its own, the
    # shared loop's, the Snappy driver's, the planner's and the trace - is published
    # through this object, so a generation that has lost the job cannot narrate it.
    _publish = execution_publisher(job_id, agent="builder")

    attempt = attempt_mod.prepare(state, job_id=job_id, mode=mode)
    budget = budget_mod.settle(carried_epoch=state.get("builder_deadline_epoch", 0.0),
                               pipeline_deadline_epoch=state.get("pipeline_deadline_epoch"))

    def _patch(*, retry_count: int, noop_count: int = 0, api_failure: str = "") -> dict:
        return turn_patch.TurnPatch(
            workspace=str(attempt.workspace), request_txt=attempt.request_txt,
            review_brief_txt=attempt.review_brief_txt, deadline_epoch=budget.deadline_epoch,
            retry_count=retry_count, noop_count=noop_count, api_failure=api_failure,
            flag_responses=attempt_mod.flag_responses(attempt.workspace)).state()

    if budget.exhausted:
        # Aggregate budget exhausted: fail TRUTHFULLY without another expensive attempt. Nothing
        # is authored, and no execution success is written - the executor still owns that verdict.
        logger.error("Builder aggregate budget exhausted (%ds total) - job_id=%s: failing "
                     "truthfully", bcfg.BUILDER_TOTAL_TIMEOUT_SECONDS, job_id)
        await _publish.anote("Mesh design exceeded its overall time budget",
                             op_id=f"budget-exhausted:{attempt.retry_count}")
        return _patch(retry_count=bcfg.MAX_BUILDER_RETRIES + 1,      # → failure sink
                      noop_count=state.get("builder_noop_count", 0))

    authored_before = (noop_mod.authored_digest(attempt.workspace, attempt.engine)
                       if attempt.is_retry else "")

    # BUILDER_MAX_TOTAL_ATTEMPTS is the TRUE ceiling (incl. the reviewer-feedback bonus), so the
    # denominator is honest and constant across attempts - no "5 of 4", no goalpost that jumps
    # partway through.
    await _publish.aattempt(attempt.retry_count, bcfg.BUILDER_MAX_TOTAL_ATTEMPTS)
    await _publish.anote("Designing the mesh", op_id=f"designing:{attempt.retry_count}")

    # Supersession and cancellation propagate out of here untouched: a stale generation writes
    # nothing, and a cancelled attempt is not a failed one.
    outcome = await invoke.run_attempt(attempt, state, job_id=job_id, publish=_publish,
                                       timeout_s=budget.attempt_timeout_s)

    if outcome.provider_failed:
        attempt_capture.record_attempt(job_id, attempt=attempt, outcome=outcome)
        # An API failure routes to the failure sink; execution truth is the executor's to write.
        return _patch(retry_count=attempt.retry_count, api_failure=outcome.api_failure)

    verdict = noop_mod.assess(
        before=authored_before,
        after=noop_mod.authored_digest(attempt.workspace, attempt.engine) if authored_before else "",
        carried_count=state.get("builder_noop_count", 0),
        retry_count=attempt.retry_count)
    if verdict.repeated:
        logger.warning("Builder retry no-op: authored mesh spec unchanged - hash=%s "
                       "consecutive_noops=%d - job_id=%s",
                       authored_before, verdict.consecutive, job_id)
    if verdict.budget_exhausted:
        logger.error("Builder: %d consecutive no-ops - exhausting retry budget - job_id=%s",
                     verdict.consecutive, job_id)

    logger.info("Builder finished - job_id=%s mode=%s attempt=%d workspace=%s",
                job_id, mode, attempt.retry_count, attempt.workspace)
    await _publish.anote(f"Mesh built - {len(attempt.tool_calls)} steps",
                         op_id=f"built:{attempt.retry_count}")

    attempt_capture.record_attempt(job_id, attempt=attempt, outcome=outcome, noop=verdict)
    return _patch(retry_count=verdict.retry_count, noop_count=verdict.consecutive)
