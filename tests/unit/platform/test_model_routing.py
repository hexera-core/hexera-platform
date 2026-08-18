# Responsibility: Verify the routing ceiling, retries, failover eligibility and lease release under concurrency.
# Boundaries: our own defects never reach a standby, and every attempt emits exactly one record.
from __future__ import annotations

import asyncio

import pytest

from meshpipeline.adapters._shared.resilience import reset_breakers
from meshpipeline.adapters.model_capacity.local import LocalCapacityController
from meshpipeline.adapters.model_inference.routing import RouteExhausted, Usage, execute
from meshpipeline.contracts import model_capacity
from meshpipeline.contracts.model_routing import (
    FAILOVER_ELIGIBLE,
    Capability,
    FailureCategory,
    ModelRoute,
    RetryPolicy,
    RouteTarget,
    SelectionReason,
)

# Distinct circuit groups: the standby MUST NOT share the primary's failure history, or the
# outage it exists to survive would open it too. OTHER shares the primary's group on purpose -
# a different model (so a different quota domain) can still be one failure domain.
PRIMARY = RouteTarget(provider="fakeprov", model="primary-model", circuit_group="grp_primary")
STANDBY = RouteTarget(provider="otherprov", model="standby-model", circuit_group="grp_standby")
OTHER = RouteTarget(provider="fakeprov", model="other-model", circuit_group="grp_other")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    reset_breakers()
    model_capacity.set_capacity_controller(LocalCapacityController(poll_interval_s=0.001))
    records: list = []

    class _Sink:
        def record(self, call): records.append(call)

    from meshpipeline.contracts import inference_telemetry
    inference_telemetry.set_inference_telemetry(_Sink())
    # NOTE: asyncio.sleep is deliberately NOT stubbed. Backoff is disabled per-route
    # (backoff_base_s=0), which keeps these fast without breaking the real timeout and
    # cancellation semantics the lease tests exist to prove.
    yield records
    inference_telemetry.set_inference_telemetry(None)
    model_capacity.set_capacity_controller(None)
    reset_breakers()


def _route(**over) -> ModelRoute:
    base = {
        "role": "testrole", "primary": PRIMARY, "standby": None,
        "required_capabilities": frozenset({Capability.TOOLS}),
        "timeout_s": 0.5,
        "retry": RetryPolicy(max_attempts=3, backoff_base_s=0, backoff_max_s=0, jitter=0),
        "concurrency_budget": 4, "queue_deadline_s": 0.2,
    }
    base.update(over)
    return ModelRoute(**base)


def _ok(_target):
    async def _f(_t):
        return ("result", Usage(input_tokens=10, output_tokens=5))
    return _f


def _raises(exc):
    async def _f(_t):
        raise exc
    return _f


def _classify_fixed(category):
    return lambda _exc: category


# the ceiling
async def test_the_configured_ceiling_is_never_exceeded_under_heavy_fan_in():
    route = _route(concurrency_budget=200, queue_deadline_s=30.0)
    peak = 0
    live = 0
    lock = asyncio.Lock()

    async def _invoke(_t):
        nonlocal peak, live
        async with lock:
            live += 1
            peak = max(peak, live)
        await asyncio.sleep(0)          # a real await point, so overlap is possible
        async with lock:
            live -= 1
        return ("ok", Usage())

    await asyncio.gather(*[
        execute(route, _invoke, _classify_fixed(FailureCategory.CONNECTION), job_id="j")
        for _ in range(250)
    ])
    assert peak <= 200, f"admission let {peak} calls into a 200-slot domain"
    assert await model_capacity.depth(PRIMARY.domain.key) == 0, "leases leaked after success"


async def test_roles_sharing_a_quota_domain_share_one_ceiling():
    budget = 3
    a = _route(role="role_a", concurrency_budget=budget, queue_deadline_s=30.0)
    b = _route(role="role_b", concurrency_budget=budget, queue_deadline_s=30.0)
    assert a.primary.domain.key == b.primary.domain.key

    peak = 0
    live = 0
    lock = asyncio.Lock()

    async def _invoke(_t):
        nonlocal peak, live
        async with lock:
            live += 1
            peak = max(peak, live)
        await asyncio.sleep(0)
        async with lock:
            live -= 1
        return ("ok", Usage())

    await asyncio.gather(*[
        execute(r, _invoke, _classify_fixed(FailureCategory.CONNECTION), job_id="j")
        for r in ([a] * 10 + [b] * 10)
    ])
    assert peak <= budget, f"two roles on one domain reached {peak}, ceiling was {budget}"


