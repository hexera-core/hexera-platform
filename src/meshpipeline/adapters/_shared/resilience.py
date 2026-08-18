# Responsibility: Stop calling a dependency that is failing, and let it recover before trying again.
# Owns: the circuit-breaker state machine, the per-dependency registry, and the retry wrapper callers go through.
# Boundaries: it decides whether a call is attempted.
# Collaborates with: errors.py for classification and the model-inference routing layer.
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from enum import Enum

from meshpipeline.errors import FailureClass, SystemFailure, classify_exception

logger = logging.getLogger(__name__)


def _cfg(name: str, default):
    try:
        import meshpipeline.settings.runtime as rtcfg
        return getattr(rtcfg, name, default)
    except Exception:
        return default


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:

    def __init__(self, name: str, *, failure_threshold: int = 5,
                 recovery_timeout: float = 30.0, half_open_max: int = 1,
                 clock=time.monotonic):
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self.half_open_max = max(1, int(half_open_max))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_inflight = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_half_open()
            return self._state

    def _maybe_half_open(self) -> None:
        if (self._state == CircuitState.OPEN
                and self._clock() - self._opened_at >= self.recovery_timeout):
            self._state = CircuitState.HALF_OPEN
            self._half_open_inflight = 0
            logger.info("circuit '%s': OPEN → HALF_OPEN (probing)", self.name)

    def allow(self) -> bool:
        with self._lock:
            self._maybe_half_open()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.OPEN:
                return False
            # HALF_OPEN: allow a limited number of probes
            if self._half_open_inflight < self.half_open_max:
                self._half_open_inflight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                logger.info("circuit '%s': %s → CLOSED (recovered)", self.name, self._state.value)
            self._state = CircuitState.CLOSED
            self._half_open_inflight = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                self._trip()
            elif self._consecutive_failures >= self.failure_threshold:
                self._trip()

    def _trip(self) -> None:
        if self._state != CircuitState.OPEN:
            logger.warning("circuit '%s': → OPEN after %d consecutive failures",
                           self.name, self._consecutive_failures)
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._half_open_inflight = 0


# per-process registry  #
_BREAKERS: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_breaker(name: str) -> CircuitBreaker:
    with _REGISTRY_LOCK:
        b = _BREAKERS.get(name)
        if b is None:
            b = CircuitBreaker(
                name,
                failure_threshold=int(_cfg("CIRCUIT_FAILURE_THRESHOLD", 5)),
                recovery_timeout=float(_cfg("CIRCUIT_RECOVERY_SECONDS", 30.0)),
                half_open_max=int(_cfg("CIRCUIT_HALF_OPEN_MAX", 1)),
            )
            _BREAKERS[name] = b
        return b


def breaker_states() -> dict[str, str]:
    with _REGISTRY_LOCK:
        return {n: b.state.value for n, b in _BREAKERS.items()}


def reset_breakers() -> None:  # test helper
    with _REGISTRY_LOCK:
        _BREAKERS.clear()


def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(base * (2 ** attempt), cap) + random.uniform(0, base)


async def call_with_resilience(
    *, name: str, dependency: str, fn,
    timeout: float, max_retries: int = 3,
    base_delay: float = 1.0, max_delay: float = 30.0,
    fallback=None,
):
    breaker = get_breaker(name)
    _metric("circuit_state", name, breaker.state.value)

    if not breaker.allow():
        _metric_inc("resilience_short_circuit", name)
        logger.warning("resilience[%s]: circuit OPEN - short-circuiting", name)
        if fallback is not None:
            return await _run(fallback)
        raise SystemFailure(dependency, FailureClass.PROVIDER_DOWN,
                            f"circuit '{name}' is open")

    last: SystemFailure | None = None
    for attempt in range(max_retries + 1):
        try:
            result = await asyncio.wait_for(_run(fn), timeout)
            breaker.record_success()
            return result
        except TimeoutError as exc:
            last = SystemFailure(dependency, FailureClass.PROVIDER_TRANSIENT,
                                 f"timed out after {timeout}s", cause=exc)
        except SystemFailure as exc:
            last = exc
        except Exception as exc:
            last = classify_exception(exc, dependency)

        breaker.record_failure()
        _metric_inc("resilience_failure", name)
        logger.warning("resilience[%s]: attempt %d/%d failed - %s",
                       name, attempt + 1, max_retries + 1, last.operator_detail[:160])

        if (not last.failure_class.is_retryable
                or attempt == max_retries
                or not breaker.allow()):
            break
        await asyncio.sleep(_backoff(attempt, base_delay, max_delay))

    if fallback is not None:
        logger.warning("resilience[%s]: exhausted - running fallback", name)
        _metric_inc("resilience_fallback", name)
        return await _run(fallback)
    raise last or SystemFailure(dependency, FailureClass.PROVIDER_DOWN, "exhausted")


async def _run(fn):
    res = fn()
    if asyncio.iscoroutine(res):
        return await res
    return res


# metrics (no-op safe if prometheus_client absent)  #
def _metric_inc(metric: str, label: str) -> None:
    try:
        from meshpipeline.metrics import inc
        inc(metric, label)
    except Exception:
        pass


def _metric(metric: str, label: str, value: str) -> None:
    try:
        from meshpipeline.metrics import set_state
        set_state(metric, label, value)
    except Exception:
        pass
