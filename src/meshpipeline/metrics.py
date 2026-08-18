# Responsibility: Record the counters and states the deployment is observed through.
# Boundaries: instrumentation; it changes no behaviour and is safe to no-op.
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

try:
    from prometheus_client import Counter, Gauge
    _OK = True
except Exception:
    _OK = False

if _OK:
    _RESILIENCE = Counter(
        "mesh_resilience_events_total",
        "Resilience events by type and dependency",
        ["event", "dependency"],
    )
    _CIRCUIT = Gauge(
        "mesh_circuit_state",
        "Circuit breaker state (0=closed,1=half_open,2=open) by dependency",
        ["dependency"],
        # multiprocess (Celery prefork): report the WORST state across worker
        # processes - if any process has a dependency's breaker open, show open.
        # Required for Gauges under PROMETHEUS_MULTIPROC_DIR; ignored single-process.
        multiprocess_mode="max",
    )
    _FAILURES = Counter(
        "mesh_system_failures_total",
        "System failures by class and dependency",
        ["failure_class", "dependency"],
    )
    _STATE_VAL = {"closed": 0, "half_open": 1, "open": 2}


def inc(event: str, dependency: str) -> None:
    if _OK:
        try:
            _RESILIENCE.labels(event=event, dependency=dependency).inc()
        except Exception:
            pass


def set_state(_metric: str, dependency: str, value: str) -> None:
    if _OK:
        try:
            _CIRCUIT.labels(dependency=dependency).set(_STATE_VAL.get(value, 0))
        except Exception:
            pass


def failure(failure_class: str, dependency: str) -> None:
    if _OK:
        try:
            _FAILURES.labels(failure_class=failure_class, dependency=dependency).inc()
        except Exception:
            pass
