# Responsibility: Drive the builder's tool loop through the canonical agent loop.
# Boundaries: the builder's binding to the shared loop; budgets, accounting and fencing are the loop's.
from __future__ import annotations

import itertools
import logging
from pathlib import Path

import meshpipeline.agents.builder.settings as bcfg
from meshpipeline.agents.builder.context_prep import BuilderContextPreparer
from meshpipeline.agents.builder.executor import BuilderToolExecutor
from meshpipeline.agents.builder.loop_policy import (
    BUILDER_NO_PROGRESS_THRESHOLD,
    BuilderLoopPolicy,
)
from meshpipeline.agents.builder.tool_context import BuilderToolContext
from meshpipeline.agents.builder.tools import _active_tools
from meshpipeline.agents.loop.runner import run_agent_loop
from meshpipeline.agents.loop.tracing import ExecutionTraceContext, accepts_reasoning
from meshpipeline.application.execution_publisher import StaleExecutionPublish
from meshpipeline.contracts import rationale as _R
from meshpipeline.contracts.agent_loop import LoopExit, LoopLimits
from meshpipeline.contracts.event_stream import ExecutionEventPublisher
from meshpipeline.contracts.model_inference import Conversation

logger = logging.getLogger(__name__)

def _append_tool_result(messages: list[dict], call_id: str, content) -> None:
    messages.append({"role": "tool", "tool_call_id": call_id, "content": content})


async def _run_tool_loop(
    messages: Conversation,
    workspace: Path,
    job_id: str,
    user_id: str,
    max_rounds: int,                   # PROVIDER ROUNDS. The old name said tool calls and never
                                       # governed them; BUILDER_MAX_ROUNDS is what arrives here.
    mode: str = "initial",
    tool_calls_out: list | None = None,
    publish: ExecutionEventPublisher | None = None,
    engine: str = "",                  # the pre-selected mesh engine (runner + authoring palette)
    loop_timeout: int | None = None,   # this attempt's budget = min(per-attempt, aggregate)
    mesh_fidelity: str = "",           # EFFECTIVE mesh-detail tier - soft authoring input only
    attempt: int = 0,
    geometry=None,                     # the VERIFIED MaterializedGeometry for this process
    execution_id: str = "",
    execution_generation: int = 0,
    user_dispute: dict | None = None,   # the engineer's flags, when answering a dispute
) -> tuple[str, list]:
    import time as _time

    # The clock starts when this coroutine starts; run_mesh derives its dispatch budget from what
    # REMAINS so its result (even a timeout verdict) always returns to the model before the outer
    # loop expires.
    effective_timeout = bcfg.BUILDER_LOOP_TIMEOUT if loop_timeout is None else int(loop_timeout)
    loop_deadline = _time.monotonic() + effective_timeout

    # THE typed context every tool in this loop receives. Built once, here, from what the caller
    # already resolved - never rediscovered from the workspace by the tools themselves.
    tool_context = BuilderToolContext(
        workspace=workspace, geometry=geometry, job_id=job_id,
        execution_id=execution_id, execution_generation=execution_generation,
        engine=engine, mesh_fidelity=mesh_fidelity, loop_deadline=loop_deadline,
        user_dispute=user_dispute)
    executor = BuilderToolExecutor(
        context=tool_context, publish=publish, tool_calls_out=tool_calls_out)
    context = BuilderContextPreparer(workspace=workspace, job_id=job_id, engine=engine)
    policy = BuilderLoopPolicy(
        engine=engine, mode=mode, executor=executor, context=context, max_rounds=max_rounds,
        limits_=LoopLimits(max_rounds=max_rounds, total_timeout_s=float(effective_timeout),
                           no_progress_threshold=BUILDER_NO_PROGRESS_THRESHOLD))

    _round_seq = itertools.count(1)

    async def _on_round(result) -> None:
        _round_no = next(_round_seq)
        context.note_provider_tokens(result.input_tokens)
        executor.capture_reasoning(result.reasoning_text)
        if publish and result.assistant_text:
            # the model's account TO THE USER. Its chain-of-thought is captured above and
            # never published: that is the agent talking to itself.
            try:
                await publish.anote(result.assistant_text,
                                    op_id=f"round:{mode}:{_round_no}")
            except StaleExecutionPublish:
                raise
            except Exception:  # noqa: BLE001
                pass
        executor.announce([c.name for c in result.tool_calls])
        if result.finish_reason == "length":
            policy.note_truncated()
            # A truncated round still consumed wall-clock. It counts as a round either
            # way; a valid tool call inside it is NEVER discarded - the runner executes it and
            # the continuation instruction tells the model its results are already above.
            policy.queue_note(policy.note_truncated_continuation() if result.tool_calls
                              else policy.note_truncated_no_tool_call())

    # ONE conversation: the runner appends tool results, corrections and nudges to this list,
    # and it is the list returned. Handing it a copy would silently discard everything the loop
    # produced - including the corrections the model is supposed to read.
    conversation: list = list(messages)
    # No `provider_failure_marker` below: unlike the Reviewer - whose marker IS the non-verdict
    # outcome, a control path - the Builder's exhaustion is fully described by the record's exit
    # reason and tally. A parallel string would be vocabulary with no consumer.
    result = await run_agent_loop(
        driver=policy, provider_call=_provider_call(engine, job_id, user_id),
        messages=conversation, tools=_active_tools(engine), job_id=job_id, user_id=user_id,
        pipeline_attempt=attempt, agent_attempt=attempt, deadline_s=float(effective_timeout),
        append_tool_result=_append_tool_result, on_round_awaited=_on_round,
        # PUBLIC TRACE: role and identity only. What a reader may see is decided by
        # trace.policy from the deployment's mode, never here. The builder's context carries
        # the ownership-checked publisher, so a superseded generation traces nothing.
        execution_trace=(ExecutionTraceContext(publisher=publish, job_id=str(job_id),
                                              role="builder", attempt=int(attempt))
                         if publish is not None else None))

    if result.exit is LoopExit.terminal_action and result.payload:
        # THE AUTHORITATIVE RESULT. Published only after the terminal action carried a
        # payload - never on the way in, and never from the model's own words.
        if publish is not None:
            await _R.abuilder_configuration(publish, ready=True)
        return str(result.payload), conversation
    if result.exit is LoopExit.provider_failed:
        raise RuntimeError(f"[API_FAILURE] {result.failure_marker}")
    # The loop ended without an authoritative submission. Say that, rather than
    # letting the timeline trail off after the last tool.
    if publish is not None:
        await _R.abuilder_configuration(
            publish, ready=False,
            detail="the builder finished its rounds without submitting a mesh specification")
    return "", conversation


def _provider_call(engine: str, job_id: str, user_id: str):
    from meshpipeline.contracts import model_inference as llm_router

    async def _call(*, messages, tools, job_id, user_id, tool_choice="auto", on_reasoning=None):
        logger.info("Router: Builder request - messages=%d tool_choice=%s job_id=%s",
                    len(messages), tool_choice, job_id)
        extra = ({"on_reasoning": on_reasoning}
                 if on_reasoning is not None
                 and accepts_reasoning(llm_router.call_builder_model) else {})
        return await llm_router.call_builder_model(
            messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id,
            user_id=user_id, **extra)
    return _call


__all__ = ["_run_tool_loop"]
