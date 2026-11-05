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
from meshpipeline.adapters.model_inference.protocols import _EmptyResponse, responses_normalize
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
        # Resolved through the MODULE, per attempt, exactly as chat_completions.py does it.
        # A module-level `client_for = providers.client_for` would snapshot the function at
        # import and so survive `monkeypatch.setattr(providers, "client_for", ...)` - the patch
        # point two safety tests use to assert the provider is never dialled at all
        # (test_failure_handling_wiring.py, test_provider_error_hardening.py). Under a snapshot
        # those assertions would pass vacuously here and the real client would be built: a paid
        # network call from a unit test on any machine with OPENAI_API_KEY set.
        client = providers.client_for(target)
        # `trace` is DELIBERATELY NOT FORWARDED. It carries langfuse's {session_id, user_id,
        # name}, which are not OpenAI parameters: they work on the chat path only because
        # langfuse's client wrapper PATCHES the completions methods and strips them before the
        # SDK sees them. langfuse 2.36.2 patches Completions/AsyncCompletions/ChatCompletion/
        # Completion and nothing else - `responses.create` is untouched, so passing them here
        # raises TypeError, which providers.classify cannot read and routing therefore calls an
        # APPLICATION_DEFECT: terminal, no retry, no failover. Dropping them loses no tracing
        # that exists - an unpatched method emits no langfuse generation whether or not it is
        # handed these kwargs. THE RESPONSES PATH IS UNTRACED IN LANGFUSE until langfuse gains a
        # Responses wrapper; OpenTelemetry (which instruments httpx, not the SDK) is unaffected.
        if spec.stream:
            stream = await client.responses.create(**request)
            response = await consume_responses_stream(stream, label=label,
                                                      on_reasoning=on_reasoning)
        else:
            response = await client.responses.create(**request)
        # This protocol's `output[]` is chat's `choices[]`: the container that either holds an
        # answer or does not. A payload with no output items at all - a `failed` status, or an
        # `incomplete` that produced nothing - normalises to ok=True with assistant_text="", so
        # without this the user is handed a blank reply where the chat path would have retried
        # and then reported a failure marker. Sited after both branches, like chat's own guard.
        # NOT a "no readable text" check: a truncated response whose only item is a `reasoning`
        # item (probed 2026-09-15 at max_output_tokens=16) is KEPT, because it carries real usage
        # and normalises to finish_reason="length", which is what the builder's truncation
        # recovery reads. Discarding that would burn retries on a request already paid for.
        if not (response is not None and responses_normalize.output_items(response)):
            raise _EmptyResponse(f"{label}: no output items")
        return response, responses_normalize.usage_from(response)

    def normalize(self, response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
        return responses_normalize.normalize(response, target, attempts)