async def test_separate_provider_model_domains_stay_independent():
    blocked = asyncio.Event()
    saturated = _route(role="sat", primary=PRIMARY, concurrency_budget=1, queue_deadline_s=5.0)
    other = _route(role="oth", primary=OTHER, concurrency_budget=1, queue_deadline_s=0.05)

    async def _hold(_t):
        await blocked.wait()
        return ("held", Usage())

    holder = asyncio.create_task(
        execute(saturated, _hold, _classify_fixed(FailureCategory.CONNECTION), job_id="j"))
    await asyncio.sleep(0)              # let the holder take the only slot

    # the OTHER domain is untouched and serves immediately
    result, target, _attempts = await execute(other, _ok(OTHER),
                                   _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert result == "result" and target is OTHER
    blocked.set()
    await holder


async def test_queue_deadline_is_respected_and_reports_saturation():
    blocked = asyncio.Event()
    route = _route(concurrency_budget=1, queue_deadline_s=0.05)

    async def _hold(_t):
        await blocked.wait()
        return ("held", Usage())

    holder = asyncio.create_task(
        execute(route, _hold, _classify_fixed(FailureCategory.CONNECTION), job_id="j"))
    await asyncio.sleep(0)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _ok(PRIMARY), _classify_fixed(FailureCategory.CONNECTION),
                      job_id="j")
    assert ei.value.category is FailureCategory.OVERLOAD
    blocked.set()
    await holder


