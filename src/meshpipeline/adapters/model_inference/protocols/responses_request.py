# Responsibility: Turn a neutral Conversation into the exact JSON body the OpenAI Responses API
# accepts - a pure function from the product's own vocabulary to one provider's wire shape.
# Boundaries: translation only. No network call, no SDK object, no streaming and no reading of a
# response - that is protocols/responses.py's job.
# Collaborates with: call_kwargs.SamplingSpec for sampling intent, contracts/model_routing.py for
# the target being addressed, and protocols/responses.py, which calls build_request() per
# attempt exactly as chat_completions.py calls kwargs_for().

# WHY THIS IS ITS OWN MODULE. Chat Completions and Responses disagree on every axis this file
# touches: where the system prompt lives, what a conversation turn looks like, how a tool call
# and its result travel, and which sampling fields are legal at all. None of that is provider
# GLUE - it is the wire format itself - so it belongs beside chat_completions.py behind the
# WireProtocol seam rather than as a branch inside it. Every shape below was probed live against
# api.openai.com/v1/responses with the production key on 2026-09-15 (design doc §1); several
# read as "surely that's wrong" and are not - see the comments at each one.
from __future__ import annotations

import logging

from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec
from meshpipeline.contracts.model_inference import (
    Conversation,
    ParallelToolCalls,
    ToolChoice,
    ToolDefinition,
)
from meshpipeline.contracts.model_routing import RouteTarget

logger = logging.getLogger(__name__)


def _content_part(part: dict) -> dict:
    # Both parts rename AND, for the image, flatten. Chat's nested
    # {"image_url": {"url": ...}} is refused BY NAME - "Invalid value: 'image_url'. Supported
    # values are: 'input_text', 'input_image', ..." - so this is not cosmetic renaming, it is the
    # only shape the API will parse at all.
    kind = part.get("type")
    if kind == "text":
        return {"type": "input_text", "text": part["text"]}
    if kind == "image_url":
        return {"type": "input_image", "image_url": part["image_url"]["url"]}
    raise ValueError(f"responses_request: unhandled content part type {kind!r}")


def to_input_items(conversation: Conversation) -> tuple[str, list[dict]]:
    """Split a Conversation into the Responses shape.

    System turns leave `input` ENTIRELY and become the top-level `instructions` string - joined
    with "\\n\\n" rather than keeping only the last one, because the product composes more than
    one (a base persona plus a per-job addendum) and dropping any of them is silent prompt loss.
    Everything else becomes one input item:

    - an assistant turn's tool_calls become top-level `function_call` items, not a field on a
      message - Responses has no `message.tool_calls`, tool calls are their own output kind;
    - a `tool` turn becomes a `function_call_output` item, correlated back to its call by the
      `tool_call_id` the chat turn already carries as `call_id`;
    - everything else keeps its role, with multimodal content parts renamed/flattened.
    """
    system_parts: list[str] = []
    items: list[dict] = []
    for turn in conversation:
        role = turn.get("role")
        if role == "system":
            system_parts.append(turn["content"])
            continue
        if role == "assistant" and turn.get("tool_calls"):
            # An assistant turn that also carries text alongside its calls is not covered by any
            # probe, but dropping that text would repeat the exact silent-loss mistake the system
            # turns above are guarded against, so it is emitted as its own item first.
            if turn.get("content"):
                items.append({"role": "assistant", "content": turn["content"]})
            for call in turn["tool_calls"]:
                fn = call["function"]
                items.append({
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": fn["name"],
                    "arguments": fn["arguments"],
                })
            continue
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": turn["tool_call_id"],
                "output": turn["content"],
            })
            continue
        content = turn.get("content")
        if isinstance(content, list):
            content = [_content_part(part) for part in content]
        items.append({"role": role, "content": content})
    return "\n\n".join(system_parts), items


def to_responses_tools(tools: list[ToolDefinition]) -> list[dict]:
    """Flatten chat's externally-tagged tool declaration into Responses' internally-tagged one:
    `{"type":"function","function":{name,description,parameters}}` becomes
    `{"type":"function", name, description, parameters}` at the top level - probed, not assumed."""
    out = []
    for tool in tools:
        fn = tool["function"]
        out.append({"type": tool.get("type", "function"), **fn})
    return out


