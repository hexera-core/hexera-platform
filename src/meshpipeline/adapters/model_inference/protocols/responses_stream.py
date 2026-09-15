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

# The events that end a Responses stream WITH AN ANSWER attached - the SDK's assembled
# `response` object. `response.incomplete` is here, not treated as a dead stream: probed
# 2026-09-15 with max_output_tokens=16, a truncated stream terminates on `response.incomplete`
# whose `.response` holds the partial `output[]`, `status="incomplete"` and the REAL usage - a
# request the account has already paid for and the model has already partly answered. Acting
# only on `response.completed` raised _EmptyResponse for it, throwing that answer away and
# burning two more retries on an output-cap problem no retry can fix. Returned instead, it
# reaches responses_normalize, which turns `incomplete` into finish_reason="length" so the
# builder's truncation recovery runs.
# `response.failed` is DELIBERATELY absent: it reports a server-side error, not a short answer,
# and returning it would normalise to ok=True carrying whatever fragment preceded the failure.
# It is raised below instead, so the same target is retried.
_TERMINAL_EVENTS = ("response.completed", "response.incomplete")


async def consume_responses_stream(stream: Any, *, label: str = "stream",
                                   on_reasoning: ReasoningSink = None) -> Any:
    # A terminal event (_TERMINAL_EVENTS) carries the SDK's own assembled `response` object -
    # output[] and usage already resolved. That is what responses_normalize.normalize() reads,
    # so this function's only job for the OUTPUT is to wait for that event and hand its `.response`
    # straight through, unmodified. Every delta below exists for liveness and the reasoning sink,
    # never for reconstructing output[] - the API already did that reconstruction; redoing it here
    # would be a second, divergent implementation of what response.completed already provides.
    _t_start = time.monotonic()
    _t_first_event: float | None = None
    _t_last_heartbeat = _t_start
    _n_events = 0
    _reasoning_parts: list[str] = []

    terminal_response: Any = None
    terminal_event: str = ""
    saw_failed: bool = False
    failure_detail: Any = None

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
                    # The sink contract is cumulative, not per-fragment: ReasoningSink is
                    # documented (contracts/model_inference.py:55-56) as receiving "the reasoning
                    # so far, as it arrives", and consume_chat_stream's identical handshake in
                    # streaming.py sends "".join(reasoning_parts) rather than the bare chunk. The
                    # two-coloured await handshake matches consume_chat_stream's in streaming.py:
                    # sinks come in both colours, so await what comes back when the sink is a
                    # coroutine function rather than inventing a third convention.
                    _emitted = on_reasoning("".join(_reasoning_parts))
                    if inspect.isawaitable(_emitted):
                        await _emitted
                except Exception:            # never let a trace failure break the stream
                    on_reasoning = None
        elif event_type in _TERMINAL_EVENTS:
            terminal_response = getattr(event, "response", None)
            terminal_event = event_type
        elif event_type == "response.failed":
            # The FACT of the failure is tracked apart from its DETAIL: a response.failed whose
            # `.response` or `.error` is absent would otherwise be indistinguishable from a
            # stream that simply stopped, and the raise below would name the wrong cause.
            saw_failed = True
            failure_detail = getattr(getattr(event, "response", None), "error", None)

    _elapsed = time.monotonic() - _t_start
    _ttfe = (_t_first_event - _t_start) if _t_first_event is not None else -1.0

    if terminal_response is None:
        # A stream that ends with no answer attached - either no terminal event at all (a dead
        # stream: no output, no usage, nothing to bill against) or response.failed. Raising
        # _EmptyResponse - rather than returning something normalize() would happily read as a
        # successful but empty answer - lets router._classify route this to
        # FailureCategory.EMPTY_RESPONSE, retryable against the same target, the same outcome an
        # empty chat-completions body gets.
        logger.info(
            "consume_responses_stream[%s]: no answer - failed=%s events=%d elapsed=%.1fs "
            "ttfe=%.1fs reasoning=%dch",
            label, saw_failed, _n_events, _elapsed, _ttfe,
            sum(len(p) for p in _reasoning_parts),
        )
        if saw_failed:
            raise _EmptyResponse(
                f"responses stream [{label}] ended in response.failed after {_n_events} "
                f"events: {failure_detail if failure_detail is not None else 'no error detail'}")
        raise _EmptyResponse(
            f"responses stream [{label}] ended without a terminal response event "
            f"after {_n_events} events")

    logger.info(
        "consume_responses_stream[%s]: done via %s events=%d elapsed=%.1fs ttfe=%.1fs "
        "reasoning=%dch",
        label, terminal_event, _n_events, _elapsed, _ttfe,
        sum(len(p) for p in _reasoning_parts),
    )
    return terminal_response
