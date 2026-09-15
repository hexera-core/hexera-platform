# Responsibility: Speak the OpenAI Responses wire format for ONE attempt against one target.
# Owns: dispatching that attempt (streamed or not) and handing back what WireProtocol promises.
# Boundaries: one attempt against one target; it builds no policy and decides no retry - it only
# assembles the request, normalize and stream helpers this protocol's OWN sibling modules already
# implement.
# Collaborates with: responses_request.py (request body), responses_normalize.py (usage and
# result reading) and responses_stream.py (the streamed terminal event), plus providers.py for
# the client itself.

# WHY THIS MODULE DOES SO LITTLE. Every hard question here - what the request body looks like,
# which sampling fields survive, how a stream's terminal event becomes a response object, how
# usage and tool calls are read back out - was already answered by the three modules this one
# assembles (tasks 2-4). Duplicating any of that here would give this format two implementations
# of the same question, which is exactly what chat_completions.py's own WHY note warns against
# for the OTHER protocol. This class exists only so those three modules present a single
# WireProtocol object at the seam in protocols/__init__.py, the same shape ChatCompletions does.
from __future__ import annotations

from typing import Any

from meshpipeline.adapters.model_inference import providers
from meshpipeline.adapters.model_inference.call_kwargs import SamplingSpec
from meshpipeline.adapters.model_inference.protocols import responses_normalize
from meshpipeline.adapters.model_inference.protocols.responses_request import build_request
from meshpipeline.adapters.model_inference.protocols.responses_stream import (
    consume_responses_stream,
)
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

# `providers.client_for` is imported as a module (`from ... import providers`) rather than the
# function itself, so that `protocols.responses.client_for` - what the test patches, and what a
# real caller reaches through this module's own name - resolves through THIS module's namespace.
client_for = providers.client_for


class Responses:
    """The OpenAI Responses protocol - the only wire format `openai` speaks."""

    async def invoke(
        self, target: RouteTarget, conversation: Conversation, *,
        tools: list[ToolDefinition] | None, tool_choice: ToolChoice,
        spec: SamplingSpec, parallel_tool_calls: ParallelToolCalls,
        on_reasoning: ReasoningSink, label: str, trace: dict,
    ) -> tuple[Any, Usage]:
        request = build_request(target, conversation, tools=tools, tool_choice=tool_choice,
                                spec=spec, parallel_tool_calls=parallel_tool_calls)
        client = client_for(target)
        if spec.stream:
            stream = await client.responses.create(**request, **trace)
            response = await consume_responses_stream(stream, label=label,
                                                       on_reasoning=on_reasoning)
        else:
            response = await client.responses.create(**request, **trace)
        return response, responses_normalize.usage_from(response)

    def normalize(self, response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
        return responses_normalize.normalize(response, target, attempts)
