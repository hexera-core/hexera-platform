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
from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec, spec_for
from meshpipeline.adapters.model_inference.failure_markers import marker_for
from meshpipeline.adapters.model_inference.protocols import (
    _EmptyResponse,
    _ProviderFailure,
    protocol_for,
)
from meshpipeline.adapters.model_inference.providers import classify
from meshpipeline.adapters.model_inference.routing import (
    _NEVER_SWALLOWED,
    RouteExhausted,
    Usage,
    describe_failure,
    execute,
)
from meshpipeline.contracts.model_capacity import CapacityError
from meshpipeline.contracts.model_inference import (
    REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS,
    Conversation,
    ModelRoundResult,
    ParallelToolCalls,
    ProposeReply,
    ProviderAttemptInfo,
    ReasoningSink,
    ToolChoice,
    ToolDefinition,
    ToolsUnsupported,
    UnpricedModel,
)
from meshpipeline.contracts.model_routing import (
    Capability,
    FailureCategory,
    ModelRoute,
    RetryPolicy,
    RouteTarget,
)

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
                         cached_input_tokens=usage.cached_input_tokens,
                         cache_write_tokens=usage.cache_write_tokens,
                         cache_write_30m_tokens=usage.cache_write_30m_tokens)


def _unexpected_category(exc: BaseException) -> FailureCategory:
    """The category of an exception that escaped the route rather than exhausting it. Admission
    (a capacity controller that is missing or broken) is "cannot take this right now" - OVERLOAD,
    a retryable marker - never PROVIDER_DOWN; anything else is classified as a provider error is."""
    if isinstance(exc, CapacityError):
        return FailureCategory.OVERLOAD
    try:
        return _classify(exc)
    except Exception:  # noqa: BLE001 - a classifier that breaks still yields a marker
        return FailureCategory.APPLICATION_DEFECT


async def _run(route: ModelRoute, invoke, *, job_id: str) -> ModelRoundResult:
    # EVERY agent route answers with a result: an answer, or a failure marker the caller's
    # provider-failure path handles. No exception but cancellation and the ownership fences
    # (_NEVER_SWALLOWED) leaves this function - a raw one used to crash the agent node instead
    # (a builder crash ended the run with no retry).
    try:
        response, target, attempts = await execute(route, invoke, _classify, job_id=job_id,
                                                   cost_of=_cost_of)
    except _NEVER_SWALLOWED:
        raise
    except RouteExhausted as exc:
        # `detail` is routing.describe_failure's account - the provider's own words, credentials
        # masked. The marker below keeps only the category, so this line is where they survive.
        logger.error("Router: %s exhausted its route - category=%s phase=%s provider=%s "
                     "model=%s attempts=%d job_id=%s detail=%s", route.role, exc.category.value,
                     exc.phase.value, exc.provider, exc.model, exc.attempts, job_id, exc.detail)
        return ModelRoundResult(
            failure_marker=marker_for(route.role, exc.category),
            provider=ProviderAttemptInfo(attempts=exc.attempts, provider=exc.provider,
                                         model=exc.model))
    except Exception as exc:  # noqa: BLE001 - see above: a marker, never a raw exception
        category = _unexpected_category(exc)
        logger.error("Router: %s route raised instead of answering - category=%s job_id=%s "
                     "detail=%s", route.role, category.value, job_id, describe_failure(exc),
                     exc_info=True)
        return ModelRoundResult(failure_marker=marker_for(route.role, category))
    # Read back through the protocol of the target that ACTUALLY answered, which failover may
    # have made a different one from the target the first attempt was built for.
    try:
        return protocol_for(target.provider).normalize(response, target, attempts)
    except _NEVER_SWALLOWED:
        raise
    except Exception as exc:  # noqa: BLE001
        # The provider answered and the answer could not be read. Asking again may well get a
        # readable one, so it is the retryable EMPTY_RESPONSE marker, not an outage.
        logger.error("Router: %s answer from %s could not be read - job_id=%s detail=%s",
                     route.role, target.label, job_id, describe_failure(exc), exc_info=True)
        return ModelRoundResult(
            failure_marker=marker_for(route.role, FailureCategory.EMPTY_RESPONSE),
            provider=ProviderAttemptInfo(attempts=attempts, provider=target.provider,
                                         model=target.model))


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

    from meshpipeline.contracts.inference_telemetry import calling

    with calling(label):
        return await _run(route, _invoke, job_id=job_id)


async def call_intake_model(messages: Conversation, job_id: str = "", user_id: str = "",
                            name: str = "Intake",
                            tools: list[ToolDefinition] | None = None) -> ModelRoundResult:
    return await _run_chat(icfg.INTAKE_ROUTE, name, messages, job_id, user_id, tools=tools)


# repair review (non-streaming): the proposer and the redirect interpreter, and the investigator's
# native tool mode. It rides the intake route - a short interactive reply on the same chat model -
# under its own trace label. No tools is today's call exactly; tools need a route that declares them.
async def call_repair_review_model(messages: Conversation, job_id: str = "",
                                   name: str = "RepairReview", *,
                                   tools: list[ToolDefinition] | None = None,
                                   user_id: str = "") -> ModelRoundResult:
    route = icfg.INTAKE_ROUTE
    if tools is None:
        return await _run_chat(route, name, messages, job_id, user_id)
    if Capability.TOOLS not in route.required_capabilities:
        raise ToolsUnsupported(f"the {route.role} route declares no tool capability")
    return await _run_chat(route, name, messages, job_id, user_id, tools=tools)


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


