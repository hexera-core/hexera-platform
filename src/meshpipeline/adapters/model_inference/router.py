# Responsibility: Perform the one model call each agent role makes.
# Owns: the per-role entry points, message assembly, streaming consumption and telemetry recording.
# Boundaries: it executes a call the routing layer chose.
# Collaborates with: adapters/model_inference/routing.py and the tracing and pricing modules.
from __future__ import annotations

import logging
from typing import Any

import meshpipeline.agents.builder.settings as bcfg
import meshpipeline.agents.intake.settings as icfg
import meshpipeline.agents.reviewer.settings as rcfg
from meshpipeline.adapters.model_inference.failure_markers import marker_for
from meshpipeline.adapters.model_inference.messages import to_provider_messages
from meshpipeline.adapters.model_inference.providers import classify, client_for
from meshpipeline.adapters.model_inference.routing import RouteExhausted, Usage, execute
from meshpipeline.contracts.model_inference import (
    Conversation,
    ModelRoundResult,
    ParallelToolCalls,
    ProviderAttemptInfo,
    ReasoningSink,
    ToolCallRequest,
    ToolChoice,
    ToolDefinition,
)
from meshpipeline.contracts.model_routing import FailureCategory, ModelRoute, RouteTarget

logger = logging.getLogger(__name__)


class _EmptyResponse(Exception):
    pass


def _classify(exc: BaseException) -> FailureCategory:
    if isinstance(exc, _EmptyResponse):
        return FailureCategory.EMPTY_RESPONSE
    return classify(exc)


def _cost_of(target: RouteTarget, usage: Usage) -> float:
    from meshpipeline.adapters.inference_telemetry.pricing import estimate_cost
    return estimate_cost(target.provider, target.model,
                         input_tokens=usage.input_tokens,
                         output_tokens=usage.output_tokens,
                         cached_input_tokens=usage.cached_input_tokens)


def _usage_from(response) -> Usage:
    u = getattr(response, "usage", None)
    if u is None:
        return Usage()
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    if not cached:
        cached = int(getattr(u, "prompt_cache_hit_tokens", 0) or 0)   # direct DeepSeek's name
    return Usage(
        input_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
        cached_input_tokens=cached,
        output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
    )


def _for_target(base: dict, target: RouteTarget) -> dict:
    kw = dict(base)
    kw["model"] = target.model
    return kw


async def _streamed(target: RouteTarget, messages: list, call_kw: dict, label: str, lf: dict,
                    on_reasoning: ReasoningSink = None):
    from meshpipeline.adapters.model_inference.streaming import consume_chat_stream
    stream = await client_for(target).chat.completions.create(
        messages=to_provider_messages(messages), **call_kw, **lf)
    return await consume_chat_stream(stream, label=label, on_reasoning=on_reasoning)


async def _chat(target: RouteTarget, messages: list, call_kw: dict, lf: dict):
    return await client_for(target).chat.completions.create(
        messages=to_provider_messages(messages), **call_kw, **lf)


