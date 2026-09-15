# Responsibility: Consume a Responses API event stream into the one object responses_normalize
# already knows how to read, so the streamed and non-streamed paths converge before normalisation.
# Boundaries: assembly of a provider event stream; it makes no call, retries nothing, and does not
# normalise - that stays in responses_normalize.py so there is exactly one place that reads output[].
# Collaborates with: responses_normalize.py (consumes this module's return value) and streaming.py,
# whose consume_chat_stream this mirrors for the liveness logging and the reasoning-sink handshake.
from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from meshpipeline.adapters.model_inference.protocols import _EmptyResponse
from meshpipeline.contracts.model_inference import ReasoningSink

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0


async def consume_responses_stream(stream: Any, *, label: str = "stream",
                                   on_reasoning: ReasoningSink = None) -> Any:
    # The terminal `response.completed` event carries the SDK's own assembled `response` object -
    # final output[] and usage already resolved. That is what responses_normalize.normalize() reads,
    # so this function's only job for the OUTPUT is to wait for that event and hand its `.response`
    # straight through, unmodified. Every delta below exists for liveness and the reasoning sink,
    # never for reconstructing output[] - the API already did that reconstruction; redoing it here
    # would be a second, divergent implementation of what response.completed already provides.
    _t_start = time.monotonic()
    _t_first_event: float | None = None
    _t_last_heartbeat = _t_start
    _n_events = 0
    _reasoning_parts: list[str] = []

    completed_response: Any = None

    async for event in stream:
        _n_events += 1
        _now = time.monotonic()
        if _t_first_event is None:
            _t_first_event = _now
            logger.info(
                "consume_responses_stream[%s]: first event after %.1fs",
                label, _now - _t_start,
            )
        if _now - _t_last_heartbeat >= _HEARTBEAT_SECONDS:
            logger.info(
                "consume_responses_stream[%s]: alive - %d events, %.0fs elapsed, "
                "reasoning=%dch (still streaming)",
                label, _n_events, _now - _t_start, sum(len(p) for p in _reasoning_parts),
            )
            _t_last_heartbeat = _now

        event_type = getattr(event, "type", None)

        if event_type == "response.reasoning_summary_text.delta":
            delta = getattr(event, "delta", "") or ""
            _reasoning_parts.append(delta)
            if on_reasoning is not None:
                try:
                    # The Responses delta is already an incremental fragment (unlike chat's
                    # reasoning_content, which repeats from empty each chunk), so the fragment
                    # itself is what gets forwarded here - accumulating first would double it up.
                    # The two-coloured handshake still matches consume_chat_stream's in
                    # streaming.py: sinks come in both colours, so await what comes back when the
                    # sink is a coroutine function rather than inventing a third convention.
                    _emitted = on_reasoning(delta)
                    if inspect.isawaitable(_emitted):
                        await _emitted
                except Exception:            # never let a trace failure break the stream
                    on_reasoning = None
        elif event_type == "response.completed":
            completed_response = getattr(event, "response", None)

    _elapsed = time.monotonic() - _t_start
    _ttfe = (_t_first_event - _t_start) if _t_first_event is not None else -1.0

    if completed_response is None:
        # A stream that ends without response.completed is truncated, not empty-by-design. Raising
        # _EmptyResponse - rather than returning something normalize() would happily read as a
        # successful but empty answer - lets router._classify route this to
        # FailureCategory.EMPTY_RESPONSE, the same outcome an empty chat-completions body gets.
        logger.info(
            "consume_responses_stream[%s]: no response.completed - events=%d elapsed=%.1fs "
            "ttfe=%.1fs reasoning=%dch",
            label, _n_events, _elapsed, _ttfe, sum(len(p) for p in _reasoning_parts),
        )
        raise _EmptyResponse(
            f"responses stream [{label}] ended without a response.completed event "
            f"after {_n_events} events")

    logger.info(
        "consume_responses_stream[%s]: done events=%d elapsed=%.1fs ttfe=%.1fs reasoning=%dch",
        label, _n_events, _elapsed, _ttfe, sum(len(p) for p in _reasoning_parts),
    )
    return completed_response
