# Responsibility: Speak the OpenAI chat-completions wire format for ONE attempt against one target.
# Owns: that format's request shape, its stream/non-stream choice, and the reading of its response.
# Boundaries: one attempt against one target; it chooses no target, decides no retry and prices nothing.
# Collaborates with: providers.py for the client, and messages.py / streaming.py for the wire helpers.

# WHY THIS IS A MODULE AND NOT router.py. Everything below used to sit in router.py as private
# helpers, which was honest while every configured provider spoke this one format. It is the
# format itself - `messages`, `tools` as a top-level list, `choices[0].message`, `max_tokens`,
# `prompt_tokens`/`completion_tokens` - not anything about routing, so it belongs behind the
# WireProtocol seam where a second format can sit beside it rather than inside it.
from __future__ import annotations

from typing import Any

from meshpipeline.adapters.model_inference import providers
from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec, kwargs_for
from meshpipeline.adapters.model_inference.messages import to_provider_messages
from meshpipeline.adapters.model_inference.routing import Usage
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
from meshpipeline.contracts.model_routing import RouteTarget


class _EmptyResponse(Exception):
    pass


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


def _with_tool_kwargs(call_kw: dict, tools, tool_choice, parallel_tool_calls) -> dict:
    if tools:
        call_kw["tools"] = tools
        call_kw["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            call_kw["parallel_tool_calls"] = parallel_tool_calls
    return call_kw


class ChatCompletions:
    """The OpenAI chat-completions protocol, as DeepInfra and DeepSeek serve it."""

    async def invoke(
        self, target: RouteTarget, conversation: Conversation, *,
        tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
        spec: SamplingSpec, parallel_tool_calls: ParallelToolCalls,
        on_reasoning: ReasoningSink, label: str, trace: dict,
    ) -> tuple[Any, Usage]:
        # The sampling parameters a call may carry depend on WHO IS SERVING IT, not only on the
        # role. Built here, per attempt, because a retry may land on a different target than the
        # first try. Whether the role streams is part of that same intent, so it is read off the
        # spec rather than passed separately - the two could not then disagree.
        call_kw = _with_tool_kwargs(kwargs_for(target, spec), tools, tool_choice,
                                    parallel_tool_calls)
        if spec.stream:
            response = await self._streamed(target, conversation, call_kw, label, trace,
                                            on_reasoning=on_reasoning)
        else:
            response = await self._chat(target, conversation, call_kw, trace)
        if not (response and response.choices):
            raise _EmptyResponse(f"{label}: no choices")
        return response, _usage_from(response)

    async def _streamed(self, target: RouteTarget, messages: list, call_kw: dict, label: str,
                        lf: dict, on_reasoning: ReasoningSink = None):
        from meshpipeline.adapters.model_inference.streaming import consume_chat_stream
        stream = await providers.client_for(target).chat.completions.create(
            messages=to_provider_messages(messages), **call_kw, **lf)
        return await consume_chat_stream(stream, label=label, on_reasoning=on_reasoning)

    async def _chat(self, target: RouteTarget, messages: list, call_kw: dict, lf: dict):
        return await providers.client_for(target).chat.completions.create(
            messages=to_provider_messages(messages), **call_kw, **lf)

    def normalize(self, response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
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
