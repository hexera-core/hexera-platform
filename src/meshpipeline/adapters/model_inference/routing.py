# Responsibility: Choose a route for a capability and drive its attempts until one succeeds or all are exhausted.
# Owns: attempt ordering, capacity admission, breaker consultation, usage accounting, and the exhaustion error.
# Boundaries: it decides WHICH target is tried and whether to try again.
# Collaborates with: adapters/_shared/resilience.py, routes.py and contracts/model_capacity.py.
from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from meshpipeline.contracts import inference_telemetry as telemetry
from meshpipeline.contracts import model_capacity as capacity
from meshpipeline.contracts.inference_telemetry import InferenceCall
from meshpipeline.contracts.model_routing import (
    FailureCategory,
    ModelRoute,
    RouteTarget,
    SelectionReason,
)

logger = logging.getLogger(__name__)


class Phase(str, Enum):

    ADMISSION = "admission"
    CIRCUIT = "circuit"
    TIMEOUT = "timeout"
    PROVIDER = "provider"


class RouteExhausted(Exception):

    def __init__(self, role: str, category: FailureCategory, detail: str = "", *,
                 provider: str = "", model: str = "", attempts: int = 0,
                 phase: Phase = Phase.PROVIDER) -> None:
        self.role = role
        self.category = category
        self.detail = detail
        self.provider = provider
        self.model = model
        self.attempts = attempts
        self.phase = phase
        super().__init__(
            f"{role}: no route available - category={category.value} phase={phase.value} "
            f"provider={provider or '?'} model={model or '?'} attempts={attempts} "
            f"{detail}".strip())


class Usage:

    __slots__ = ("input_tokens", "cached_input_tokens", "output_tokens", "ttft_s")

    def __init__(self, input_tokens: int = 0, cached_input_tokens: int = 0,
                 output_tokens: int = 0, ttft_s: float | None = None) -> None:
        self.input_tokens = input_tokens
        self.cached_input_tokens = cached_input_tokens
        self.output_tokens = output_tokens
        self.ttft_s = ttft_s


def _backoff(route: ModelRoute, category: FailureCategory, attempt: int) -> float:
    raw = route.retry.backoff_for(category, attempt)
    j = route.retry.jitter
    if j <= 0:
        return raw
    return max(0.0, raw * (1.0 + random.uniform(-j, j)))


class _Budget:

    __slots__ = ("_deadline",)

    def __init__(self, total_s: float | None) -> None:
        self._deadline = None if total_s is None else time.monotonic() + total_s

    @property
    def uncapped(self) -> bool:
        return self._deadline is None

    def remaining(self) -> float:
        if self._deadline is None:
            return float("inf")
        return self._deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def cap(self, want: float) -> float:
        return want if self.uncapped else max(0.0, min(want, self.remaining()))


def _breaker_for(target: RouteTarget):
    # Keyed by the DECLARED CIRCUIT GROUP - never by the derived quota domain. The group is the
    # name operators see in /readyz, so it must survive a model change; the domain is free to
    # change with routing. Roles sharing a group share failure history (builder+planner), and a
    # standby declares its own so a sick primary never opens it.
    from meshpipeline.adapters._shared.resilience import get_breaker
    return get_breaker(target.circuit_group)


def _domain_budget(domain_key: str, route: ModelRoute) -> int:
    try:
        from meshpipeline.adapters.model_inference.routes import all_routes, domain_budget
        # the live route is included so its own declared budget counts even when the
        # registry has not been built (tests, and any caller holding a route directly)
        return domain_budget(domain_key, {**all_routes(), route.role: route})
    except Exception:  # noqa: BLE001
        return route.concurrency_budget


