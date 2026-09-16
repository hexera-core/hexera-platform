# Responsibility: Perform the one model call each agent role makes.
# Owns: the per-role entry points, the tracing and pricing they carry, and the conversion of an
# exhausted route into a failure marker.
# Boundaries: it executes a call the routing layer chose; HOW that call is put on the wire
# belongs to the protocol the attempt's target speaks.
# Collaborates with: routing.py, protocols/ (resolved per attempt target), tracing and pricing.
from __future__ import annotations

import logging

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.adapters.model_inference.call_kwargs import spec_for
from meshpipeline.adapters.model_inference.failure_markers import marker_for
from meshpipeline.adapters.model_inference.protocols import (
    _EmptyResponse,
    _ProviderFailure,
    protocol_for,
)
from meshpipeline.adapters.model_inference.providers import classify
from meshpipeline.adapters.model_inference.routing import RouteExhausted, Usage, execute
from meshpipeline.contracts.model_inference import (
    Conversation,
    ModelRoundResult,
    ParallelToolCalls,
    ProviderAttemptInfo,
    ReasoningSink,
    ToolChoice,
    ToolDefinition,
)
from meshpipeline.contracts.model_routing import FailureCategory, ModelRoute, RouteTarget

logger = logging.getLogger(__name__)


def _classify(exc: BaseException) -> FailureCategory:
    # The two seam exceptions classify DIFFERENTLY ON PURPOSE, and the difference is failover.
    # A provider that reports its own generation failed gets the category an
    # openai.InternalServerError gets from providers.classify - SERVICE_UNAVAILABLE, which is in
    # FAILOVER_ELIGIBLE, so the standby is dialled once retries against the sick target are spent.
    # EMPTY_RESPONSE is deliberately NOT failover-eligible (routing._RETRYABLE explains why: an
    # empty answer is worth asking the same model again and is no evidence the provider is
    # unwell). Collapsing these two back into one exception would silently strip failover from
    # every real provider failure and report it in telemetry as an empty completion.
    if isinstance(exc, _ProviderFailure):
        return FailureCategory.SERVICE_UNAVAILABLE
    if isinstance(exc, _EmptyResponse):
        return FailureCategory.EMPTY_RESPONSE
    return classify(exc)


def _cost_of(target: RouteTarget, usage: Usage) -> float:
    from meshpipeline.adapters.inference_telemetry.pricing import estimate_cost
    return estimate_cost(target.provider, target.model,
                         input_tokens=usage.input_tokens,
                         output_tokens=usage.output_tokens,
                         cached_input_tokens=usage.cached_input_tokens)


async def _run(route: ModelRoute, invoke, *, job_id: str) -> ModelRoundResult:
    try:
        response, target, attempts = await execute(route, invoke, _classify, job_id=job_id,
                                                   cost_of=_cost_of)
    except RouteExhausted as exc:
        logger.error("Router: %s exhausted its route - category=%s phase=%s provider=%s "
                     "model=%s attempts=%d job_id=%s", route.role, exc.category.value,
                     exc.phase.value, exc.provider, exc.model, exc.attempts, job_id)
        return ModelRoundResult(
            failure_marker=marker_for(route.role, exc.category),
            provider=ProviderAttemptInfo(attempts=exc.attempts, provider=exc.provider,
                                         model=exc.model))
    # Read back through the protocol of the target that ACTUALLY answered, which failover may
    # have made a different one from the target the first attempt was built for.
    return protocol_for(target.provider).normalize(response, target, attempts)