# v4 repair proposer (non-streaming, tools, images). Its model is chosen per CALL as a `provider:model`
# key (contracts.model_inference.REPAIR_V4_PROPOSE_ROUTES) rather than read from a ROUTE_MATRIX row, so it
# adds no per-role setting, sampling branch or marker vocabulary. What it does NOT skip is pricing: the
# per-file and per-wave caps are only as good as the meter, so an unpriced key is refused before dialling.
_PROPOSE_ROLE = "repair_v4_propose"
_PROPOSE_SPEC = SamplingSpec(max_tokens=REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS, thinking_off=True)
#: one attempt of a repair_v4_propose round, and the whole round (both attempts and the backoff), in seconds
PROPOSE_ATTEMPT_TIMEOUT_S = 120.0
PROPOSE_ROUND_DEADLINE_S = 300.0


def _propose_route(provider: str, model: str) -> ModelRoute:
    return ModelRoute(
        role=_PROPOSE_ROLE,
        primary=RouteTarget(provider=provider, model=model, circuit_group=f"{_PROPOSE_ROLE}:{model}"),
        required_capabilities=frozenset({Capability.TOOLS, Capability.MULTIMODAL}),
        # One round must fit well inside a file's 600 s wall (evalspec.SHARED_BUDGET): an attempt is cut at 120 s
        # (4096 output tokens at the slowest plausible rate) and the whole round - admission, both attempts and the
        # backoff between them - at 300 s. Measured rounds take 3-4 s.
        timeout_s=PROPOSE_ATTEMPT_TIMEOUT_S, total_deadline_s=PROPOSE_ROUND_DEADLINE_S,
        retry=RetryPolicy(max_attempts=2, backoff_base_s=5.0, backoff_max_s=30.0),
        concurrency_budget=4, queue_deadline_s=60.0, require_price=True)


async def call_repair_v4_propose(
    messages: Conversation, tools: list[ToolDefinition], *, route: str, job_id: str = "",
    user_id: str = "", tool_choice: ToolChoice = "auto", cache_key: str = "",
    max_output_tokens: int | None = None) -> ProposeReply:
    from dataclasses import replace

    from meshpipeline.adapters.inference_telemetry.pricing import estimate_cost, require_price
    from meshpipeline.adapters.model_inference.tracing import langfuse_kwargs

    provider, sep, model = route.partition(":")
    if not (provider and sep and model):
        raise ValueError(f"repair_v4_propose route {route!r} is not 'provider:model'")
    require_price(provider, model)          # before anything else: no price, no call
    spec = replace(_PROPOSE_SPEC, cache_key=cache_key)
    if max_output_tokens is not None:  # only ever LOWER: the proposer reserved exactly this allowance
        spec = replace(spec, max_tokens=max(1, min(int(max_output_tokens), _PROPOSE_SPEC.max_tokens)))

    async def _invoke(target: RouteTarget):
        return await protocol_for(target.provider).invoke(
            target, messages, tools=tools, tool_choice=tool_choice, spec=spec,
            parallel_tool_calls=None, on_reasoning=None, label=f"{_PROPOSE_ROLE}:{job_id[:8]}",
            trace=langfuse_kwargs(session_id=job_id, user_id=user_id, name=_PROPOSE_ROLE))

    route_obj = _propose_route(provider, model)
    try:
        response, target, attempts = await execute(route_obj, _invoke, _classify, job_id=job_id,
                                                   cost_of=_cost_of)
        result = protocol_for(target.provider).normalize(response, target, attempts)
    except UnpricedModel:
        raise
    except _NEVER_SWALLOWED:
        raise
    except RouteExhausted as exc:
        logger.error("Router: %s exhausted its route - category=%s provider=%s model=%s attempts=%d "
                     "job_id=%s detail=%s", _PROPOSE_ROLE, exc.category.value, exc.provider, exc.model,
                     exc.attempts, job_id, exc.detail)
        return ProposeReply(
            ModelRoundResult(failure_marker=f"{_PROPOSE_ROLE}:{exc.category.value}",
                             provider=ProviderAttemptInfo(attempts=exc.attempts, provider=exc.provider,
                                                          model=exc.model)),
            0.0, provider, model)
    except Exception as exc:  # noqa: BLE001 - a marker, never a raw exception, as every other route
        category = _unexpected_category(exc)
        logger.error("Router: %s raised instead of answering - category=%s job_id=%s detail=%s",
                     _PROPOSE_ROLE, category.value, job_id, describe_failure(exc), exc_info=True)
        return ProposeReply(ModelRoundResult(failure_marker=f"{_PROPOSE_ROLE}:{category.value}"),
                            0.0, provider, model)
    cost = estimate_cost(target.provider, target.model, input_tokens=result.input_tokens,
                         output_tokens=result.output_tokens,
                         cached_input_tokens=result.cached_input_tokens,
                         cache_write_tokens=result.cache_write_tokens,
                         cache_write_30m_tokens=result.cache_write_30m_tokens)
    return ProposeReply(result, cost, target.provider, target.model)


def repair_v4_prices(route: str) -> tuple[float, float, float]:
    from meshpipeline.adapters.inference_telemetry.pricing import require_price
    provider, sep, model = route.partition(":")
    if not (provider and sep and model):
        raise ValueError(f"repair_v4_propose route {route!r} is not 'provider:model'")
    p_in, p_out, p_cached = require_price(provider, model)
    # The reservation prices ALL input at the dearest rate input can be billed at. A cache WRITE costs more
    # than fresh input (astra 30m: $12.50/M vs $10/M), so pricing input at p_in alone would make the
    # "upper bound" a lower bound for exactly the tokens the provider writes to cache.
    from meshpipeline.adapters.inference_telemetry.pricing import cache_write_prices
    writes = cache_write_prices(provider, model)
    return (max(p_in, writes[0]) if writes else p_in, p_out, p_cached)
