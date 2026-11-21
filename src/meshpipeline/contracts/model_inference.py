# Responsibility: Declare the one way this product calls a language model, per agent role.
# Owns: the round result, tool-call request and provider-attempt types, the repair-review tool schema and
#       its typed refusal (ToolsUnsupported), plus the per-role entry points.
# Boundaries: it defines the call shape; routing, retries and provider wire formats live in adapters/model_inference/.
# Collaborates with: contracts/model_routing.py and agents/loop/ which drives these calls.
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

# the neutral conversation vocabulary
Role = Literal["system", "user", "assistant", "tool"]

ContentPart = dict[str, Any]
"""One part of a multimodal message. Either {"type": "text", "text": ...} or
{"type": "image_url", "image_url": {"url": ...}} - the reviewer sends a rendered view and its
question as one user turn."""

Message = dict[str, Any]
"""One turn of a conversation - the product's own record, not a vendor's:

    role:         "system" | "user" | "assistant" | "tool"
    content:      str, or list[ContentPart] for a multimodal turn, or None on an
                  assistant turn that only calls tools
    tool_calls:   (assistant) the calls the model asked for - each {"id", "type",
                  "function": {"name", "arguments"}}
    tool_call_id: (tool) which call this result answers
    name:         (tool) the tool that produced it

A dict rather than a frozen TypedDict on purpose: it is a wire record that agents compose
incrementally and the capture layer serialises verbatim.
"""

Conversation = list[Message]
"""A full exchange, oldest turn first - what every call_* function takes."""


class ModelInferenceError(RuntimeError):
    pass


# the normalized round contract
# ONE result type for every route, so a single agent loop can drive Intake's non-streaming call,
# Builder's streaming call and Reviewer's streaming MULTIMODAL call without knowing which is
# which. Streaming stays entirely inside adapters/model_inference: SDK chunks, provider response
# objects and the assembled-stream machinery never cross this boundary.

ToolDefinition = dict[str, Any]
"""One OpenAI-style function-tool declaration, as the agents' rosters already compose them."""

ToolChoice = Literal["auto", "none", "required"] | dict[str, Any]
"""Either a mode, or an explicit {"type": "function", "function": {"name": ...}} selection.
The Builder already uses the explicit form to force a required authoring step."""

ReasoningSink = Callable[[str], None] | Callable[[str], Awaitable[None]] | None
"""Where a streaming route hands the reasoning so far, as it arrives. Two colours because the
two trace routes publish differently: reviewer and intake emit synchronously, the builder's
ownership-checked publisher only awaitably. None is the whole feature switched off, which is
what a non-streamed route and an untraced run pass. Optional everywhere: a round must never
fail because nobody was listening."""

ParallelToolCalls = bool | None
"""None = leave the provider default alone. Typed here because whether a round may contain
several tool calls decides whether a ROUND limit is a meaningful proxy for a TOOL-CALL limit -
an accountability question. No route sets it today; the contract simply stops it being
un-nameable."""


@dataclass(frozen=True)
class ToolCallRequest:

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ProviderAttemptInfo:

    attempts: int = 0
    provider: str = ""
    model: str = ""


@dataclass(frozen=True)
class ModelRoundResult:

    tool_calls: tuple[ToolCallRequest, ...] = ()
    assistant_text: str = ""
    reasoning_text: str = ""
    """The model's chain-of-thought, when the provider returns one (DeepInfra's
    `reasoning_content`). TRANSPORT ONLY. The Builder already captures it to the training corpus
    and has never published it to a user; this field exists so that behaviour survives
    normalization rather than being smuggled out on a vendor object. It must NEVER reach an
    AgentRunRecord, a checkpoint, or a sanitized recording - agents/loop/diagnostics.py has no
    path that can carry it, and contracts/agent_loop.py cannot represent it at all."""
    reasoning_tokens: int = 0
    """How many tokens the provider says the reasoning cost, when it says so at all.

    ZERO MEANS NOT REPORTED, not "no reasoning". Most routes never populate this,
    and the public trace shows a reasoning-token count only when it is a real
    number from the provider - `output_tokens` is a DIFFERENT quantity and must
    never be shown wearing this label. An invented count is worse than none."""
    finish_reason: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    provider: ProviderAttemptInfo = ProviderAttemptInfo()
    failure_marker: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure_marker


ModelReply = ModelRoundResult
"""What a native-tool round answers: text (`assistant_text`), or a typed `tool_calls` list.
The SAME type every route already returns - a tool conversation is not a second result shape,
so the decision cache, telemetry and failure markers hold for it unchanged."""


class ToolsUnsupported(ModelInferenceError):
    """The installed router (or the route it would ask) cannot call tools natively. A caller
    that offered tools treats this as "use the JSON-in-text protocol" - never as a failure."""


@dataclass(frozen=True)
class ToolSchema:
    """One tool a repair-review round may call: its name, what it does, and a JSON Schema for its
    arguments. Typed so a caller declares tools rather than hand-builds vendor dicts; the port
    turns each into the neutral `ToolDefinition` every other route already speaks."""

    name: str
    description: str
    parameters: dict[str, Any]

    def definition(self) -> ToolDefinition:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def assistant_turn(reply: ModelReply) -> Message:
    """The assistant turn a reply makes, for threading tool results back after it."""
    turn: Message = {"role": "assistant", "content": reply.assistant_text or None}
    if reply.tool_calls:
        turn["tool_calls"] = [{"id": c.id, "type": "function",
                               "function": {"name": c.name, "arguments": c.arguments}}
                              for c in reply.tool_calls]
    return turn


