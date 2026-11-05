# Responsibility: Give each round and tool call its span and correlation id.
# Boundaries: tracing structure; it records no payload and changes no control flow.
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from meshpipeline.contracts.event_stream import EventStreamError, ExecutionEventPublisher


@dataclass(frozen=True)
class TraceContext:
    publisher: Any = None
    job_id: str = ""
    role: str = ""
    attempt: int = 0


# The builder's execution-owned counterpart of TraceContext. A DISTINCT type, not a retype:
# reviewer and intake construct TraceContext and must keep publishing synchronously. Its
# publisher is the ownership-checked contract, which exposes no synchronous emitter, so an
# execution trace cannot be built around a plain publisher or fall back to one.
@dataclass(frozen=True)
class ExecutionTraceContext:
    publisher: ExecutionEventPublisher
    job_id: str = ""
    role: str = ""
    attempt: int = 0


def _warn(what: str) -> None:
    try:
        import logging
        logging.getLogger(__name__).warning("public trace: %s could not be published; the run is unaffected", what)
    except Exception:
        pass


def _correlation_id(job_id: str, role: str, attempt: int, round_no: int, index: int,
                    provider_id: str) -> str:
    base = f"c:{job_id or 'job'}:{role or 'agent'}:{attempt}:{round_no}:{index}"
    return base if not provider_id else f"{base}:{str(provider_id)[:40]}"


def call_id(trace: TraceContext | None, round_no: int, index: int, provider_id: str) -> str:
    if trace is None:
        return ""
    return _correlation_id(trace.job_id, trace.role, trace.attempt, round_no, index, provider_id)


def tool_call(trace: TraceContext | None, cid: str, name: str,
              parsed: Any, *, status: str) -> None:
    if not cid or trace is None or trace.publisher is None:
        return
    try:
        trace.publisher.tool_call(cid, name, parsed, status)
    except Exception:      # observability never fails a real invocation
        _warn("tool_call")


def tool_result(trace: TraceContext | None, cid: str, name: str,
                result: Any, status: str, t0: float) -> None:
    if not cid or trace is None or trace.publisher is None:
        return
    try:
        trace.publisher.tool_result(f"{cid}:r", cid, name, result, status,
                                    int((time.monotonic() - t0) * 1000))
    except Exception:
        _warn("tool_result")


def round_begin(trace: TraceContext | None, round_no: int) -> str:
    if trace is None or trace.publisher is None:
        return ""
    try:
        from meshpipeline.trace.policy import reasoning_id
        rid = reasoning_id(trace.job_id, trace.role, trace.attempt, round_no)
        trace.publisher.reasoning(rid, "started", status="active")
        return rid
    except Exception:                 # the trace must never cost a round
        return ""


def reasoning_sink(trace: TraceContext | None, rid: str):
    # A callable the provider hands each reasoning delta to, publishing against the SAME id the
    # round opened with - so the card the browser already shows fills in rather than a new one
    # appearing per chunk. Returns None when there is nothing listening, which is what keeps a
    # non-streamed route and an untraced run on exactly their old path.
    if not rid or trace is None or trace.publisher is None:
        return None

    def _publish(text: str) -> None:
        try:
            trace.publisher.reasoning(rid, "started", status="active", content=text or None)
        except Exception:                    # the trace must never cost a round
            pass

    return _publish


def areasoning_sink(trace: ExecutionTraceContext | None, rid: str):
    # The builder's counterpart of reasoning_sink. The ownership-checked publisher exposes only
    # the awaitable emitter, so this sink is a coroutine function and the stream consumer awaits
    # what it returns. Best-effort like its sibling, INCLUDING on a stale-execution error: a
    # superseded job is caught by the round's own begin and end, and raising here would tear down
    # a stream mid-token instead.
    if not rid or trace is None or trace.publisher is None:
        return None

    async def _publish(text: str) -> None:
        try:
            await trace.publisher.areasoning(rid, "started", status="active",
                                             content=text or None)
        except Exception:                    # the trace must never cost a round
            pass

    return _publish


