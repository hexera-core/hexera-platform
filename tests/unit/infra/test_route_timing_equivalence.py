# Responsibility: Verify the backoff schedules are unchanged and every exit releases its capacity lease.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference.routes import all_routes
from meshpipeline.adapters.model_inference.routing import (
    Phase,
    RouteExhausted,
    Usage,
    execute,
)
from meshpipeline.contracts import inference_telemetry, model_capacity
from meshpipeline.contracts.model_routing import FailureCategory as FC


@pytest.fixture(autouse=True)
def _wired():
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))
    inference_telemetry.set_inference_telemetry(None)
    yield
    model_capacity.set_capacity_controller(None)
    reset_breakers()


# the preserved schedules, read from each role's own policy
# (role, category, attempt) -> the exact delay the pre-routing implementation used.
TIMINGS = [
    # builder: ordinary min(15*2^n, 120); rate limit min(60*2^n, 300)
    ("builder", FC.CONNECTION, 0, 15.0), ("builder", FC.CONNECTION, 1, 30.0),
    ("builder", FC.CONNECTION, 4, 120.0),                      # capped
    ("builder", FC.RATE_LIMIT, 0, 60.0), ("builder", FC.RATE_LIMIT, 1, 120.0),
    ("builder", FC.RATE_LIMIT, 2, 240.0), ("builder", FC.RATE_LIMIT, 3, 300.0),  # capped
    # planner shares the builder's schedule (it shared the builder's call)
    ("planner", FC.CONNECTION, 0, 15.0), ("planner", FC.RATE_LIMIT, 0, 60.0),
    # reviewers: ordinary min(5*2^n, 60); rate limit min(60*2^n, 300)
    ("visual_reviewer", FC.CONNECTION, 0, 5.0), ("visual_reviewer", FC.CONNECTION, 1, 10.0),
    ("visual_reviewer", FC.CONNECTION, 5, 60.0),               # capped
    ("visual_reviewer", FC.RATE_LIMIT, 0, 60.0), ("visual_reviewer", FC.RATE_LIMIT, 1, 120.0),
    # intake: NO rate-limit special case - min(5*2^n, 60) for everything
    ("intake", FC.CONNECTION, 0, 5.0), ("intake", FC.RATE_LIMIT, 0, 5.0),
    ("intake", FC.RATE_LIMIT, 1, 10.0), ("intake", FC.RATE_LIMIT, 5, 60.0),
    # summarizer: no distinct rate-limit policy existed; do not invent one
    ("summarizer", FC.CONNECTION, 0, 2.0), ("summarizer", FC.RATE_LIMIT, 0, 2.0),
]


@pytest.mark.parametrize("role,category,attempt,expected", TIMINGS,
                         ids=[f"{r}-{c.value}-n{n}" for r, c, n, _ in TIMINGS])
def test_the_backoff_schedule_is_unchanged(role, category, attempt, expected):
    assert all_routes()[role].retry.backoff_for(category, attempt) == expected


def test_rate_limits_back_off_longer_than_ordinary_failures_where_they_always_did():
    for role in ("builder", "planner", "visual_reviewer"):
        p = all_routes()[role].retry
        assert p.backoff_for(FC.RATE_LIMIT, 0) > p.backoff_for(FC.CONNECTION, 0), role


# the executor actually USES the category-specific schedule
async def _drive(route, exc, monkeypatch) -> list[float]:
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(s, *a, **k):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _invoke(_t):
        raise exc

    def _classify(e):
        return FC.RATE_LIMIT if isinstance(e, PermissionError) else FC.CONNECTION

    with pytest.raises(RouteExhausted):
        await execute(route, _invoke, _classify, job_id="j")
    return slept


async def test_the_executor_sleeps_the_rate_limit_schedule_for_a_rate_limit(monkeypatch):
    route = all_routes()["builder"]
    slept = await _drive(route, PermissionError("429"), monkeypatch)
    # builder: 3 attempts -> 2 sleeps, jittered around 60 and 120
    assert len(slept) == 2
    assert 45.0 <= slept[0] <= 75.0, slept        # 60 +/- 25% jitter
    assert 90.0 <= slept[1] <= 150.0, slept       # 120 +/- 25%


async def test_the_executor_sleeps_the_ordinary_schedule_for_an_ordinary_failure(monkeypatch):
    route = all_routes()["builder"]
    slept = await _drive(route, ConnectionError("down"), monkeypatch)
    assert len(slept) == 2
    assert 11.0 <= slept[0] <= 19.0, slept        # 15 +/- 25%
    assert 22.0 <= slept[1] <= 38.0, slept        # 30 +/- 25%