async def execute(
    route: ModelRoute,
    invoke: Callable[[RouteTarget], Awaitable[tuple[Any, Usage]]],
    classify: Callable[[BaseException], FailureCategory],
    *,
    job_id: str = "",
    cost_of: Callable[[RouteTarget, Usage], float] | None = None,
) -> tuple[Any, RouteTarget, int]:
    last_category = FailureCategory.SERVICE_UNAVAILABLE
    last_detail = ""
    last_cause: BaseException | None = None
    last_phase = Phase.PROVIDER
    last_target: RouteTarget | None = None
    last_attempts = 0
    failover_occurred = False
    # ONE budget for the whole route: admission + attempts + backoff all spend from it.
    budget_clock = _Budget(route.total_deadline_s)

    for index, target in enumerate(route.targets):
        is_standby = index > 0
        reason = _selection_reason(is_standby, last_category, route)
        last_target = target

        breaker = _breaker_for(target)
        if not breaker.allow():
            last_category, last_phase = FailureCategory.CIRCUIT_OPEN, Phase.CIRCUIT
            last_detail = f"circuit open for {target.label}"
            logger.warning("route %s: %s - not dialling %s", route.role, last_detail, target.label)
            _emit(route, target, reason, job_id, attempts=0, category=last_category,
                  circuit_state=_state(breaker), failover=is_standby, cost_of=cost_of)
            if not route.may_fail_over(last_category):
                break
            failover_occurred = True
            continue

        # The ceiling belongs to the QUOTA DOMAIN, not to this role: two roles on the same
        # provider+model share one pool, so they must contend against one number.
        budget = _domain_budget(target.domain.key, route)
        # Queueing spends the SAME total budget as the attempts do.
        queue_wait = budget_clock.cap(route.queue_deadline_s)
        lease = await capacity.acquire(target.domain.key, budget, queue_wait)
        if lease is None:
            expired = budget_clock.expired()
            last_category = FailureCategory.TIMEOUT if expired else FailureCategory.OVERLOAD
            last_phase = Phase.TIMEOUT if expired else Phase.ADMISSION
            _why = ("route deadline exhausted while queued" if expired
                    else f"queue deadline {queue_wait:.1f}s exceeded")
            last_detail = f"{target.label}: {_why} at budget {budget}"
            logger.warning("route %s: %s", route.role, last_detail)
            _emit(route, target, reason, job_id, attempts=0, category=last_category,
                  circuit_state=_state(breaker), failover=is_standby, cost_of=cost_of,
                  timed_out=expired)
            if not route.may_fail_over(last_category):
                break
            failover_occurred = True
            continue

        try:
            (result, category, detail, attempts, usage, latency, started,
             cause) = await _attempt_target(route, target, invoke, classify, budget_clock)
        finally:
            # ALWAYS returns the slot: success, failure, timeout, and cancellation alike.
            await capacity.release(lease)
        last_attempts = attempts

        if category is None:
            breaker.record_success()
            _emit(route, target, reason, job_id, attempts=attempts, category=None,
                  circuit_state=_state(breaker), failover=failover_occurred or is_standby,
                  lease=lease, usage=usage, latency=latency, started=started,
                  status="ok", cost_of=cost_of)
            return result, target, attempts

        last_category, last_detail, last_cause = category, detail, cause
        last_phase = Phase.TIMEOUT if category is FailureCategory.TIMEOUT else Phase.PROVIDER
        if category in (FailureCategory.RATE_LIMIT, FailureCategory.OVERLOAD,
                        FailureCategory.TIMEOUT, FailureCategory.CONNECTION,
                        FailureCategory.SERVICE_UNAVAILABLE,
                        FailureCategory.MODEL_UNAVAILABLE):
            breaker.record_failure()

        _emit(route, target, reason, job_id, attempts=attempts, category=category,
              circuit_state=_state(breaker), failover=failover_occurred or is_standby,
              lease=lease, usage=usage, latency=latency, started=started,
              status=_status_for(category),
              timed_out=category is FailureCategory.TIMEOUT, cost_of=cost_of)

        if not route.may_fail_over(category):
            # Our defect, or no standby: stop. Routing around it would buy the same failure.
            break
        failover_occurred = True

    # The neutral application failure. The provider's SDK exception is CHAINED, never promoted:
    # a caller must not need `openai.RateLimitError` to handle a failure, but an operator must
    # still be able to see what actually happened.
    raise RouteExhausted(
        route.role, last_category, last_detail,
        provider=last_target.provider if last_target else "",
        model=last_target.model if last_target else "",
        attempts=last_attempts, phase=last_phase,
    ) from last_cause