# retry vs failover
async def test_transient_failures_retry_against_the_same_route_and_stay_bounded():
    calls = 0

    async def _flaky(_t):
        nonlocal calls
        calls += 1
        raise ConnectionError("blip")

    route = _route(retry=RetryPolicy(max_attempts=3, backoff_base_s=0, backoff_max_s=0, jitter=0))
    with pytest.raises(RouteExhausted):
        await execute(route, _flaky, _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert calls == 3, f"retry was not bounded by max_attempts (made {calls} calls)"


async def test_a_single_transient_error_does_not_change_provider():
    seen: list[str] = []
    attempts = 0

    async def _once_then_ok(t):
        nonlocal attempts
        seen.append(t.provider)
        attempts += 1
        if attempts == 1:
            raise ConnectionError("blip")
        return ("ok", Usage())

    route = _route(standby=STANDBY)
    result, target, _attempts = await execute(route, _once_then_ok,
                                   _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert result == "ok" and target is PRIMARY
    assert set(seen) == {"fakeprov"}, f"a single blip reached the standby: {seen}"


async def test_no_standby_is_used_when_none_is_configured():
    route = _route(standby=None)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _raises(ConnectionError("down")),
                      _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert ei.value.category is FailureCategory.CONNECTION


async def test_an_eligible_failure_activates_the_configured_standby(_fresh):
    async def _invoke(t):
        if t.provider == "fakeprov":
            raise ConnectionError("primary down")
        return ("standby-result", Usage(input_tokens=1))

    route = _route(standby=STANDBY)
    result, target, _attempts = await execute(route, _invoke,
                                   _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert result == "standby-result" and target is STANDBY
    served = [r for r in _fresh if r.failure_category == ""]
    assert served and served[-1].target == "standby"
    assert served[-1].selection_reason == SelectionReason.STANDBY_PRIMARY_FAILED.value
    assert served[-1].failover_occurred is True


@pytest.mark.parametrize("category", sorted(
    {FailureCategory.AUTH, FailureCategory.INVALID_REQUEST,
     FailureCategory.INSUFFICIENT_BALANCE, FailureCategory.TOOL_SCHEMA,
     FailureCategory.APPLICATION_DEFECT, FailureCategory.POLICY_REJECTION,
     FailureCategory.INVALID_OUTPUT, FailureCategory.QUALITY_FAILED},
    key=lambda c: c.value))
async def test_our_own_defects_never_reach_the_standby(category):
    reached: list[str] = []

    async def _invoke(t):
        reached.append(t.provider)
        raise RuntimeError("nope")

    route = _route(standby=STANDBY)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _invoke, _classify_fixed(category), job_id="j")
    assert ei.value.category is category
    assert set(reached) == {"fakeprov"}, f"{category.value} was routed to the standby: {reached}"


def test_a_route_cannot_declare_a_non_transient_failure_as_failover_eligible():
    with pytest.raises(ValueError, match="configuration/application failures"):
        _route(allowed_failover=frozenset({FailureCategory.AUTH}))


def test_the_eligible_and_never_sets_partition_the_vocabulary():
    from meshpipeline.contracts.model_routing import NEVER_FAILOVER
    assert FAILOVER_ELIGIBLE & NEVER_FAILOVER == frozenset()
    assert FAILOVER_ELIGIBLE | NEVER_FAILOVER == set(FailureCategory)


# circuit
async def test_an_open_primary_circuit_sends_traffic_to_the_standby_without_dialling():
    dialled: list[str] = []

    async def _invoke(t):
        dialled.append(t.provider)
        return ("ok", Usage())

    from meshpipeline.adapters._shared.resilience import get_breaker
    b = get_breaker(PRIMARY.circuit_group)
    for _ in range(b.failure_threshold):
        b.record_failure()
    assert not b.allow()

    route = _route(standby=STANDBY)
    result, target, _attempts = await execute(route, _invoke,
                                   _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert result == "ok" and target is STANDBY
    assert dialled == ["otherprov"], "an open circuit still dialled the primary"


async def test_both_routes_unavailable_fails_clearly():
    route = _route(standby=STANDBY)
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _raises(ConnectionError("all down")),
                      _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert ei.value.role == "testrole"
    assert ei.value.category is FailureCategory.CONNECTION


# leases are always returned
async def test_the_lease_is_released_after_success():
    await execute(_route(), _ok(PRIMARY), _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert await model_capacity.depth(PRIMARY.domain.key) == 0


async def test_the_lease_is_released_after_failure():
    with pytest.raises(RouteExhausted):
        await execute(_route(), _raises(ConnectionError("x")),
                      _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert await model_capacity.depth(PRIMARY.domain.key) == 0


async def test_the_lease_is_released_after_timeout():
    async def _stall(_t):
        await asyncio.sleep(10)
        return ("never", Usage())

    # a real (short) timeout, so asyncio.wait_for genuinely fires
    route = _route(timeout_s=0.01,
                   retry=RetryPolicy(max_attempts=1, backoff_base_s=0, backoff_max_s=0, jitter=0))
    with pytest.raises(RouteExhausted) as ei:
        await execute(route, _stall, _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert ei.value.category is FailureCategory.TIMEOUT
    assert await model_capacity.depth(PRIMARY.domain.key) == 0, "a timeout leaked its lease"


async def test_the_lease_is_released_after_cancellation():
    started = asyncio.Event()

    async def _stall(_t):
        started.set()
        await asyncio.sleep(10)
        return ("never", Usage())

    task = asyncio.create_task(
        execute(_route(timeout_s=30), _stall, _classify_fixed(FailureCategory.CONNECTION),
                job_id="j"))
    await started.wait()
    assert await model_capacity.depth(PRIMARY.domain.key) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await model_capacity.depth(PRIMARY.domain.key) == 0, "cancellation leaked its lease"


# provenance + no loss / no duplication
async def test_provenance_identifies_the_route_that_actually_served_the_call(_fresh):
    await execute(_route(), _ok(PRIMARY), _classify_fixed(FailureCategory.CONNECTION),
                  job_id="job-7")
    rec = _fresh[-1]
    assert rec.job_id == "job-7" and rec.role == "testrole"
    assert rec.provider == "fakeprov" and rec.model == "primary-model"
    assert rec.target == "primary"
    assert rec.selection_reason == SelectionReason.NO_STANDBY_CONFIGURED.value
    assert rec.failure_category == "" and rec.attempts == 1
    assert rec.input_tokens == 10 and rec.output_tokens == 5


async def test_every_call_emits_exactly_one_record_per_attempted_route(_fresh):
    async def _invoke(t):
        if t.provider == "fakeprov":
            raise ConnectionError("down")
        return ("ok", Usage())

    await execute(_route(standby=STANDBY), _invoke,
                  _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert len(_fresh) == 2, f"expected one record per attempted route, got {len(_fresh)}"
    assert [r.target for r in _fresh] == ["primary", "standby"]
    assert _fresh[0].failure_category == FailureCategory.CONNECTION.value
    assert _fresh[1].failure_category == ""


async def test_a_successful_call_is_never_executed_twice():
    calls = 0

    async def _count(_t):
        nonlocal calls
        calls += 1
        return ("ok", Usage())

    await execute(_route(standby=STANDBY), _count,
                  _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    assert calls == 1, f"a successful call ran {calls} times"


async def test_telemetry_carries_no_content_fields(_fresh):
    await execute(_route(), _ok(PRIMARY), _classify_fixed(FailureCategory.CONNECTION), job_id="j")
    fields = set(_fresh[-1].as_record())
    forbidden = {"prompt", "messages", "content", "completion", "text", "api_key",
                 "authorization", "headers", "tool_arguments", "output"}
    assert fields & forbidden == set(), f"telemetry record exposes content fields: {fields & forbidden}"
