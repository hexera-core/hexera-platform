# Responsibility: Say which WIRE PROTOCOL a provider speaks, and hand back the adapter that speaks it.
# Boundaries: a registry and its contract; no request is built and no call is made here.
# Collaborates with: router.py, which resolves a protocol per ATTEMPT TARGET.

# WHY THIS EXISTS. Until now a provider was a client plus a parameter set, because both configured
# providers spoke the OpenAI chat-completions wire format - so the format itself could be assumed
# in router.py, streaming.py and messages.py without anyone noticing it was an assumption. OpenAI's
# Responses API is a different protocol on every axis the product uses (see the design doc's §1),
# and Anthropic is a third. Branching on provider inside each of those seven sites would multiply
# them; declaring the protocol once, here, does not.
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec
from meshpipeline.adapters.model_inference.routing import Usage
from meshpipeline.contracts.model_inference import (
    Conversation,
    ModelRoundResult,
    ParallelToolCalls,
    ReasoningSink,
    ToolChoice,
    ToolDefinition,
)
from meshpipeline.contracts.model_routing import RouteTarget


class _EmptyResponse(Exception):
    """A target answered, but with nothing this protocol can read.

    Lives on the seam rather than in one protocol because it is a statement about the ATTEMPT,
    not about a wire format: every protocol can be handed an empty body, and router._classify
    turns this one exception into FailureCategory.EMPTY_RESPONSE for all of them. Keeping it here
    is also what lets router.py import the seam WITHOUT importing any implementation module.
    """


@runtime_checkable
class WireProtocol(Protocol):

    async def invoke(
        self, target: RouteTarget, conversation: Conversation, *,
        tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
        spec: SamplingSpec, parallel_tool_calls: ParallelToolCalls,
        on_reasoning: ReasoningSink, label: str, trace: dict,
    ) -> tuple[Any, Usage]:
        """Make ONE attempt against one target and return what routing.execute expects.

        The returned object is this protocol's own; it is opaque to everything above and is
        handed straight back to this protocol's normalize()."""

    def normalize(self, response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
        """Turn this protocol's response into the one result type every role shares."""


def _registry() -> dict[str, WireProtocol]:
    # Imported inside the function, not at module top. The protocol modules import THIS module
    # (for WireProtocol and _EmptyResponse), so naming them up there would have the seam and its
    # implementations importing each other; and asking "who speaks what" would drag in every
    # protocol's SDK-facing helpers whether or not the caller's provider speaks that protocol.
    from meshpipeline.adapters.model_inference.protocols.chat_completions import ChatCompletions
    from meshpipeline.adapters.model_inference.protocols.responses import Responses
    chat = ChatCompletions()
    # `openai` now resolves to its own protocol rather than being absent. Registering
    # ChatCompletions for it - because it would "probably work" - would send a chat-shaped
    # request to an API whose request and response are different on every axis the product uses,
    # which fails later, more confusingly, and only after the tokens are paid for. No role is
    # routed at "openai" yet (ROUTE_MATRIX is unchanged); this only makes the provider callable.
    return {"deepinfra": chat, "deepseek": chat, "openai": Responses()}


_CACHE: dict[str, WireProtocol] | None = None


def registered() -> list[str]:
    global _CACHE
    if _CACHE is None:
        _CACHE = _registry()
    return sorted(_CACHE)


def protocol_for(provider: str) -> WireProtocol:
    global _CACHE
    if _CACHE is None:
        _CACHE = _registry()
    try:
        return _CACHE[provider]
    except KeyError:
        raise KeyError(
            f"no wire protocol registered for provider {provider!r}; providers that have one: "
            f"{sorted(_CACHE)}") from None
