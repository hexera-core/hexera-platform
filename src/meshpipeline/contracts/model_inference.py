# Responsibility: Declare the one way this product calls a language model, per agent role.
# Owns: the round result, tool-call request and provider-attempt types, the repair-review tool schema and
#       its typed refusal (ToolsUnsupported), the priced reply and fail-closed refusal (UnpricedModel) of the
#       repair_v4_propose call kind, plus the per-role entry points.
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
    cache_write_tokens: int = 0
    """Input tokens the provider wrote to its prompt cache (a subset of input_tokens, billed above the
    fresh-input rate). Zero for providers that report none."""
    cache_write_30m_tokens: int = 0
    """Of cache_write_tokens, those written at the 30-minute TTL; the remainder is priced at the 12-hour rate."""
    provider: ProviderAttemptInfo = ProviderAttemptInfo()
    failure_marker: str = ""

    @property
    def ok(self) -> bool:
        return not self.failure_marker


ModelReply = ModelRoundResult
"""What a native-tool round answers: text (`assistant_text`), or a typed `tool_calls` list.
The SAME type every route already returns - a tool conversation is not a second result shape,
so the decision cache, telemetry and failure markers hold for it unchanged."""


class UnpricedModel(ModelInferenceError):
    """No price is declared for a `provider:model`. Raised BEFORE the provider is dialled: an unpriced
    model would meter at $0.00 and no spending cap could ever trip, so the call is refused instead.
    The remedy is a `provider:model=<in>,<out>,<cached>` entry in MODEL_PRICE_OVERRIDES."""


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

    # The v4 repair proposer: tools and images, priced per call (see ProposeReply). Its model is a
    # `provider:model` key chosen by the caller, not a role route.
    async def call_repair_v4_propose(
        self, messages: Conversation, tools: list[ToolDefinition], *, route: str, job_id: str = "",
        user_id: str = "", tool_choice: ToolChoice = "auto", cache_key: str = "",
        max_output_tokens: int | None = None,
    ) -> ProposeReply: ...

    # The search summarizer is respond-or-RAISE and calls no tools, but it returns the SAME
    # normalized result so no provider response object escapes the adapter on any route.
    async def call_summarizer_model(
        self, messages: Conversation, job_id: str = "",
    ) -> ModelRoundResult: ...


@dataclass(frozen=True)
class ProposeReply:
    """One `repair_v4_propose` round: the normalized reply, what it cost, and which model answered.

    `cost_usd` is computed where the price table lives (the router), from the provider's own usage
    counts, with the model that ACTUALLY answered - so a caller that keeps a spending ledger never
    needs the price table or the provider's response object. A failed round carries a marker in
    `reply` and costs 0.0."""

    reply: ModelReply
    cost_usd: float
    provider: str
    model: str


REPAIR_V4_PROPOSE_ROUTES: tuple[str, ...] = ("openai:gpt-6-astra", "openai:gpt-5.6-terra")
"""The `provider:model` chain the v4 AI proposer may run on, strongest first. The second entry is the
fallback when the first fails its probe (repair_v4.proposer.select_route). Not a role route: it is
declared here, not in ROUTE_MATRIX, so it adds no per-role setting, spec or marker vocabulary."""

REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS = 4096
"""The hard ceiling on one repair_v4_propose answer (hidden reasoning included). The router sends it as the
request's output limit AND the proposer reserves against it, so the spending estimate is an upper bound on the
output side by construction; change it here and both move together.

MEASURED (derived from the tool schemas, no model call): the largest plausible `propose` call - 5 candidates, each
with 30 targets, the largest params object any registered op accepts and a 400-character rationale, plus two
`declare_unfixable` calls - is 6,485 characters, i.e. 1,850-2,600 tokens at 3.5-2.5 characters per token. A typical
call (1-3 candidates, a handful of targets) is 300-500 tokens. 4096 covers the extreme call with ~1,500 tokens
left for reasoning and a typical call with ~3,500. ASSUMED, not measured: how much hidden reasoning the model
spends. If rounds come back truncated (`AIProposer.result()["truncated_rounds"]`), raise this."""

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


async def call_repair_v4_propose(
    messages: Conversation, tools: Sequence[ToolDefinition], *, route: str, job_id: str = "",
    user_id: str = "", tool_choice: ToolChoice = "auto", cache_key: str = "",
    max_output_tokens: int | None = None,
) -> ProposeReply:
    """One round of the v4 AI proposer: `route` is a `provider:model` from REPAIR_V4_PROPOSE_ROUTES.

    Images ride in `messages` as `image_url` parts and tools are the neutral definitions; `job_id` is
    passed through to telemetry. `cache_key` names the static prefix (system turn + tools) so the
    provider can serve it from its prompt cache. Raises UnpricedModel, before dialling, when the model
    has no declared price; raises ToolsUnsupported for a router written before this call kind."""
    call = getattr(_r(), "call_repair_v4_propose", None)
    if call is None:
        raise ToolsUnsupported("the installed router has no repair_v4_propose call kind")
    # `max_output_tokens` LOWERS the round's output allowance below REPAIR_V4_PROPOSE_MAX_OUTPUT_TOKENS (the proposer
    # reserved exactly that much); it is passed only when set, so a router without the keyword is still called as before
    extra = {} if max_output_tokens is None else {"max_output_tokens": int(max_output_tokens)}
    reply: ProposeReply = await call(messages=messages, tools=list(tools), route=route, job_id=job_id,
                                     user_id=user_id, tool_choice=tool_choice, cache_key=cache_key, **extra)
    return reply


def repair_v4_route_prices(route: str) -> tuple[float, float, float]:
    """(fresh input, output, cached input) USD per 1M tokens for a `provider:model` repair_v4 route, from the
    price table. Raises UnpricedModel (naming MODEL_PRICE_OVERRIDES) when none is declared. The proposer sizes
    its reservations from THIS, so a cheaper model is not reserved at a dearer one's price."""
    prices = getattr(_r(), "repair_v4_prices", None)
    if prices is None:
        raise ToolsUnsupported("the installed router cannot price a repair_v4_propose route")
    result: tuple[float, float, float] = prices(route)
    return result