def accepts_reasoning(call) -> bool:
    # Whether a callable will take the sink at all. Streaming reasoning is an optional capability:
    # the non-streamed routes and every stand-in a test puts in a provider's place were written
    # without it, and offering it to one of those is a TypeError that costs the whole round for a
    # decoration. Asked at each seam that forwards, because the callable one layer down is not the
    # one this layer declares.
    import inspect

    try:
        params = inspect.signature(call).parameters
    except (TypeError, ValueError):          # a builtin or C callable declares nothing
        return False
    return "on_reasoning" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def round_end(trace: TraceContext | None, rid: str, t0: float,
              result: Any, *, phase: str = "completed") -> None:
    if not rid or trace is None or trace.publisher is None:
        return
    try:
        duration_ms = int((time.monotonic() - t0) * 1000)
        # A reasoning-token count is published ONLY when the provider reported one:
        # `reasoning_tokens` is 0 when unreported, and output/total tokens are a
        # different quantity that never stands in for it.
        tokens = int(getattr(result, "reasoning_tokens", 0) or 0) if result else 0
        content = str(getattr(result, "reasoning_text", "") or "") if result else ""
        trace.publisher.reasoning(
            rid, phase, duration_ms=duration_ms,
            token_count=tokens or None, content=content or None,
            status="success" if phase == "completed" else "failure")
    except Exception:
        pass


# Execution-owned counterparts. Same events, same identities, same best-effort rule for a
# transport failure - but a refusal is never swallowed: a worker whose claim is gone must learn
# it here as it would at any other fenced boundary.


# The correlation id is derived, not published, so it is the SAME string on both routes - a
# reader following a call across the two contexts sees one identity, not two.
def execution_call_id(trace: ExecutionTraceContext | None, round_no: int, index: int,
                      provider_id: str) -> str:
    if trace is None:
        return ""
    return _correlation_id(trace.job_id, trace.role, trace.attempt, round_no, index, provider_id)


async def atool_call(trace: ExecutionTraceContext | None, cid: str, name: str,
                     parsed: Any, *, status: str) -> None:
    if not cid or trace is None or trace.publisher is None:
        return
    try:
        await trace.publisher.atool_call(cid, name, parsed, status)
    except EventStreamError:
        raise
    except Exception:      # observability never fails a real invocation
        _warn("tool_call")


async def atool_result(trace: ExecutionTraceContext | None, cid: str, name: str,
                       result: Any, status: str, t0: float) -> None:
    if not cid or trace is None or trace.publisher is None:
        return
    try:
        await trace.publisher.atool_result(f"{cid}:r", cid, name, result, status,
                                           int((time.monotonic() - t0) * 1000))
    except EventStreamError:
        raise
    except Exception:
        _warn("tool_result")


async def around_begin(trace: ExecutionTraceContext | None, round_no: int) -> str:
    if trace is None or trace.publisher is None:
        return ""
    from meshpipeline.trace.policy import reasoning_id
    rid = reasoning_id(trace.job_id, trace.role, trace.attempt, round_no)
    try:
        await trace.publisher.areasoning(rid, "started", status="active")
        return rid
    except EventStreamError:
        raise
    except Exception:                 # the trace must never cost a round
        return ""


async def around_end(trace: ExecutionTraceContext | None, rid: str, t0: float,
                     result: Any, *, phase: str = "completed") -> None:
    if not rid or trace is None or trace.publisher is None:
        return
    try:
        duration_ms = int((time.monotonic() - t0) * 1000)
        tokens = int(getattr(result, "reasoning_tokens", 0) or 0) if result else 0
        content = str(getattr(result, "reasoning_text", "") or "") if result else ""
        await trace.publisher.areasoning(
            rid, phase, duration_ms=duration_ms,
            token_count=tokens or None, content=content or None,
            status="success" if phase == "completed" else "failure")
    except EventStreamError:
        raise
    except Exception:
        pass


__all__ = ["ExecutionTraceContext", "TraceContext", "accepts_reasoning", "areasoning_sink",
           "around_begin", "around_end", "atool_call", "atool_result", "call_id",
           "execution_call_id", "reasoning_sink", "round_begin", "round_end", "tool_call",
           "tool_result"]
