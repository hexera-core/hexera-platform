# Responsibility: Consume a streaming chat response into one complete round result.
# Boundaries: assembly of a provider stream, including tool-call fragments; it makes no call and retries nothing.
from __future__ import annotations

import inspect
import logging
import time
from typing import Any

from meshpipeline.contracts.model_inference import ReasoningSink

logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 15.0


async def consume_chat_stream(stream: Any, *, label: str = "stream",
                              on_reasoning: ReasoningSink = None) -> Any:
    # on_reasoning receives the reasoning text so far, as it arrives. Optional and best-effort: a
    # trace that raises must not cost the round, and a caller that passes nothing gets exactly the
    # behaviour it had before. Without it the reasoning is only knowable once the round ends, so a
    # 100-second round shows a live "thinking" card with nothing in it for 100 seconds.
    from openai.types.chat import ChatCompletion, ChatCompletionMessage
    from openai.types.chat.chat_completion import Choice
    from openai.types.chat.chat_completion_message_tool_call import (
        ChatCompletionMessageToolCall,
        Function,
    )

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    finish_reason: str | None = None
    role: str = "assistant"
    usage = None
    _id = "stream"
    _model = ""
    _created = 0

    _t_start = time.monotonic()
    _t_first_chunk: float | None = None
    _t_last_heartbeat = _t_start
    _n_chunks = 0
    _content_chars = 0
    _reasoning_chars = 0

    async for chunk in stream:
        _n_chunks += 1
        _now = time.monotonic()
        if _t_first_chunk is None:
            _t_first_chunk = _now
            logger.info(
                "consume_chat_stream[%s]: first chunk after %.1fs",
                label, _now - _t_start,
            )
        if _now - _t_last_heartbeat >= _HEARTBEAT_SECONDS:
            logger.info(
                "consume_chat_stream[%s]: alive - %d chunks, %.0fs elapsed, "
                "content=%dch reasoning=%dch (still streaming)",
                label, _n_chunks, _now - _t_start, _content_chars, _reasoning_chars,
            )
            _t_last_heartbeat = _now

        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        _id = getattr(chunk, "id", _id) or _id
        _model = getattr(chunk, "model", _model) or _model
        _created = getattr(chunk, "created", _created) or _created

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        choice = choices[0]
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if getattr(delta, "role", None):
            role = delta.role
        if getattr(delta, "content", None):
            content_parts.append(delta.content)
            _content_chars += len(delta.content)

        _rc = getattr(delta, "reasoning_content", None)
        if _rc is None:
            _extra = getattr(delta, "model_extra", None) or {}
            _rc = _extra.get("reasoning_content")
        if _rc:
            reasoning_parts.append(_rc)
            _reasoning_chars += len(_rc)
            if on_reasoning is not None:
                try:
                    # Sinks come in both colours: the reviewer's publisher emits synchronously,
                    # the builder's ownership-checked one only awaitably. Awaiting what comes
                    # back covers both, where calling and discarding would leave the builder -
                    # the one route this exists for - publishing an un-awaited coroutine.
                    _emitted = on_reasoning("".join(reasoning_parts))
                    if inspect.isawaitable(_emitted):
                        await _emitted
                except Exception:            # never let a trace failure break the stream
                    on_reasoning = None

        for tc in getattr(delta, "tool_calls", None) or []:
            idx = getattr(tc, "index", 0) or 0
            slot = tool_acc.setdefault(
                idx, {"id": None, "type": "function", "name": "", "arguments": ""}
            )
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            if getattr(tc, "type", None):
                slot["type"] = tc.type
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"] += fn.arguments

    tool_calls = None
    if tool_acc:
        tool_calls = []
        for idx in sorted(tool_acc):
            s = tool_acc[idx]
            tool_calls.append(
                ChatCompletionMessageToolCall.model_construct(
                    id=s["id"] or f"call_{idx}",
                    type=s["type"] or "function",
                    function=Function.model_construct(
                        name=s["name"], arguments=s["arguments"]
                    ),
                )
            )

    content = "".join(content_parts) if content_parts else None
    reasoning = "".join(reasoning_parts) if reasoning_parts else None

    msg_fields: dict = {"role": role, "content": content}
    if tool_calls is not None:
        msg_fields["tool_calls"] = tool_calls
    if reasoning is not None:
        msg_fields["reasoning_content"] = reasoning
    message = ChatCompletionMessage.model_construct(**msg_fields)

    choice_obj = Choice.model_construct(
        index=0,
        message=message,
        finish_reason=finish_reason or "stop",
    )
    completion = ChatCompletion.model_construct(
        id=_id,
        object="chat.completion",
        created=_created,
        model=_model,
        choices=[choice_obj],
        usage=usage,
    )
    _elapsed = time.monotonic() - _t_start
    _ttft = (_t_first_chunk - _t_start) if _t_first_chunk is not None else -1.0
    logger.info(
        "consume_chat_stream[%s]: done finish=%s chunks=%d elapsed=%.1fs ttft=%.1fs "
        "content=%dch reasoning=%dch tool_calls=%d usage=%s",
        label, finish_reason, _n_chunks, _elapsed, _ttft,
        len(content or ""), len(reasoning or ""), len(tool_calls or []),
        "yes" if usage is not None else "none",
    )
    return completion