def _normalize(response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
    choice = response.choices[0]
    msg = choice.message
    calls = tuple(
        ToolCallRequest(id=str(tc.id or ""), name=str(tc.function.name or ""),
                        arguments=str(tc.function.arguments or ""))
        for tc in (msg.tool_calls or []))
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning is None:
        reasoning = (getattr(msg, "model_extra", None) or {}).get("reasoning_content")
    usage = _usage_from(response)
    # Reasoning tokens, ONLY when the provider reports them (OpenAI-style
    # completion_tokens_details.reasoning_tokens). Absent stays 0 = not reported;
    # nothing here substitutes output_tokens for a count nobody gave us.
    _rt = 0
    try:
        _details = getattr(getattr(response, "usage", None),
                           "completion_tokens_details", None)
        _raw = getattr(_details, "reasoning_tokens", None)
        if _raw is None and isinstance(_details, dict):
            _raw = _details.get("reasoning_tokens")
        if _raw is not None:
            _rt = max(0, int(_raw))
    except (TypeError, ValueError, AttributeError):
        _rt = 0
    return ModelRoundResult(
        tool_calls=calls,
        assistant_text=str(msg.content or ""),
        reasoning_text=str(reasoning or ""),
        reasoning_tokens=_rt,
        finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        provider=ProviderAttemptInfo(attempts=attempts, provider=target.provider,
                                     model=target.model))


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
    return _normalize(response, target, attempts)


def _with_tool_kwargs(call_kw: dict, tools, tool_choice, parallel_tool_calls) -> dict:
    if tools:
        call_kw["tools"] = tools
        call_kw["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            call_kw["parallel_tool_calls"] = parallel_tool_calls
    return call_kw


# builder + planner (streaming, tools)
async def _run_glm_stream(route, label, messages: Conversation,
                          tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
                          job_id: str, user_id: str,
                          parallel_tool_calls: ParallelToolCalls = None,
                          *, call_kwargs: dict | None = None,
                          on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.deepinfra import BUILDER_CALL_KWARGS
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    # The base sampling kwargs are role-specific. The builder uses BUILDER_CALL_KWARGS; the
    # planner passes its OWN PLANNER kwargs, so a BUILDER_* change never moves the planner.
    _base = BUILDER_CALL_KWARGS if call_kwargs is None else call_kwargs

    async def _invoke(target: RouteTarget):
        call_kw = _with_tool_kwargs(_for_target(_base, target), tools, tool_choice,
                                    parallel_tool_calls)
        response = await _streamed(
            target, messages, call_kw, f"{label}:{job_id[:8]}",
            langfuse_kwargs(session_id=job_id, user_id=user_id, name=label),
            on_reasoning=on_reasoning)
        if not (response and response.choices):
            raise _EmptyResponse(f"{label}: no choices")
        return response, _usage_from(response)

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
    from meshpipeline.adapters.model_inference.deepinfra import _planner_call_kwargs
    return await _run_glm_stream(pcfg.PLANNER_ROUTE, "Planner",
                                 messages, tools, tool_choice, job_id, user_id,
                                 parallel_tool_calls,
                                 call_kwargs=_planner_call_kwargs(), on_reasoning=on_reasoning)


# reviewers (streaming, tools)
async def _run_reviewer(route, label, messages: Conversation, tools: list[ToolDefinition],
                        job_id: str, user_id: str,
                        parallel_tool_calls: ParallelToolCalls = None,
                        on_reasoning: ReasoningSink = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.deepinfra import REVIEWER_CALL_KWARGS
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    async def _invoke(target: RouteTarget):
        # tool_choice stays "auto" for the reviewer: whether this model honours a FORCED
        # function choice is unverified, so nothing depends on it yet.
        call_kw = _with_tool_kwargs(_for_target(REVIEWER_CALL_KWARGS, target), tools, "auto",
                                    parallel_tool_calls)
        response = await _streamed(
            target, messages, call_kw, f"{label}:{job_id[:8]}",
            langfuse_kwargs(session_id=job_id, user_id=user_id, name=label),
            on_reasoning=on_reasoning)
        if not (response and response.choices):
            raise _EmptyResponse(f"{label}: no choices")
        return response, _usage_from(response)

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
async def _run_chat(route, base_kwargs, label, messages: Conversation, job_id: str,
                    user_id: str, tools: list[ToolDefinition] | None = None,
                    parallel_tool_calls: ParallelToolCalls = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    async def _invoke(target: RouteTarget):
        call_kw = _with_tool_kwargs(_for_target(base_kwargs, target), tools, "auto",
                                    parallel_tool_calls)
        response = await _chat(
            target, messages, call_kw,
            langfuse_kwargs(session_id=job_id, user_id=user_id, name=label))
        if not (response and response.choices):
            raise _EmptyResponse(f"{label}: no choices")
        return response, _usage_from(response)

    return await _run(route, _invoke, job_id=job_id)


async def call_intake_model(messages: Conversation, job_id: str = "", user_id: str = "",
                            name: str = "Intake",
                            tools: list[ToolDefinition] | None = None) -> ModelRoundResult:
    from meshpipeline.adapters.model_inference.deepseek import INTAKE_CALL_KWARGS
    return await _run_chat(icfg.INTAKE_ROUTE, INTAKE_CALL_KWARGS, name,
                           messages, job_id, user_id, tools=tools)


# search summarizer (non-streaming; respond-or-RAISE)
async def call_summarizer_model(messages: Conversation, job_id: str = "") -> ModelRoundResult:
    import meshpipeline.agent_tools.shared.settings as scfg
    from meshpipeline.adapters.model_inference.deepseek import SUMMARIZER_CALL_KWARGS

    async def _invoke(target: RouteTarget):
        call_kw = _for_target(SUMMARIZER_CALL_KWARGS, target)
        response = await _chat(target, messages, call_kw, {})
        if not (response and response.choices):
            raise _EmptyResponse("summarizer: no choices")
        return response, _usage_from(response)

    # Respond-or-RAISE: a route failure propagates as RouteExhausted for the caller's own
    # best-effort handling, unlike the agent routes which convert it to a failure marker.
    response, target, attempts = await execute(scfg.SUMMARIZER_ROUTE, _invoke, _classify,
                                               job_id=job_id, cost_of=_cost_of)
    return _normalize(response, target, attempts)