async def _attempt_target(route, target, invoke, classify, budget: _Budget):
    started = datetime.now(UTC).isoformat()
    t0 = time.monotonic()
    usage = Usage()
    detail = ""
    category: FailureCategory | None = None
    cause: BaseException | None = None
    attempt = 0

    for attempt in range(route.retry.max_attempts):
        # An attempt may never outlive the route's total budget. Without this, `timeout_s` is
        # per-attempt and N attempts cost N x timeout - the caller waits for a deadline nobody
        # declared.
        attempt_timeout = budget.cap(route.timeout_s)
        if budget.expired():
            return (None, FailureCategory.TIMEOUT,
                    f"route deadline {route.total_deadline_s}s exhausted after {attempt} attempt(s)",
                    attempt or 1, usage, time.monotonic() - t0, started, cause)
        try:
            result, usage = await asyncio.wait_for(invoke(target), timeout=attempt_timeout)
            return result, None, "", attempt + 1, usage, time.monotonic() - t0, started, None
        except asyncio.CancelledError:
            # Never swallowed: the pipeline cancels on its own deadlines and must keep doing so.
            raise
        except TimeoutError as exc:
            category, cause = FailureCategory.TIMEOUT, exc
            detail = f"timeout after {attempt_timeout:.1f}s"
        except BaseException as exc:  # noqa: BLE001 - classified, then retried or surfaced
            category, cause = classify(exc), exc
            detail = f"{type(exc).__name__}"
        if category not in _RETRYABLE:
            # Terminal for this target: our defect, or a category retrying cannot help.
            return (None, category, detail, attempt + 1, usage, time.monotonic() - t0, started,
                    cause)
        if attempt < route.retry.max_attempts - 1:
            delay = budget.cap(_backoff(route, category, attempt))
            if budget.expired():
                break
            await asyncio.sleep(delay)
    return (None, category or FailureCategory.SERVICE_UNAVAILABLE, detail,
            attempt + 1, usage, time.monotonic() - t0, started, cause)


_RETRYABLE: frozenset[FailureCategory] = frozenset({
    FailureCategory.RATE_LIMIT,
    FailureCategory.OVERLOAD,
    FailureCategory.TIMEOUT,
    FailureCategory.CONNECTION,
    FailureCategory.SERVICE_UNAVAILABLE,
    FailureCategory.MODEL_UNAVAILABLE,
    FailureCategory.EMPTY_RESPONSE,
})
"""Retryable AGAINST THE SAME TARGET. Note EMPTY_RESPONSE is retryable but NOT failover-
eligible: an empty completion is worth asking the same model again, and is not evidence the
provider is unwell."""


def _selection_reason(is_standby: bool, last: FailureCategory, route: ModelRoute) -> str:
    if not is_standby:
        return (SelectionReason.PRIMARY.value if route.standby is not None
                else SelectionReason.NO_STANDBY_CONFIGURED.value)
    if last is FailureCategory.CIRCUIT_OPEN:
        return SelectionReason.STANDBY_PRIMARY_CIRCUIT_OPEN.value
    if last is FailureCategory.OVERLOAD:
        return SelectionReason.STANDBY_PRIMARY_SATURATED.value
    return SelectionReason.STANDBY_PRIMARY_FAILED.value


def _state(breaker) -> str:
    try:
        return breaker.state.value
    except Exception:  # noqa: BLE001
        return ""


def _status_for(category: FailureCategory) -> str:
    return {FailureCategory.RATE_LIMIT: "429",
            FailureCategory.SERVICE_UNAVAILABLE: "503",
            FailureCategory.OVERLOAD: "503"}.get(category, category.value)


def _emit(route, target, reason, job_id, *, attempts, category, circuit_state,
          failover, cost_of, lease=None, usage=None, latency=0.0, started="",
          status="", timed_out=False):
    usage = usage or Usage()
    cost = 0.0
    if cost_of is not None:
        try:
            cost = cost_of(target, usage)
        except Exception:  # noqa: BLE001
            cost = 0.0
    telemetry.record(InferenceCall(
        job_id=job_id,
        role=route.role,
        route_id=f"{route.role}:{'standby' if failover and target is route.standby else 'primary'}",
        provider=target.provider,
        model=target.model,
        target="standby" if target is route.standby else "primary",
        selection_reason=reason,
        queue_wait_s=round(lease.waited_s, 4) if lease else 0.0,
        active_at_admission=lease.active_at_admission if lease else 0,
        attempts=attempts,
        circuit_state=circuit_state,
        started_at=started or datetime.now(UTC).isoformat(),
        ended_at=datetime.now(UTC).isoformat(),
        latency_s=round(latency, 4),
        ttft_s=usage.ttft_s,
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        estimated_cost_usd=round(cost, 6),
        provider_status=status or (category.value if category else "ok"),
        failure_category=category.value if category else "",
        timed_out=timed_out,
        cancelled=False,
        failover_occurred=bool(failover),
    ))
