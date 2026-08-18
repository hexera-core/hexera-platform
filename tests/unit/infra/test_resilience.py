# Responsibility: Verify the breaker, the retry policy and the failure taxonomy behave as declared.
import pytest

import meshpipeline.adapters._shared.resilience as R  # noqa: E402
from meshpipeline.adapters._shared.resilience import CircuitBreaker, CircuitState, call_with_resilience  # noqa: E402
from meshpipeline.errors import (  # noqa: E402
    FailureClass,
    SystemFailure,
    classify_api_failure,
    classify_exception,
    failed_reason_for,
    record_dead_letter,
    user_message_for,
)


class _Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


def test_system_failure_is_picklable():
    # Celery's result backend pickles the raised exception. SystemFailure's
    # positional __init__ broke default pickling (-> UnpickleableExceptionWrapper);
    # __reduce__ must round-trip dependency + class + detail.
    import pickle
    exc = SystemFailure("solvability_gate", FailureClass.INTERNAL,
                        "ValueError: negative axis 0 index: -1",
                        cause=ValueError("boom"))
    back = pickle.loads(pickle.dumps(exc))
    assert isinstance(back, SystemFailure)
    assert back.dependency == "solvability_gate"
    assert back.failure_class is FailureClass.INTERNAL
    assert "negative axis" in back.operator_detail


# circuit breaker  #

def test_breaker_opens_after_threshold():
    cb = CircuitBreaker("x", failure_threshold=3, recovery_timeout=10, clock=_Clock())
    assert cb.state == CircuitState.CLOSED and cb.allow()
    for _ in range(3):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow() is False  # open blocks


def test_breaker_half_open_then_closed_on_success():
    clk = _Clock()
    cb = CircuitBreaker("x", failure_threshold=2, recovery_timeout=10, clock=clk)
    cb.record_failure(); cb.record_failure()
    assert cb.state == CircuitState.OPEN
    clk.advance(11)
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.allow() is True            # one probe allowed
    assert cb.allow() is False           # second probe blocked (half_open_max=1)
    cb.record_success()
    assert cb.state == CircuitState.CLOSED


def test_breaker_half_open_failure_reopens():
    clk = _Clock()
    cb = CircuitBreaker("x", failure_threshold=1, recovery_timeout=5, clock=clk)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    clk.advance(6)
    assert cb.allow() is True            # probe
    cb.record_failure()                  # probe fails
    assert cb.state == CircuitState.OPEN


def test_success_resets_consecutive_failures():
    cb = CircuitBreaker("x", failure_threshold=3, clock=_Clock())
    cb.record_failure(); cb.record_failure()
    cb.record_success()
    cb.record_failure(); cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # counter was reset; 2 < 3


# classification  #

def test_classify_timeout_is_transient():
    class APITimeoutError(Exception): pass
    sf = classify_exception(APITimeoutError("slow"), "deepinfra")
    assert sf.failure_class == FailureClass.PROVIDER_TRANSIENT
    assert sf.failure_class.is_retryable


def test_classify_connection_is_dependency_down():
    class ConnectionError_(Exception): pass
    ConnectionError_.__name__ = "ConnectionError"
    sf = classify_exception(ConnectionError_("refused"), "redis")
    assert sf.failure_class == FailureClass.DEPENDENCY_DOWN


def test_classify_subprocess_timeout_is_resource():
    import subprocess
    sf = classify_exception(subprocess.TimeoutExpired("cmd", 1), "openfoam")
    assert sf.failure_class == FailureClass.RESOURCE


def test_classify_api_failure_strings():
    assert classify_api_failure("rate_limit") == FailureClass.PROVIDER_TRANSIENT
    assert classify_api_failure("reviewer_render_unavailable") == FailureClass.REVIEW_EVIDENCE_MISSING
    assert classify_api_failure("builder_timeout") == FailureClass.PROVIDER_TRANSIENT


def test_user_message_is_blameless():
    for fc in FailureClass:
        if fc.is_system:
            msg = user_message_for(fc).lower()
            assert "try again" in msg
            assert "our side" in msg or "on our side" in msg


def test_failed_reason_maps_to_db_values():
    # must only ever produce values that exist in the DB FailedReason enum
    valid = {"api_failure", "unhandled", "reviewer_rejected", "mesh_generation"}
    for fc in FailureClass:
        assert failed_reason_for(fc) in valid


def test_record_dead_letter_never_raises():
    # no Redis in tests → best-effort, must not raise
    record_dead_letter("job-1", FailureClass.PROVIDER_DOWN, "deepinfra", "down")


# call_with_resilience  #

@pytest.fixture(autouse=True)
def _reset():
    R.reset_breakers()
    yield
    R.reset_breakers()


@pytest.mark.asyncio
async def test_resilience_success_passthrough():
    async def fn(): return 42
    out = await call_with_resilience(name="t", dependency="d", fn=fn, timeout=1, max_retries=2)
    assert out == 42


@pytest.mark.asyncio
async def test_resilience_retries_transient_then_succeeds():
    calls = {"n": 0}
    class APITimeoutError(Exception): pass
    async def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise APITimeoutError("slow")
        return "ok"
    out = await call_with_resilience(name="t", dependency="d", fn=fn,
                                     timeout=1, max_retries=3, base_delay=0.0, max_delay=0.0)
    assert out == "ok" and calls["n"] == 3


@pytest.mark.asyncio
async def test_resilience_non_retryable_fails_fast():
    calls = {"n": 0}
    async def fn():
        calls["n"] += 1
        raise ValueError("permanent")     # INTERNAL → not retryable
    with pytest.raises(SystemFailure) as ei:
        await call_with_resilience(name="t", dependency="d", fn=fn,
                                   timeout=1, max_retries=5, base_delay=0.0)
    assert calls["n"] == 1
    assert ei.value.failure_class == FailureClass.INTERNAL


@pytest.mark.asyncio
async def test_resilience_uses_fallback_on_exhaustion():
    class APITimeoutError(Exception): pass
    async def fn(): raise APITimeoutError("slow")
    async def fb(): return "fallback-result"
    out = await call_with_resilience(name="t", dependency="d", fn=fn, fallback=fb,
                                     timeout=1, max_retries=1, base_delay=0.0, max_delay=0.0)
    assert out == "fallback-result"


@pytest.mark.asyncio
async def test_resilience_short_circuits_when_open():
    # trip the breaker first
    cb = R.get_breaker("t")
    for _ in range(int(__import__("meshpipeline.settings.runtime", fromlist=[""]).CIRCUIT_FAILURE_THRESHOLD)):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    ran = {"n": 0}
    async def fn():
        ran["n"] += 1
        return "should-not-run"
    with pytest.raises(SystemFailure) as ei:
        await call_with_resilience(name="t", dependency="d", fn=fn, timeout=1)
    assert ran["n"] == 0
    assert ei.value.failure_class == FailureClass.PROVIDER_DOWN


@pytest.mark.asyncio
async def test_resilience_timeout_classified_transient():
    import asyncio
    async def fn(): await asyncio.sleep(0.2)
    with pytest.raises(SystemFailure) as ei:
        await call_with_resilience(name="t", dependency="d", fn=fn,
                                   timeout=0.01, max_retries=0)
    assert ei.value.failure_class == FailureClass.PROVIDER_TRANSIENT
