# Responsibility: Record what one build attempt did, for capture and diagnostics.
# Boundaries: serialisation of an attempt already finished.
from __future__ import annotations

from typing import Any

from meshpipeline.capture.logger import TrainingLogger


def serialise_messages(messages: list) -> list:
    out: list = []
    for message in messages:
        copy = dict(message)
        if isinstance(copy.get("content"), list):
            copy["content"] = [
                ({"type": "image_url", "image_url": {"url": "[base64_image_omitted]"}}
                 if part.get("type") == "image_url" else part)
                for part in copy["content"]
            ]
        out.append(copy)
    return out


def record_attempt(job_id: str, *, attempt: Any, outcome: Any, noop: Any = None,
                   logger_factory=TrainingLogger) -> None:
    payload = {
        "mode":             attempt.mode,
        "spec_authored":    outcome.spec_authored,
        "builder_message_histories":        serialise_messages(outcome.messages_out),
        "builder_tool_call_histories":      attempt.tool_calls,
        "builder_system_message_snapshots": attempt.system_snapshot,
        "builder_full_responses":           outcome.final_text,
        "builder_tools_definition":         attempt.tools,
    }
    if outcome.provider_failed:
        # A provider outage authored nothing and ran no tools worth counting: the record says so
        # rather than reporting an empty transcript as a completed attempt.
        payload["api_failure"] = outcome.api_failure
        payload["builder_message_histories"] = []
    else:
        payload["tool_calls"] = len(attempt.tool_calls)
        payload["response_len"] = len(outcome.final_text)
        if noop is not None:
            payload["noop"] = noop.repeated
            payload["noop_count"] = noop.consecutive
    logger_factory(job_id).log("builder_attempt", op_id=f"builder:{attempt.retry_count}",
                               payload=payload, attempt=attempt.retry_count)


__all__ = ["record_attempt", "serialise_messages"]
