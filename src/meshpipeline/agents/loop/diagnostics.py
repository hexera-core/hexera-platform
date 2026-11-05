# Responsibility: Emit an agent run's record, and the separate record a superseded generation leaves.
# Boundaries: a superseded worker's counters are published under their own event type, never as an agent run.
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from meshpipeline.contracts.agent_loop import AgentRunRecord, DiagnosticValue

logger = logging.getLogger(__name__)

TRAINING_EVENT = "agent_run"
# A superseded generation is NOT an agent run. Separate event type, separate consumer path,
# never merged into the authoritative one.
SUPERSEDED_EVENT = "agent_run_superseded"

# Exactly what may leave this module. Everything else in a record is dropped.
TALLY_FIELDS = ("rounds", "tool_calls", "provider_attempts", "malformed_calls",
                "plaintext_turns", "progress_count", "consecutive_no_progress", "elapsed_s")
LIMIT_FIELDS = ("max_rounds", "total_timeout_s",
                "warn_at_remaining_rounds", "closing_at_remaining_rounds",
                "no_progress_threshold")
ROUND_FIELDS = ("index", "provider_attempts", "finish_reason", "had_tool_calls",
                "input_tokens", "cached_input_tokens", "output_tokens", "duration_ms")
TOOL_CALL_FIELDS = ("round_index", "call_index", "tool", "category", "malformed",
                    "accepted", "duration_ms")


def _scalar(value: Any) -> DiagnosticValue | None:
    if isinstance(value, bool | int | float | str):
        return value
    # Homogeneous tuples only: a mixed sequence is not a series of anything, and admitting one
    # would let an arbitrary object ride along inside it.
    for element in (str, int):
        if isinstance(value, (tuple, list)) and value and all(
                isinstance(v, element) and not isinstance(v, bool) for v in value):
            return tuple(value)
    if isinstance(value, (tuple, list)) and not value:
        return ()
    return None


def sanitize_extension(extension: Any) -> dict[str, DiagnosticValue]:
    try:
        raw: Mapping[str, Any] = extension.sanitized()
    except Exception as exc:  # noqa: BLE001 - diagnostics never fail an agent
        logger.warning("agent diagnostics: extension.sanitized() failed: %s", exc)
        return {}
    out: dict[str, DiagnosticValue] = {}
    for key, value in raw.items():
        scalar = _scalar(value)
        if scalar is not None:
            out[str(key)] = scalar
    return out


def sanitized(record: AgentRunRecord) -> dict[str, Any]:
    tally, limits = record.tally, record.limits
    return {
        "role": record.role.value,
        "job_id": record.job_id,
        "pipeline_attempt": record.pipeline_attempt,
        "agent_attempt": record.agent_attempt,
        "exit": record.exit.value,
        "failure_marker": record.failure_marker,
        "tally": {f: getattr(tally, f) for f in TALLY_FIELDS},
        "calls_by_category": {c.category: c.count for c in tally.calls_by_category},
        "limits": {f: getattr(limits, f) for f in LIMIT_FIELDS},
        "rounds": [{f: getattr(r, f) for f in ROUND_FIELDS} for r in record.rounds],
        "tool_calls": [{f: getattr(t, f) for f in TOOL_CALL_FIELDS} for t in record.tool_calls],
        "extension": sanitize_extension(record.extension),
    }


def emit(record: AgentRunRecord) -> dict[str, Any]:
    payload = sanitized(record)
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(record.job_id).log(
            TRAINING_EVENT, payload, attempt=record.pipeline_attempt,
            op_id=f"agent:{record.role.value}:{record.pipeline_attempt}")
    except Exception as exc:  # noqa: BLE001 - accounting must never fail the agent it observes
        logger.warning("agent diagnostics: could not emit %s for job %s: %s",
                       record.role.value, record.job_id, exc)
    return payload


def emit_superseded(*, role: Any, job_id: str, pipeline_attempt: int, agent_attempt: int,
                    tally: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "authoritative": False,
        "role": getattr(role, "value", str(role)),
        "job_id": job_id,
        "pipeline_attempt": pipeline_attempt,
        "agent_attempt": agent_attempt,
        "exit": "superseded",
        "rounds_before_supersession": getattr(tally, "rounds", 0),
        "tool_calls_before_supersession": getattr(tally, "tool_calls", 0),
    }
    try:
        from meshpipeline.capture.logger import TrainingLogger
        TrainingLogger(job_id).log(SUPERSEDED_EVENT, payload, attempt=pipeline_attempt,
                                   op_id=f"superseded:{pipeline_attempt}")
    except Exception as exc:  # noqa: BLE001 - a superseded worker must not fail louder than it is
        logger.warning("agent diagnostics: could not emit supersession for job %s: %s",
                       job_id, exc)
    return payload


__all__ = ["LIMIT_FIELDS", "ROUND_FIELDS", "SUPERSEDED_EVENT", "TALLY_FIELDS", "TOOL_CALL_FIELDS",
           "TRAINING_EVENT", "emit", "emit_superseded", "sanitize_extension", "sanitized"]