def to_responses_tool_choice(tool_choice: ToolChoice) -> ToolChoice:
    """Flatten a FORCED tool choice the same way the tool declaration above is flattened.

    `agents/loop/provider_round.py:62-64` builds chat's externally-tagged
    `{"type":"function","function":{"name":...}}` whenever a policy forces a tool, and
    `agents/builder/loop_policy.py` returns a real forced tool - so the builder's first forced
    round would have carried it verbatim. Probed 2026-09-15 against /v1/responses: that shape is
    a 400, *"Missing required parameter: 'tool_choice.name'"*, while the flat
    `{"type":"function","name":"get_x"}` is accepted. The string forms ("auto", "required",
    "none") are identical on both protocols and pass through untouched; so does any other dict
    shape (`{"type":"mcp",...}` and friends), which this product does not build and must not
    have guessed at here.
    """
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        fn = tool_choice.get("function")
        if isinstance(fn, dict):
            return {"type": "function", **fn}
    return tool_choice


# Every one of these is a 400 on the Responses API, on all four priced models, verified
# 2026-09-15 (design doc §1): `temperature` at any non-default value, `top_p` and
# `presence_penalty` outright, `min_p` and `top_k` as unknown parameters. SamplingSpec keeps
# carrying them because DeepInfra and DeepSeek still honour them - only THIS translation drops
# them, so the drop is declared here rather than discovered as a 400 in production.
_REJECTED_SAMPLING_FIELDS = ("temperature", "top_p", "presence_penalty", "min_p", "top_k")

# Logged once per (target, field) rather than once per call: a streaming role redrives this on
# every retry and every round, and a WARNING per round would bury the one fact an operator needs
# - that tuning is being discarded - under noise from a route that behaves this way on purpose,
# every time. Keyed by target rather than by role because build_request is not handed a role name;
# in practice each route's target already identifies which role's tuning is being dropped.
_warned_drops: set[tuple[str, str]] = set()


def _warn_on_dropped_sampling(target: RouteTarget, spec: SamplingSpec) -> None:
    for field in _REJECTED_SAMPLING_FIELDS:
        value = getattr(spec, field)
        if value is None:
            continue
        key = (target.label, field)
        if key in _warned_drops:
            continue
        _warned_drops.add(key)
        logger.warning(
            "responses request for %s: dropping %s=%r - the Responses API rejects it (400); "
            "SamplingSpec keeps carrying it because other protocols still honour it",
            target.label, field, value)


# THE REASONING SUMMARY IS OPT-IN, AND WITHOUT THIS FIELD NOTHING PRODUCES ONE. Probed
# 2026-09-15 on gpt-5.6-luna: `reasoning:{"effort":"high"}` alone returns a reasoning item whose
# `summary` is `[]` and streams zero `response.reasoning_summary_text.delta` events, while
# `{"summary":"auto"}` returns `summary_text` parts and streamed 84 deltas for the same prompt.
# So the whole reasoning chain - the stream sink, the cumulative text, normalize's
# `_summary_text_from_reasoning` - has no producer at all unless this is sent. Accepted (200) on
# all four priced models and alongside tools with a forced tool_choice.
#
# EFFORT IS NOT SET HERE. SamplingSpec carries no effort field, so any value would be this
# module's invention rather than a role's intent; the API's own default stands (design §11).
_REASONING_SUMMARY = {"summary": "auto"}


def build_request(
    target: RouteTarget, conversation: Conversation, *,
    tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
    spec: SamplingSpec, parallel_tool_calls: ParallelToolCalls,
) -> dict:
    """The JSON body for `client.responses.create`."""
    instructions, items = to_input_items(conversation)
    _warn_on_dropped_sampling(target, spec)

    request: dict = {"model": target.model, "input": items}
    if instructions:
        request["instructions"] = instructions
    # `max_tokens` is refused - "Use `max_completion_tokens` instead" - and Responses renames it
    # again, to `max_output_tokens`. Neither older name is sent.
    request["max_output_tokens"] = spec.max_tokens
    if spec.stream:
        request["stream"] = True
    # Gated on the ONE reasoning-related intent SamplingSpec carries. `thinking_off` is how
    # intake and the summarizer say they want no chain-of-thought (it becomes DeepSeek's
    # `thinking: {"type": "disabled"}` on the chat path), and asking OpenAI to narrate reasoning
    # a role has declared it does not want would be incoherent - and billed, since summary text
    # is output tokens. Read this for exactly what it is: NOT a claim that `thinking_off` is
    # honoured here. It is not - OpenAI reasons at its own default either way, and this only
    # decides whether a summary of that reasoning is requested back.
    if not spec.thinking_off:
        request["reasoning"] = dict(_REASONING_SUMMARY)

    if tools:
        request["tools"] = to_responses_tools(tools)
        request["tool_choice"] = to_responses_tool_choice(tool_choice)
        if parallel_tool_calls is not None:
            request["parallel_tool_calls"] = parallel_tool_calls
    return request
