# Responsibility: Turn a Responses API payload into the one result type every role shares.
# Owns: usage extraction, the output[] walk - text, tool calls and reasoning - for that format,
# and the translation of `status` into the finish_reason vocabulary every consumer already reads.
# Boundaries: normalisation only. No network call, no SDK object construction, no streaming -
# that is protocols/responses.py's job; this module only reads what it returns.
# Collaborates with: responses_request.py (the other half of the same wire format) and
# contracts/model_inference.py, whose ModelRoundResult is the shape every protocol converges on.
from __future__ import annotations

from typing import Any

from meshpipeline.adapters.model_inference.routing import Usage
from meshpipeline.contracts.model_inference import (
    ModelRoundResult,
    ProviderAttemptInfo,
    ToolCallRequest,
)
from meshpipeline.contracts.model_routing import RouteTarget

_MISSING = object()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read one field whether `obj` is an SDK response object (attribute access) or a plain
    dict (what the tests pass, and what a recorded fixture is cheapest to pin as). Tries
    attribute access first - the shape every non-test caller actually hands us - then falls
    back to item access so a dict fixture works without a second code path."""
    value = getattr(obj, key, _MISSING)
    if value is not _MISSING:
        return value
    try:
        return obj[key]
    except (KeyError, TypeError):
        return default


def usage_from(response: Any) -> Usage:
    raw = _get(response, "usage", None)
    if raw is None:
        return Usage()
    input_details = _get(raw, "input_tokens_details", None) or {}
    return Usage(
        input_tokens=int(_get(raw, "input_tokens", 0) or 0),
        cached_input_tokens=int(_get(input_details, "cached_tokens", 0) or 0),
        output_tokens=int(_get(raw, "output_tokens", 0) or 0),
    )


def _reasoning_tokens_from(response: Any) -> int:
    # Reasoning tokens ONLY when the provider actually reports them. Absent stays 0 = not
    # reported; nothing here substitutes output_tokens for a count nobody gave us - see
    # ModelRoundResult.reasoning_tokens's docstring.
    raw = _get(response, "usage", None)
    if raw is None:
        return 0
    output_details = _get(raw, "output_tokens_details", None) or {}
    value = _get(output_details, "reasoning_tokens", None)
    if value is None:
        return 0
    return max(0, int(value))


def _text_from_message(item: Any) -> str:
    parts = _get(item, "content", None) or []
    texts = [str(_get(part, "text", "") or "") for part in parts
             if _get(part, "type", None) == "output_text"]
    return "".join(texts)


def _summary_text_from_reasoning(item: Any) -> str:
    # The Responses reasoning item's `summary` is a list of parts, the non-streamed twin of the
    # `reasoning_summary_text` stream event (design doc §1) - NOT the model's chain-of-thought.
    # ModelRoundResult.reasoning_text is documented as that chain-of-thought (DeepInfra's
    # reasoning_content). A summary is a different, coarser thing. It is carried here anyway
    # because the field is transport-only and never reaches a user - the Builder captures it to
    # the training corpus and has no path that publishes it - but a reader of this function
    # should not assume the two mean the same thing just because they share a field.
    # Filtered on the part's own type, exactly as _text_from_message filters `output_text`:
    # probed 2026-09-15, every part came back `{"type": "summary_text", "text": ...}`, and the
    # reasoning item carries a SIBLING `content` list of a different part type. Concatenating
    # whatever happens to have a `text` field would silently splice a future part kind into the
    # summary rather than ignoring it.
    parts = _get(item, "summary", None) or []
    texts = [str(_get(part, "text", "") or "") for part in parts
             if _get(part, "type", None) == "summary_text"]
    return "".join(texts)


# The two vocabularies do not intersect. Chat says "stop" / "tool_calls" / "length"; Responses
# says "completed" / "incomplete" / "failed" on a DIFFERENT field (`status`). Passing the
# Responses word straight through made agents/builder/loop.py:91 - `if finish_reason ==
# "length"` - dead code on this protocol: a max_output_tokens truncation arrives as "incomplete",
# so the builder never learned it was cut off and silently dropped the tail. The consumer is
# right and the producer changed vocabulary underneath it, so the translation belongs here.
_INCOMPLETE_REASONS = {
    # Probed 2026-09-15: `incomplete_details: {"reason": "max_output_tokens"}` is what a
    # truncated response carries, streamed and not.
    "max_output_tokens": "length",
    "content_filter": "content_filter",
}


def _finish_reason_from(response: Any, has_tool_calls: bool) -> str:
    status = str(_get(response, "status", "") or "")
    if status == "incomplete":
        details = _get(response, "incomplete_details", None) or {}
        reason = str(_get(details, "reason", "") or "")
        # An unrecognised (or absent) reason still means the model stopped before it finished,
        # and "length" is this product's word for that. Guessing "stop" would be the failure
        # this mapping exists to prevent: a truncated round reported as a complete one.
        return _INCOMPLETE_REASONS.get(reason, "length")
    if status == "completed":
        return "tool_calls" if has_tool_calls else "stop"
    # "failed", and the transient statuses a terminal payload should never carry ("queued",
    # "in_progress", "cancelled"), have no chat equivalent to be honestly translated into.
    # They pass through under their own name: nothing branches on them, and inventing "stop"
    # for a failure would be worse than a word the reader can look up.
    return status


def output_items(response: Any) -> list:
    """The response's `output[]`, this protocol's equivalent of chat's `choices[]`.

    Exported so protocols/responses.py can ask "did this attempt return anything readable at
    all?" without a second opinion about where output lives or how to read it off an SDK object.
    """
    return list(_get(response, "output", None) or [])


def normalize(response: Any, target: RouteTarget, attempts: int) -> ModelRoundResult:
    assistant_text = ""
    reasoning_text = ""
    tool_calls: list[ToolCallRequest] = []

    for item in output_items(response):
        kind = _get(item, "type", None)
        if kind == "message":
            assistant_text += _text_from_message(item)
        elif kind == "function_call":
            tool_calls.append(ToolCallRequest(
                id=str(_get(item, "call_id", "") or ""),
                name=str(_get(item, "name", "") or ""),
                arguments=str(_get(item, "arguments", "") or "")))
        elif kind == "reasoning":
            reasoning_text += _summary_text_from_reasoning(item)

    usage = usage_from(response)
    return ModelRoundResult(
        tool_calls=tuple(tool_calls),
        assistant_text=assistant_text,
        reasoning_text=reasoning_text,
        reasoning_tokens=_reasoning_tokens_from(response),
        finish_reason=_finish_reason_from(response, bool(tool_calls)),
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        provider=ProviderAttemptInfo(attempts=attempts, provider=target.provider,
                                     model=target.model))