# builder + planner (streaming, tools)
async def _run_glm_stream(route, label, messages: Conversation,
                          tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
                          job_id: str, user_id: str,
                          parallel_tool_calls: ParallelToolCalls = None,
                          *, on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    async def _invoke(target: RouteTarget):
        # Per ATTEMPT TARGET, not per route: failover can land the next attempt on a provider
        # that speaks a different wire format, and a choice made once for the route would send
        # the primary's request shape to a standby that cannot read it.
        return await protocol_for(target.provider).invoke(
            target, messages, tools=tools, tool_choice=tool_choice,
            spec=spec_for(route.role), parallel_tool_calls=parallel_tool_calls,
            on_reasoning=on_reasoning, label=f"{label}:{job_id[:8]}",
            trace=langfuse_kwargs(session_id=job_id, user_id=user_id, name=label))

    return await _run(route, _invoke, job_id=job_id)


async def call_builder_model(messages: Conversation, tools: list[ToolDefinition] | None = None,
                             tool_choice: ToolChoice = "auto", job_id: str = "",
                             user_id: str = "",
                             parallel_tool_calls: ParallelToolCalls = None, on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    logger.info("Router: Builder request - messages=%d tools=%d tool_choice=%s job_id=%s",
                len(messages), len(tools) if tools else 0, tool_choice, job_id)
    return await _run_glm_stream(bcfg.BUILDER_ROUTE, "Builder",
                                 messages, tools, tool_choice, job_id, user_id,
                                 parallel_tool_calls, on_reasoning=on_reasoning)


async def call_planner_model(messages: Conversation, tools: list[ToolDefinition] | None = None,
                             tool_choice: ToolChoice = "auto", job_id: str = "",
                             user_id: str = "",
                             parallel_tool_calls: ParallelToolCalls = None, on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    import meshpipeline.engines.snappy.settings as pcfg
    return await _run_glm_stream(pcfg.PLANNER_ROUTE, "Planner",
                                 messages, tools, tool_choice, job_id, user_id,
                                 parallel_tool_calls, on_reasoning=on_reasoning)


# reviewers (streaming, tools)
async def _run_reviewer(route, label, messages: Conversation, tools: list[ToolDefinition],
                        job_id: str, user_id: str,
                        parallel_tool_calls: ParallelToolCalls = None,
                        on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    async def _invoke(target: RouteTarget):
        # tool_choice stays "auto" for the reviewer: whether this model honours a FORCED
        # function choice is unverified, so nothing depends on it yet.
        return await protocol_for(target.provider).invoke(
            target, messages, tools=tools, tool_choice="auto",
            spec=spec_for(route.role), parallel_tool_calls=parallel_tool_calls,
            on_reasoning=on_reasoning, label=f"{label}:{job_id[:8]}",
            trace=langfuse_kwargs(session_id=job_id, user_id=user_id, name=label))

    # BOTH reviewer roles emit `reviewer_*`: the user-facing meaning is "the review could not
    # run", not which reviewer variant ran.
    return await _run(route, _invoke, job_id=job_id)


async def call_reviewer_with_tools(messages: Conversation, tools: list[ToolDefinition],
                                   job_id: str = "", user_id: str = "",
                                   parallel_tool_calls: ParallelToolCalls = None,
                                   on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    return await _run_reviewer(rcfg.VISUAL_REVIEWER_ROUTE, "Reviewer", messages, tools,
                               job_id, user_id, parallel_tool_calls, on_reasoning=on_reasoning)


# intake (non-streaming)
async def _run_chat(route, label, messages: Conversation, job_id: str,
                    user_id: str, tools: list[ToolDefinition] | None = None,
                    parallel_tool_calls: ParallelToolCalls = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    async def _invoke(target: RouteTarget):
        return await protocol_for(target.provider).invoke(
            target, messages, tools=tools, tool_choice="auto",
            spec=spec_for(route.role), parallel_tool_calls=parallel_tool_calls,
            on_reasoning=None, label=label,
            trace=langfuse_kwargs(session_id=job_id, user_id=user_id, name=label))

    return await _run(route, _invoke, job_id=job_id)


async def call_intake_model(messages: Conversation, job_id: str = "", user_id: str = "",
                            name: str = "Intake",
                            tools: list[ToolDefinition] | None = None) -> ModelRoundResult:
    return await _run_chat(icfg.INTAKE_ROUTE, name, messages, job_id, user_id, tools=tools)


# search summarizer (non-streaming; respond-or-RAISE)
async def call_summarizer_model(messages: Conversation, job_id: str = "") -> ModelRoundResult:
    import meshpipeline.agent_tools.shared.settings as scfg

    async def _invoke(target: RouteTarget):
        return await protocol_for(target.provider).invoke(
            target, messages, tools=None, tool_choice="auto",
            spec=spec_for(scfg.SUMMARIZER_ROUTE.role), parallel_tool_calls=None,
            on_reasoning=None, label="summarizer", trace={})

    # Respond-or-RAISE: a route failure propagates as RouteExhausted for the caller's own
    # best-effort handling, unlike the agent routes which convert it to a failure marker.
    response, target, attempts = await execute(scfg.SUMMARIZER_ROUTE, _invoke, _classify,
                                               job_id=job_id, cost_of=_cost_of)
    return protocol_for(target.provider).normalize(response, target, attempts)