# the summarizer's failure contract
async def test_the_summarizer_route_deadline_is_total_not_per_attempt(monkeypatch):
    route = all_routes()["summarizer"]
    assert route.total_deadline_s == 60.0
    # the per-attempt timeout can never exceed what the total budget has left
    assert route.timeout_s <= route.total_deadline_s


async def test_the_summarizer_gets_exactly_one_retry_and_never_a_third_attempt(monkeypatch):
    route = all_routes()["summarizer"]
    calls = 0

    async def _invoke(_t):
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    monkeypatch.setattr(asyncio, "sleep", _noop)
    with pytest.raises(RouteExhausted):
        await execute(route, _invoke, lambda e: FC.CONNECTION, job_id="j")
    assert calls == 2, f"summarizer made {calls} attempts; exactly 2 are approved"


async def test_a_retryable_first_failure_then_success_returns_normally(monkeypatch):
    route = all_routes()["summarizer"]
    calls = 0

    async def _invoke(_t):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("blip")
        return ("distilled", Usage(input_tokens=5))

    monkeypatch.setattr(asyncio, "sleep", _noop)
    result, _target, _attempts = await execute(route, _invoke, lambda e: FC.CONNECTION, job_id="j")
    assert result == "distilled" and calls == 2


@pytest.mark.parametrize("category", [FC.AUTH, FC.INVALID_REQUEST, FC.INSUFFICIENT_BALANCE,
                                      FC.APPLICATION_DEFECT, FC.TOOL_SCHEMA,
                                      FC.POLICY_REJECTION, FC.INVALID_OUTPUT,
                                      FC.QUALITY_FAILED],
                         ids=lambda c: c.value)
async def test_a_non_retryable_summarizer_failure_is_never_retried(category, monkeypatch):
    route = all_routes()["summarizer"]
    calls = 0

    async def _invoke(_t):
        nonlocal calls
        calls += 1
        raise RuntimeError("nope")

    monkeypatch.setattr(asyncio, "sleep", _noop)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _invoke, lambda e: category, job_id="j")
    assert calls == 1, f"{category.value} was retried"
    assert ei.value.category is category


async def test_a_summarizer_provider_failure_raises_a_structured_neutral_exception(monkeypatch):
    route = all_routes()["summarizer"]
    original = ConnectionError("upstream refused")

    async def _invoke(_t):
        raise original

    monkeypatch.setattr(asyncio, "sleep", _noop)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _invoke, lambda e: FC.CONNECTION, job_id="j")

    exc = ei.value
    assert exc.role == "summarizer"
    assert exc.category is FC.CONNECTION            # normalized category retained
    assert exc.provider == "deepseek"               # the ACTUAL provider attempted
    assert exc.model == "deepseek-v4-flash"         # the ACTUAL model attempted
    assert exc.attempts == 2
    assert exc.phase is Phase.PROVIDER
    assert exc.__cause__ is original, "the original provider exception must remain the cause"
    assert not isinstance(exc, AttributeError)


async def test_the_neutral_exception_reports_the_real_cause_not_an_application_defect(monkeypatch):
    route = all_routes()["summarizer"]

    async def _invoke(_t):
        raise ConnectionError("down")

    monkeypatch.setattr(asyncio, "sleep", _noop)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _invoke, lambda e: FC.CONNECTION, job_id="j")
    assert ei.value.category is FC.CONNECTION
    assert ei.value.category is not FC.APPLICATION_DEFECT
    assert isinstance(ei.value.__cause__, ConnectionError)


async def test_a_timeout_releases_every_capacity_lease(monkeypatch):
    route = all_routes()["summarizer"]

    async def _stall(_t):
        await asyncio.sleep(999)
        return ("never", Usage())

    # a real, tiny per-attempt timeout so wait_for genuinely fires
    tight = _replace_timeout(route, 0.01)
    with pytest.raises(RouteExhausted):
        await execute(tight, _stall, lambda e: FC.CONNECTION, job_id="j")
    assert await model_capacity.depth(route.primary.domain.key) == 0


async def test_cancellation_releases_every_capacity_lease():
    route = all_routes()["summarizer"]
    started = asyncio.Event()

    async def _stall(_t):
        started.set()
        await asyncio.sleep(999)
        return ("never", Usage())

    task = asyncio.create_task(execute(route, _stall, lambda e: FC.CONNECTION, job_id="j"))
    await started.wait()
    assert await model_capacity.depth(route.primary.domain.key) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await model_capacity.depth(route.primary.domain.key) == 0


async def _noop(_s, *a, **k):
    return None


def _replace_timeout(route, timeout_s):
    import dataclasses
    return dataclasses.replace(route, timeout_s=timeout_s)
