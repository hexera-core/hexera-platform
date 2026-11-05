# Responsibility: Declare where per-call model telemetry is recorded.
# Boundaries: a Protocol and its binding; it aggregates nothing and stores nothing itself.
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class InferenceCall:

    # what it was for
    job_id: str
    role: str
    route_id: str

    # where it actually went
    provider: str
    model: str
    target: str                     # "primary" | "standby"
    selection_reason: str

    # admission
    queue_wait_s: float = 0.0
    active_at_admission: int = 0

    # execution
    attempts: int = 1
    circuit_state: str = ""
    started_at: str = ""            # ISO-8601 UTC
    ended_at: str = ""
    latency_s: float = 0.0
    ttft_s: float | None = None     # time to first token, where the provider streams

    # usage + cost
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    # outcome
    provider_status: str = ""       # e.g. "ok", "429", "503" - a status, never a body
    failure_category: str = ""      # contracts.model_routing.FailureCategory value, or ""
    timed_out: bool = False
    cancelled: bool = False
    failover_occurred: bool = False

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class InferenceTelemetry(Protocol):
    def record(self, call: InferenceCall) -> None:
        ...


_sink: InferenceTelemetry | None = None


def set_inference_telemetry(sink: InferenceTelemetry | None) -> None:
    global _sink
    _sink = sink


def record(call: InferenceCall) -> None:
    if _sink is None:
        return
    try:
        _sink.record(call)
    except Exception:  # noqa: BLE001 - see the Protocol contract above
        import logging
        logging.getLogger(__name__).warning(
            "inference telemetry sink failed; the call itself is unaffected", exc_info=True)