def tool_result_turn(call: ToolCallRequest, content: str) -> Message:
    """The `tool` turn answering one call. `content` is the tool's result as text (a caller's
    JSON); it is the model's data, never its instructions."""
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}


@runtime_checkable
class ModelRouter(Protocol):

    async def call_builder_model(
        self, messages: Conversation, tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto", job_id: str = "", user_id: str = "",
        parallel_tool_calls: ParallelToolCalls = None,
        on_reasoning: ReasoningSink = None,
    ) -> ModelRoundResult: ...

    async def call_planner_model(
        self, messages: Conversation, tools: list[ToolDefinition] | None = None,
        tool_choice: ToolChoice = "auto", job_id: str = "", user_id: str = "",
        parallel_tool_calls: ParallelToolCalls = None,
        on_reasoning: ReasoningSink = None,
    ) -> ModelRoundResult: ...

    async def call_intake_model(
        self, messages: Conversation, job_id: str = "", user_id: str = "",
        name: str = "Intake", tools: list[ToolDefinition] | None = None,
    ) -> ModelRoundResult: ...

    # `tools`/`user_id` are optional for a router: one without them is asked only without them,
    # and the port raises ToolsUnsupported for it when tools are offered.
    async def call_repair_review_model(
        self, messages: Conversation, job_id: str = "", name: str = "RepairReview", *,
        tools: list[ToolDefinition] | None = None, user_id: str = "",
    ) -> ModelRoundResult: ...

    async def call_reviewer_with_tools(
        self, messages: Conversation, tools: list[ToolDefinition],
        job_id: str = "", user_id: str = "",
        parallel_tool_calls: ParallelToolCalls = None,
        on_reasoning: ReasoningSink = None,
    ) -> ModelRoundResult: ...

    # The search summarizer is respond-or-RAISE and calls no tools, but it returns the SAME
    # normalized result so no provider response object escapes the adapter on any route.
    async def call_summarizer_model(
        self, messages: Conversation, job_id: str = "",
    ) -> ModelRoundResult: ...


_router: ModelRouter | None = None


def set_model_router(router: ModelRouter) -> None:
    global _router
    _router = router


def _r() -> ModelRouter:
    if _router is None:
        raise ModelInferenceError(
            "no model router configured - runtime composition must call set_model_router()")
    return _router


async def call_builder_model(
    messages: Conversation, tools: list[ToolDefinition] | None = None,
    tool_choice: ToolChoice = "auto", job_id: str = "", user_id: str = "",
    parallel_tool_calls: ParallelToolCalls = None, on_reasoning: ReasoningSink = None,
) -> ModelRoundResult:
    return await _r().call_builder_model(
        on_reasoning=on_reasoning,
        messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id,
        user_id=user_id, parallel_tool_calls=parallel_tool_calls)


async def call_planner_model(
    messages: Conversation, tools: list[ToolDefinition] | None = None,
    tool_choice: ToolChoice = "auto", job_id: str = "", user_id: str = "",
    parallel_tool_calls: ParallelToolCalls = None, on_reasoning: ReasoningSink = None,
) -> ModelRoundResult:
    return await _r().call_planner_model(
        messages=messages, tools=tools, tool_choice=tool_choice, job_id=job_id,
        user_id=user_id, parallel_tool_calls=parallel_tool_calls, on_reasoning=on_reasoning)


async def call_intake_model(
    messages: Conversation, job_id: str = "", user_id: str = "", name: str = "Intake",
    tools: list[ToolDefinition] | None = None,
) -> ModelRoundResult:
    return await _r().call_intake_model(
        messages=messages, job_id=job_id, user_id=user_id, name=name, tools=tools)


def accepts_tools(call: Callable[..., Any]) -> bool:
    """Whether a router's repair-review call takes native tools at all (a router written before
    them does not, and is asked only the way it always was)."""
    try:
        return "tools" in inspect.signature(call).parameters
    except (TypeError, ValueError):
        return False


async def call_repair_review_model(
    messages: Conversation, job_id: str = "", name: str = "RepairReview", *,
    tools: Sequence[ToolSchema] | None = None, user_id: str = "",
) -> ModelReply:
    """One repair-review round. No tools is EXACTLY today's call - the router is asked with the
    same three arguments it always was. With tools, the reply may be a typed `tool_calls` list,
    answered by `tool` turns (`tool_result_turn`); a router that cannot raises ToolsUnsupported.
    `user_id` is the round's owner: an ownerless round is asked and never kept (decision_cache)."""
    router = _r()
    if tools is None and not user_id:
        return await router.call_repair_review_model(messages=messages, job_id=job_id, name=name)
    if not accepts_tools(router.call_repair_review_model):
        if tools is not None:
            raise ToolsUnsupported("the installed router cannot call tools on the repair-review route")
        return await router.call_repair_review_model(messages=messages, job_id=job_id, name=name)
    definitions = None if tools is None else [t.definition() for t in tools]
    return await router.call_repair_review_model(messages=messages, job_id=job_id, name=name,
                                                 tools=definitions, user_id=user_id)


async def call_reviewer_with_tools(
    messages: Conversation, tools: list[ToolDefinition], job_id: str = "", user_id: str = "",
    parallel_tool_calls: ParallelToolCalls = None, on_reasoning: ReasoningSink = None,
) -> ModelRoundResult:
    return await _r().call_reviewer_with_tools(
        messages=messages, tools=tools, job_id=job_id, user_id=user_id,
        parallel_tool_calls=parallel_tool_calls, on_reasoning=on_reasoning)


async def call_summarizer_model(messages: Conversation, job_id: str = "") -> ModelRoundResult:
    return await _r().call_summarizer_model(messages=messages, job_id=job_id)
